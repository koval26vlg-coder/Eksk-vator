"""Автоматическое выставление лимиток по сигналам скальпинга и (опционально) арбитражу."""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from config import Settings
from execution import OrderExecutor
from orderbook import (
    simulated_slippage_bps_buy,
    simulated_slippage_bps_sell,
    spread_bps,
    vwap_buy_base,
    vwap_buy_with_quote,
    vwap_sell_base,
)
from scalping import ScalpSignal
from scanner import Opportunity, Quote

log = logging.getLogger(__name__)

# region agent log
def _dbg_log(hypothesis_id: str, location: str, message: str, data: dict[str, Any]) -> None:
    try:
        payload = {
            "sessionId": "fd290c",
            "runId": "pre-change",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        with open("debug-fd290c.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        return
# endregion agent log


class _AutoTradeAnalytics:
    """Простая сессионная аналитика причин пропусков/блокировок в auto_trade."""

    _META_PREFIXES = ("vol_regime_", "exit_max_hold_defer_")

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._by_symbol: dict[tuple[str, str], dict[str, int]] = {}

    def inc(self, reason: str, exchange_id: str, symbol: str) -> None:
        r = str(reason)
        self._counts[r] = int(self._counts.get(r, 0)) + 1
        k = (str(exchange_id), str(symbol))
        m = self._by_symbol.setdefault(k, {})
        m[r] = int(m.get(r, 0)) + 1

    def report_lines(self, *, top_reasons: int = 12, top_symbols: int = 12) -> list[str]:
        if not self._counts:
            return []

        def _is_meta(reason: str) -> bool:
            r = str(reason)
            return any(r.startswith(p) for p in self._META_PREFIXES)

        lines: list[str] = []
        total = sum(c for r, c in self._counts.items() if not _is_meta(r))
        lines.append(f"auto_trade-отчёт: skip/block events={total}")

        reasons = sorted(
            [(r, c) for r, c in self._counts.items() if not _is_meta(r)],
            key=lambda kv: kv[1],
            reverse=True,
        )
        lines.append("auto_trade-отчёт: топ причин (count↓):")
        for r, c in reasons[: max(1, int(top_reasons))]:
            lines.append(f"  - {r}: {int(c)}")

        # Top symbols by total events
        sym_rows: list[tuple[int, tuple[str, str], dict[str, int]]] = []
        for k, m in self._by_symbol.items():
            sym_rows.append((sum(c for r, c in m.items() if not _is_meta(r)), k, m))
        sym_rows.sort(key=lambda x: x[0], reverse=True)
        if sym_rows:
            lines.append("auto_trade-отчёт: топ инструментов по числу событий (count↓):")
            for cnt, (ex, sym), m in sym_rows[: max(1, int(top_symbols))]:
                top = sorted(
                    [(r, c) for r, c in m.items() if not _is_meta(r)],
                    key=lambda kv: kv[1],
                    reverse=True,
                )[:3]
                tail = ", ".join(f"{r}={c}" for r, c in top)
                lines.append(f"  - {ex} {sym}: {int(cnt)} ({tail})")

        return lines


class ScalpAutoTrader:
    """Лимитка у bid/ask; при наличии стакана — проверка глубины и проскальзывания (симуляция съедания книги)."""

    def __init__(
        self,
        settings: Settings,
        executor: OrderExecutor,
        logger: logging.Logger,
        *,
        atr_bps_provider: Callable[[str, str], float | None] | None = None,
    ) -> None:
        self._s = settings
        self._ex = executor
        self._log = logger
        self._atr_bps_provider = atr_bps_provider
        # region agent log
        _dbg_log(
            "H1",
            "auto_trade.py:ScalpAutoTrader.__init__",
            "settings snapshot",
            {
                "paper": bool(getattr(settings, "paper", False)),
                "symbols": list(getattr(settings, "symbols", ()) or ()),
                "reduce_only": bool(getattr(settings, "auto_trade_reduce_only", False)),
                "reduce_only_scope": str(getattr(settings, "auto_trade_reduce_only_scope", "")),
                "max_open_positions": int(getattr(settings, "auto_trade_max_open_positions", 0) or 0),
                "max_same_dir": int(getattr(settings, "auto_trade_max_positions_same_direction", 0) or 0),
            },
        )
        # endregion agent log
        self._last_mono: dict[tuple[str, str], float] = {}
        # Anti-churn: после выхода по символу не входить повторно в ту же сторону N секунд.
        # Ключ: cooldown_key(exchange_id,symbol) + side ("buy"/"sell").
        self._last_exit_side_mono: dict[tuple[str, str, str], float] = {}
        self._pending_order_ids: dict[tuple[str, str], list[str]] = {}
        # pending-тип ордера: "entry" | "exit" (нужно, чтобы cancel_pending перед SL/MAX_HOLD не снимал свежий exit)
        self._pending_kind: dict[str, str] = {}
        # live: TTL/reprice — created_mono / last_reprice_mono (monotonic), сторона и цена лимитки
        self._live_meta: dict[str, dict[str, Any]] = {}
        # σ тиков для масштаба номинала и детекции всплеска (по паре)
        self._vol_scale_state: dict[tuple[str, str], dict[str, Any]] = {}
        self._prev_eff_sigma: dict[tuple[str, str], float] = {}
        self._sigma_spike_until_mono: dict[tuple[str, str], float] = {}
        #: При AUTO_TRADE_COOLDOWN_SCOPE=symbol — сериализация on_signal по паре (два WS не гонятся за одним кулдауном).
        self._symbol_scope_locks: dict[str, asyncio.Lock] = {}
        # Авто-выход: когда позиция по ноге (биржа+символ) считается «открытой» (для max-hold).
        # Ключ: "{exchange_id}:{symbol}".
        self._pos_open_mono: dict[str, float] = {}
        # Чтобы не спамить причинами "почему exit пропущен" на каждом тике.
        self._exit_skip_log_mono: dict[str, float] = {}
        # Backoff после отказа выставления exit (например, risk per_order чуть меньше qty×price).
        self._exit_reject_until_mono: dict[tuple[str, str], float] = {}
        # Quiet-market guard: окно mid по ноге (биржа+символ), чтобы отсекать «тихий» рынок.
        # key=(exchange_id, symbol) -> deque[(mono_ts, mid)]
        self._mid_window: dict[tuple[str, str], deque[tuple[float, float]]] = {}
        # Последний mid по ноге для отчёта unrealized в конце сессии (обновляется при любом стакане/тике).
        self._last_mid: dict[tuple[str, str], float] = {}
        # Smart MAX_HOLD: последний TA-сигнал по ноге (биржа+символ), чтобы не закрывать позицию "в минус" по таймеру,
        # если сигнал всё ещё в сторону позиции.
        # key=(exchange_id, symbol) -> (mono_ts, side, impulse_bps)
        self._last_sig: dict[tuple[str, str], tuple[float, str, float | None]] = {}
        # Чтобы не спамить "quiet guard" на каждом сигнале.
        self._quiet_skip_log_mono: dict[tuple[str, str], float] = {}
        # Троттлинг для прочих «входных» guard-логов (single-open и т.п.).
        self._entry_skip_log_mono: dict[tuple[str, str, str], float] = {}
        self._ana = _AutoTradeAnalytics() if bool(getattr(settings, "paper", False)) else None

    def paper_session_report_lines(self) -> list[str] | None:
        """Многострочная аналитика причин пропусков (paper)."""
        if not self._s.paper or self._ana is None:
            return None
        out = self._ana.report_lines()
        return out or None

    def paper_unrealized_report_lines(self) -> list[str] | None:
        """Paper: unrealized PnL по открытому инвентарю на момент остановки."""
        if not self._s.paper:
            return None
        pos_list = self._ex.paper_open_positions() or []
        if not pos_list:
            return None

        fee_side_bps = float(self._s.paper_trading_fee_bps())
        lines: list[str] = []
        total_raw = 0.0
        total_net = 0.0
        missing_mid = 0

        rows: list[tuple[float, str]] = []
        for ex, sym, pos, entry in pos_list:
            mid = float(self._last_mid.get((ex, sym), 0.0) or 0.0)
            if mid <= 0:
                missing_mid += 1
                continue

            qty = abs(float(pos))
            if qty <= 1e-12:
                continue

            if pos > 0:
                pnl_bps = (mid / entry - 1.0) * 10_000.0
                pnl_quote_raw = qty * (mid - entry)
                side = "long"
            else:
                pnl_bps = (entry / mid - 1.0) * 10_000.0
                pnl_quote_raw = qty * (entry - mid)
                side = "short"

            notional = qty * mid
            exit_fee = notional * fee_side_bps / 10_000.0
            pnl_quote_net = pnl_quote_raw - exit_fee
            pnl_net_bps = pnl_bps - fee_side_bps

            total_raw += pnl_quote_raw
            total_net += pnl_quote_net

            rows.append(
                (
                    pnl_quote_net,
                    "  - %s %s %s pos≈%.8g entry≈%.8g mid≈%.8g unreal≈%+.4f USDT (net≈%+.4f, ≈%+.1f bps net)"
                    % (
                        ex,
                        sym,
                        side,
                        pos,
                        entry,
                        mid,
                        pnl_quote_raw,
                        pnl_quote_net,
                        pnl_net_bps,
                    ),
                )
            )

        if not rows and missing_mid:
            return [f"unrealized-отчёт: позиции={len(pos_list)}, но нет mid для {missing_mid} ног(и)"]

        rows.sort(key=lambda x: x[0])  # худшие сверху
        lines.append(
            "unrealized-отчёт: позиций=%d | unreal raw≈%+.4f USDT | exit fees≈%.4f | unreal net≈%+.4f"
            % (
                len(pos_list),
                float(total_raw),
                float(total_raw - total_net),
                float(total_net),
            )
        )
        if missing_mid:
            lines.append(f"unrealized-отчёт: нет mid для {missing_mid} ног(и) — пропущены из расчёта")
        lines.append("unrealized-отчёт: по позициям (net↑ хуже→лучше):")
        lines.extend([s for (_p, s) in rows[:20]])
        return lines

    def _ana_inc(self, reason: str, exchange_id: str, symbol: str) -> None:
        if self._ana is not None:
            self._ana.inc(reason, exchange_id, symbol)

    def _quiet_skip_log_ok(self, exchange_id: str, symbol: str, now_mono: float, *, throttle_s: float = 30.0) -> bool:
        key = (str(exchange_id), str(symbol))
        prev = float(self._quiet_skip_log_mono.get(key, 0.0))
        if now_mono - prev < throttle_s:
            return False
        self._quiet_skip_log_mono[key] = now_mono
        return True

    def _entry_skip_log_ok(
        self,
        tag: str,
        exchange_id: str,
        symbol: str,
        now_mono: float,
        *,
        throttle_s: float = 45.0,
    ) -> bool:
        key = (str(tag), str(exchange_id), str(symbol))
        prev = float(self._entry_skip_log_mono.get(key, 0.0))
        if now_mono - prev < throttle_s:
            return False
        self._entry_skip_log_mono[key] = now_mono
        return True

    def _record_mid(self, exchange_id: str, symbol: str, mid: float, now_mono: float) -> None:
        if mid <= 0:
            return
        key = (str(exchange_id), str(symbol))
        self._last_mid[key] = float(mid)
        thr_bps = float(getattr(self._s, "scalping_min_mid_range_bps", 0.0) or 0.0)
        auto_tune = bool(getattr(self._s, "auto_trade_auto_tune", False))
        if thr_bps <= 0 and not auto_tune:
            return
        win_s = float(getattr(self._s, "scalping_min_mid_range_window_seconds", 30.0) or 30.0)
        if win_s <= 0:
            return
        dq = self._mid_window.get(key)
        if dq is None:
            dq = deque()
            self._mid_window[key] = dq
        dq.append((now_mono, float(mid)))
        # Срежем «хвост» окна.
        cutoff = now_mono - win_s
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def _mid_range_bps(self, exchange_id: str, symbol: str, mid_ref: float, now_mono: float) -> float | None:
        """Диапазон (max-min) mid за окно в bps относительно mid_ref. None, если данных мало/выключено."""
        thr_bps = float(getattr(self._s, "scalping_min_mid_range_bps", 0.0) or 0.0)
        auto_tune = bool(getattr(self._s, "auto_trade_auto_tune", False))
        if thr_bps <= 0 and not auto_tune:
            return None
        win_s = float(getattr(self._s, "scalping_min_mid_range_window_seconds", 30.0) or 30.0)
        if win_s <= 0:
            return None
        if mid_ref <= 0:
            return None
        key = (str(exchange_id), str(symbol))
        dq = self._mid_window.get(key)
        if not dq:
            return None
        cutoff = now_mono - win_s
        # На всякий случай подчистим старое (если тик был без стакана).
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        if len(dq) < 2:
            return None
        # Нужна «зрелость» окна по времени: иначе min/max по двум тикам за долю секунды даёт ложный range.
        span_s = float(now_mono - dq[0][0])
        if span_s + 1e-9 < win_s * 0.9:
            return None
        mmin = min(v for (_t, v) in dq)
        mmax = max(v for (_t, v) in dq)
        if mmin <= 0 or mmax <= 0:
            return None
        # Как в WS-пульсе (ws_stream): (max−min)/текущий mid — иначе (max/min−1) завышает при росте цены внутри окна.
        return (mmax - mmin) / float(mid_ref) * 10_000.0

    def _exit_skip_log_ok(self, symbol: str, now_mono: float, *, throttle_s: float = 30.0) -> bool:
        sym = str(symbol)
        prev = float(self._exit_skip_log_mono.get(sym, 0.0))
        if now_mono - prev < throttle_s:
            return False
        self._exit_skip_log_mono[sym] = now_mono
        return True

    def _pos_is_dust(self, pos_base: float, mid: float) -> bool:
        """Пыль: |pos_base|×mid < AUTO_TRADE_POSITION_DUST_QUOTE (0 = выключено)."""
        thr = float(getattr(self._s, "auto_trade_position_dust_quote", 0.0) or 0.0)
        if thr <= 0:
            return False
        if mid <= 0:
            return False
        return abs(pos_base) * float(mid) < thr

    def _book_full_exit_net_ge_tp(
        self,
        pos: float,
        entry: float,
        amount: float,
        order_book: dict,
        tp_net: float,
        fee_exit_bps: float,
    ) -> bool:
        """True, если полный объём можно закрыть по стакану с VWAP так, что net (raw−exit_fee) ≥ tp_net.

        Используется для откладывания мягкого MAX_HOLD: пока глубина ещё позволяет «плюс» как у TP-логики.
        """
        if amount <= 0 or entry <= 0 or tp_net <= 0:
            return False
        bids = order_book.get("bids") or []
        asks = order_book.get("asks") or []
        if pos > 0:
            _quote, vwap, sold, complete = vwap_sell_base(bids, amount)
            if not complete or sold + 1e-12 < amount:
                return False
            raw_bps = (float(vwap) / float(entry) - 1.0) * 10_000.0
        else:
            _spent, vwap, bought, complete = vwap_buy_base(asks, amount)
            if not complete or bought + 1e-12 < amount:
                return False
            raw_bps = (float(entry) / float(vwap) - 1.0) * 10_000.0
        net_bps = raw_bps - float(fee_exit_bps)
        return net_bps + 1e-9 >= float(tp_net)

    def _has_pending_for_symbol(self, symbol: str) -> bool:
        sym = str(symbol)
        for (_ex, s), ids in self._pending_order_ids.items():
            if s == sym and ids:
                return True
        return False

    def _has_pending_exit_for_leg(self, exchange_id: str, symbol: str) -> bool:
        """Есть ли уже активная exit-лимитка по ноге (биржа+символ).

        Нужно, чтобы не выставлять второй exit при partial/задержке закрытия.
        """
        ids = self._pending_order_ids.get((exchange_id, str(symbol)), [])
        if not ids:
            return False
        for oid in ids:
            if str(self._pending_kind.get(oid, "entry")) != "exit":
                continue
            o = self._ex.get_paper_order(oid) if self._s.paper else None
            if o is None:
                # Если нет данных (или live) — считаем, что exit pending есть, лучше не дублировать.
                return True
            status = str(o.get("status", "open"))
            remaining = float(o.get("remaining", 0.0))
            if status in ("open", "partial") and remaining > 1e-18:
                return True
        return False

    async def _cancel_pending_symbol(self, symbol: str) -> int:
        """Снять все pending-ордера по symbol на всех биржах (paper и live).

        Используется для аварийного выхода (SL / MAX_HOLD), чтобы pending-entry не блокировал закрытие.
        Возвращает число попыток отмены (id).
        """
        sym = str(symbol)
        keys = [k for k, ids in self._pending_order_ids.items() if k[1] == sym and ids]
        if not keys:
            return 0
        count = 0
        for ex_id, s in keys:
            ids = self._pending_order_ids.pop((ex_id, s), [])
            for oid in ids:
                if str(self._pending_kind.get(oid, "entry")) != "entry":
                    # Не трогаем exit pending (иначе получаем partial→cancel→re-exit churn).
                    self._pending_order_ids.setdefault((ex_id, s), []).append(oid)
                    continue
                count += 1
                ok = await self._ex.cancel_order(ex_id, s, oid)
                if ok and not self._s.paper:
                    self._pop_live_meta(oid)
                self._pending_kind.pop(oid, None)
        return count

    def _exit_enabled(self) -> bool:
        return (
            float(self._s.auto_trade_tp_bps) > 0.0
            or float(self._s.auto_trade_sl_bps) > 0.0
            or float(self._s.auto_trade_max_hold_seconds) > 0.0
        )

    async def _maybe_exit_position_paper(
        self,
        exchange_id: str,
        symbol: str,
        *,
        best_bid: float,
        best_ask: float,
        order_book: dict,
        now_mono: float,
    ) -> None:
        if not self._s.paper:
            return
        if not self._exit_enabled():
            return

        # Позиции в live тут нет (нужны балансы/позиции), поэтому авто-выход включаем только для paper.
        # Важно: выход считаем по "ноге" (exchange_id, symbol), иначе при long/short на разных биржах
        # нетто-позиция по symbol может быть около нуля и exit станет слепым. Fallback на symbol — только запасной.
        pv = self._ex.paper_position_entry_vwap(exchange_id, symbol)
        if pv is None and self._s.auto_trade_reduce_only_scope == "symbol":
            pv = self._ex.paper_position_entry_vwap_symbol(symbol)
        if pv is None:
            if self._exit_skip_log_ok(symbol, now_mono):
                self._log.info("auto_trade: exit %s — пропуск: нет данных paper_position_entry_vwap*", symbol)
            self._ana_inc("exit_no_position_data", exchange_id, symbol)
            return
        pos, entry = pv
        bids = order_book.get("bids") or []
        asks = order_book.get("asks") or []
        mid = (best_bid + best_ask) / 2.0 if (best_bid > 0 and best_ask > 0) else 0.0
        if abs(pos) <= 1e-12 or entry is None or entry <= 0 or self._pos_is_dust(pos, mid):
            self._pos_open_mono.pop(f"{exchange_id}:{symbol}", None)
            if self._exit_skip_log_ok(symbol, now_mono):
                self._log.debug(
                    "auto_trade: exit %s — пропуск: позиция≈0 или нет entry (pos≈%.8f entry=%s)",
                    symbol,
                    pos,
                    entry,
                )
            return

        sym = str(symbol)
        leg_key = f"{exchange_id}:{sym}"
        self._pos_open_mono.setdefault(leg_key, now_mono)

        # Если ранее exit по этой ноге был отвергнут (risk/capital/ликвидность) — не спамим попытками на каждом тике.
        rej_key = (str(exchange_id), str(sym))
        until = float(self._exit_reject_until_mono.get(rej_key, 0.0) or 0.0)
        if now_mono < until:
            return

        if mid <= 0:
            if self._exit_skip_log_ok(symbol, now_mono):
                self._log.info("auto_trade: exit %s — пропуск: mid<=0 (bid=%s ask=%s)", symbol, best_bid, best_ask)
            self._ana_inc("exit_mid_invalid", exchange_id, symbol)
            return
        # pnl_bps: положительный = «в плюс» (и для long, и для short)
        if pos > 0:
            pnl_bps = (mid / entry - 1.0) * 10_000.0
            exit_side = "sell"
            exit_price = best_bid
        else:
            pnl_bps = (entry / mid - 1.0) * 10_000.0
            exit_side = "buy"
            exit_price = best_ask

        # entry_vwap в paper уже "съедает" комиссию на вход (buy для long / sell для short),
        # но комиссия на закрытие ещё впереди. Для TP/MAX_HOLD полезнее ориентироваться на net-оценку.
        fee_side_bps = float(self._s.paper_trading_fee_bps())
        pnl_net_bps = pnl_bps - fee_side_bps

        tp = float(self._s.auto_trade_tp_bps)
        sl = float(self._s.auto_trade_sl_bps)
        sl_atr_mult = float(getattr(self._s, "auto_trade_sl_atr_mult", 0.0) or 0.0)
        hold = float(self._s.auto_trade_max_hold_seconds)
        if pos > 0:
            hold_min_pnl = float(self._s.auto_trade_max_hold_min_pnl_bps_long)
        else:
            hold_min_pnl = float(self._s.auto_trade_max_hold_min_pnl_bps_short)
        hold_hard = float(getattr(self._s, "auto_trade_max_hold_hard_seconds", 0.0) or 0.0)

        atr_bps = self._atr_bps_provider(exchange_id, sym) if self._atr_bps_provider else None
        if atr_bps is not None and atr_bps > 0 and sl_atr_mult > 0:
            sl_eff = max(sl, sl_atr_mult * float(atr_bps))
        else:
            sl_eff = sl

        opened = float(self._pos_open_mono.get(leg_key, now_mono))
        age = now_mono - opened
        early_stop_s = float(getattr(self._s, "auto_trade_early_stop_seconds", 0.0) or 0.0)
        early_stop_loss = float(getattr(self._s, "auto_trade_early_stop_max_loss_net_bps", 0.0) or 0.0)
        early_stop_loss_raw = float(getattr(self._s, "auto_trade_early_stop_max_loss_raw_bps", 0.0) or 0.0)
        early_stop_min_age = float(getattr(self._s, "auto_trade_early_stop_min_age_seconds", 0.0) or 0.0)
        mid_stop_start = float(getattr(self._s, "auto_trade_mid_stop_start_seconds", 0.0) or 0.0)
        mid_stop_loss_raw = float(getattr(self._s, "auto_trade_mid_stop_max_loss_raw_bps", 0.0) or 0.0)
        mid_stop_thin = bool(getattr(self._s, "auto_trade_mid_stop_require_thin_book", True))
        mid_stop_spread_atr_m = float(getattr(self._s, "auto_trade_mid_stop_wide_spread_atr_mult", 0.0) or 0.0)

        reason: str | None = None
        if tp > 0 and pnl_net_bps >= tp:
            reason = f"TP net {pnl_net_bps:.1f}≥{tp:.1f} bps (raw≈{pnl_bps:.1f} fee≈{fee_side_bps:.1f})"
        elif sl_eff > 0 and pnl_bps <= -sl_eff:
            if atr_bps is not None and atr_bps > 0 and sl_atr_mult > 0:
                reason = (
                    f"SL {pnl_bps:.1f}≤-{sl_eff:.1f} bps "
                    f"(sl_bps={sl:.1f} atr≈{float(atr_bps):.1f}×{sl_atr_mult:.2f})"
                )
            else:
                reason = f"SL {pnl_bps:.1f}≤-{sl_eff:.1f} bps"
        elif (
            early_stop_s > 0
            and age + 1e-9 >= early_stop_min_age
            and age <= early_stop_s
            and (
                (early_stop_loss_raw > 0 and pnl_bps <= -early_stop_loss_raw)
                or (early_stop_loss_raw <= 0 and early_stop_loss > 0 and pnl_net_bps <= -early_stop_loss)
            )
        ):
            if early_stop_loss_raw > 0:
                reason = (
                    f"EARLY_STOP raw {pnl_bps:.1f}≤-{early_stop_loss_raw:.1f} bps in {age:.0f}s≤{early_stop_s:.0f}s "
                    f"(net≈{pnl_net_bps:.1f} fee≈{fee_side_bps:.1f})"
                )
            else:
                reason = (
                    f"EARLY_STOP net {pnl_net_bps:.1f}≤-{early_stop_loss:.1f} bps in {age:.0f}s≤{early_stop_s:.0f}s "
                    f"(raw≈{pnl_bps:.1f} fee≈{fee_side_bps:.1f})"
                )
        elif mid_stop_start > 0 and mid_stop_loss_raw > 0 and age >= mid_stop_start and pnl_bps <= -mid_stop_loss_raw:
            # Mid-phase invalidation: позиция уже "созрела" (не первые секунды),
            # и при этом убыточна заметно; закрываем только если выход объективно ухудшился.
            sp_bps = spread_bps(best_bid, best_ask)
            wide_spread = False
            if atr_bps is not None and atr_bps > 1e-9 and mid_stop_spread_atr_m > 0:
                wide_spread = sp_bps > float(atr_bps) * mid_stop_spread_atr_m + 1e-9

            thin_book = False
            if mid_stop_thin:
                max_slip = float(self._s.scalping_max_slippage_bps)
                amt = abs(float(pos))
                if pos > 0:
                    _q, vwap, _sold, complete = vwap_sell_base(bids, amt)
                    if not complete:
                        thin_book = True
                    else:
                        slip = simulated_slippage_bps_sell(best_bid, float(vwap), mid)
                        thin_book = slip > max_slip + 1e-9
                else:
                    _q, vwap, _bought, complete = vwap_buy_base(asks, amt)
                    if not complete:
                        thin_book = True
                    else:
                        slip = simulated_slippage_bps_buy(best_ask, float(vwap), mid)
                        thin_book = slip > max_slip + 1e-9

            if wide_spread or thin_book:
                self._ana_inc("mid_stop", exchange_id, sym)
                why = []
                if wide_spread:
                    if atr_bps is not None and atr_bps > 0 and mid_stop_spread_atr_m > 0:
                        why.append(f"spread {sp_bps:.2f}bps>ATR×{mid_stop_spread_atr_m:.2f} (ATR≈{float(atr_bps):.1f})")
                    else:
                        why.append(f"spread {sp_bps:.2f}bps wide")
                if thin_book:
                    why.append("thin_book")
                reason = f"MID_STOP raw {pnl_bps:.1f}≤-{mid_stop_loss_raw:.1f} bps age={age:.0f}s (" + ", ".join(why) + ")"
        else:
            if hold_hard > 0 and age >= hold_hard:
                reason = f"MAX_HOLD_HARD {age:.0f}s≥{hold_hard:.0f}s"
            elif hold > 0 and age >= hold:
                # Общий потолок по возрасту для любых "деферов" MAX_HOLD (чтобы позиция не висела бесконечно).
                stale_cap = float(getattr(self._s, "auto_trade_max_hold_book_tp_stale_seconds", 0.0) or 0.0)
                hold_skip_neg = float(getattr(self._s, "auto_trade_max_hold_skip_if_pnl_net_ge_neg_bps", 0.0) or 0.0)
                # Smart MAX_HOLD: если последний TA-сигнал свежий и в сторону позиции — не закрываем по таймеру,
                # пока net не ухудшился ниже порога, и пока рынок для удержания "нормальный" (не широкий спред/не тонкий стакан).
                smart_sig_age = float(
                    getattr(self._s, "auto_trade_max_hold_smart_signal_max_age_seconds", 0.0) or 0.0
                )
                smart_force_exit_net_le = float(
                    getattr(self._s, "auto_trade_max_hold_smart_force_exit_if_pnl_net_le_bps", 0.0) or 0.0
                )
                smart_defer_net_le = float(
                    getattr(self._s, "auto_trade_max_hold_smart_defer_if_pnl_net_le_bps", 0.0) or 0.0
                )
                smart_allow_no_sig = bool(
                    getattr(self._s, "auto_trade_max_hold_smart_allow_without_signal", False)
                )
                # Smart-defer держим только в узкой зоне около breakeven:
                # - сверху: net ≤ smart_defer_net_le (обычно небольшой плюс)
                # - снизу: net ≥ -hold_skip_neg (обычно небольшой минус от комиссий/шума)
                smart_defer_min_net_ge = (-hold_skip_neg) if hold_skip_neg > 0.0 else 0.0
                if (
                    smart_sig_age > 0
                    and (stale_cap <= 0.0 or age < stale_cap)
                    and (smart_defer_net_le <= 0.0 or pnl_net_bps <= smart_defer_net_le + 1e-9)
                    and pnl_net_bps + 1e-9 >= smart_defer_min_net_ge
                ):
                    last = self._last_sig.get((exchange_id, sym))
                    pos_side = "buy" if pos > 0 else "sell"
                    last_age = (now_mono - float(last[0])) if last is not None else float("inf")
                    has_fresh_sig = last is not None and last_age <= smart_sig_age + 1e-9
                    sig_supports = has_fresh_sig and last[1] == pos_side
                    sig_opposes = has_fresh_sig and last[1] != pos_side
                    allow_defer = (
                        (sig_supports or (smart_allow_no_sig and not has_fresh_sig))
                        and not sig_opposes
                        and (smart_force_exit_net_le == 0.0 or pnl_net_bps > smart_force_exit_net_le + 1e-9)
                    )
                    if allow_defer:
                        sp_bps = spread_bps(best_bid, best_ask)
                        wide_spread = False
                        if atr_bps is not None and atr_bps > 1e-9 and mid_stop_spread_atr_m > 0:
                            wide_spread = sp_bps > float(atr_bps) * mid_stop_spread_atr_m + 1e-9

                        thin_book = False
                        max_slip = float(self._s.scalping_max_slippage_bps)
                        amt = abs(float(pos))
                        if amt > 1e-12:
                            if pos > 0:
                                _q, vwap, _sold, complete = vwap_sell_base(bids, amt)
                                if not complete:
                                    thin_book = True
                                else:
                                    slip = simulated_slippage_bps_sell(best_bid, float(vwap), mid)
                                    thin_book = slip > max_slip + 1e-9
                            else:
                                _q, vwap, _bought, complete = vwap_buy_base(asks, amt)
                                if not complete:
                                    thin_book = True
                                else:
                                    slip = simulated_slippage_bps_buy(best_ask, float(vwap), mid)
                                    thin_book = slip > max_slip + 1e-9

                        if not wide_spread and not thin_book:
                            if self._exit_skip_log_ok(sym, now_mono):
                                side_tag = "long" if pos > 0 else "short"
                                sig_tag = (
                                    pos_side
                                    if sig_supports
                                    else ("no_signal" if (smart_allow_no_sig and not has_fresh_sig) else "stale")
                                )
                                self._log.info(
                                    "auto_trade: exit %s — smart MAX_HOLD defer (%s): свежий сигнал=%s age_sig≈%.0fs, "
                                    "pnl_net≈%.1fbps (raw≈%.1f fee≈%.1f) spread≈%.2fbps; ждём улучшения/инвалидации",
                                    sym,
                                    side_tag,
                                    sig_tag,
                                    (last_age if last is not None else float("inf")),
                                    pnl_net_bps,
                                    pnl_bps,
                                    fee_side_bps,
                                    sp_bps,
                                )
                                if sig_supports:
                                    self._ana_inc("exit_max_hold_defer_signal", exchange_id, sym)
                                else:
                                    self._ana_inc("exit_max_hold_defer_nosignal", exchange_id, sym)
                            return
                if (
                    hold_min_pnl > 0
                    and pnl_net_bps + 1e-9 < hold_min_pnl
                    and pnl_net_bps + 1e-9 >= 0.0
                ):
                    # MIN_PNL отсекает только «мелкий плюс» (ждём TP). Если net уже минус —
                    # не держим позицию до SL из‑за порога: закрываем по мягкому MAX_HOLD.
                    if self._exit_skip_log_ok(sym, now_mono):
                        side_tag = "long" if pos > 0 else "short"
                        self._log.info(
                            "auto_trade: exit %s — пропуск MAX_HOLD (%s): pnl_net≈%.1fbps (raw≈%.1f fee≈%.1f) < min=%.1f bps",
                            sym,
                            side_tag,
                            pnl_net_bps,
                            pnl_bps,
                            fee_side_bps,
                            hold_min_pnl,
                        )
                        # Важно: считаем события пропуска только когда лог прошёл троттлинг,
                        # иначе на WS будет десятки тысяч инкрементов/сессию и отчёт потеряет смысл.
                        self._ana_inc("exit_max_hold_min_pnl_skip", exchange_id, sym)
                elif (
                    hold_skip_neg > 0.0
                    and pnl_net_bps + 1e-9 < 0.0
                    and pnl_net_bps + 1e-9 >= -hold_skip_neg
                    and (stale_cap <= 0.0 or age < stale_cap)
                ):
                    if self._exit_skip_log_ok(sym, now_mono):
                        side_tag = "long" if pos > 0 else "short"
                        self._log.info(
                            "auto_trade: exit %s — пропуск MAX_HOLD (%s): pnl_net≈%.1fbps (raw≈%.1f fee≈%.1f) >= -%.1f bps",
                            sym,
                            side_tag,
                            pnl_net_bps,
                            pnl_bps,
                            fee_side_bps,
                            hold_skip_neg,
                        )
                        self._ana_inc("exit_max_hold_small_loss_skip", exchange_id, sym)
                else:
                    gate = bool(getattr(self._s, "auto_trade_max_hold_book_tp_gate", False))
                    if (
                        gate
                        and tp > 0
                        and isinstance(order_book, dict)
                        and order_book
                        and (stale_cap <= 0.0 or age < stale_cap)
                    ):
                        if self._book_full_exit_net_ge_tp(
                            pos, float(entry), abs(pos), order_book, tp, fee_side_bps
                        ):
                            if self._exit_skip_log_ok(sym, now_mono):
                                self._log.info(
                                    "auto_trade: exit %s — пропуск MAX_HOLD: по стакану полный выход по VWAP "
                                    "ещё даёт net≥%.1f bps (TP); ждём исполнение/исчерпание глубины. age≈%.0fs",
                                    sym,
                                    tp,
                                    age,
                                )
                            self._ana_inc("exit_max_hold_defer_book_tp", exchange_id, sym)
                            return
                    reason = f"MAX_HOLD {age:.0f}s≥{hold:.0f}s"

        if reason is None:
            # Это нормальный кейс: условия TP/SL/hold не достигнуты.
            return

        # Если exit уже в рынке (open/partial), не дублируем выход.
        if self._has_pending_exit_for_leg(exchange_id, sym):
            return

        # Pending-ордера по symbol.
        # - TP: обычно пропускаем (иначе "закрытия" могут мешать незавершённому входу и давать шум)
        # - SL/MAX_HOLD: разрешаем выход и пытаемся снять pending-ордера по symbol заранее
        has_pending = self._has_pending_for_symbol(sym)
        if has_pending and reason.startswith("TP") and not bool(self._s.auto_trade_tp_allow_with_pending):
            if self._exit_skip_log_ok(sym, now_mono):
                self._log.info("auto_trade: exit %s — пропуск TP: есть pending-ордера по символу", sym)
                self._ana_inc("exit_tp_blocked_by_pending", exchange_id, sym)
            return
        if has_pending and reason.startswith("TP") and bool(self._s.auto_trade_tp_allow_with_pending):
            if self._exit_skip_log_ok(sym, now_mono):
                self._log.info("auto_trade: exit %s — TP разрешён при pending (AUTO_TRADE_TP_ALLOW_WITH_PENDING=true)", sym)
        if has_pending and (reason.startswith("SL") or reason.startswith("MAX_HOLD")):
            canceled = await self._cancel_pending_symbol(sym)
            if canceled and self._exit_skip_log_ok(sym, now_mono):
                self._log.info("auto_trade: exit %s — снято pending=%s перед выходом (%s)", sym, canceled, reason)

        if exit_price <= 0:
            if self._exit_skip_log_ok(symbol, now_mono):
                self._log.info("auto_trade: exit %s — пропуск: exit_price<=0", symbol)
            self._ana_inc("exit_price_invalid", exchange_id, symbol)
            return

        amount = abs(pos)
        if self._exit_skip_log_ok(sym, now_mono, throttle_s=15.0):
            self._log.info(
                "auto_trade: exit %s %s %s pos≈%.8f entry≈%.8g mid≈%.8g pnl≈%.1fbps → %s @ %.8g (%s)",
                sym,
                exchange_id,
                "long" if pos > 0 else "short",
                pos,
                entry,
                mid,
                pnl_bps,
                exit_side,
                exit_price,
                reason,
            )
        order = await self._ex.place_limit(
            exchange_id,
            sym,
            exit_side,
            amount,
            exit_price,
            order_book=order_book,
            risk_priority="exit",
        )
        if not order:
            # Backoff: при отказе — пауза перед следующей попыткой (иначе будет сотни попыток/сек на WS).
            self._exit_reject_until_mono[rej_key] = now_mono + 15.0
            if self._exit_skip_log_ok(symbol, now_mono, throttle_s=15.0):
                self._log.info(
                    "auto_trade: exit %s — не выставлен (отказ риск/капитал/ликвидность). side=%s amount≈%.8f price≈%.8g",
                    sym,
                    exit_side,
                    amount,
                    exit_price,
                )
                self._ana_inc("exit_place_limit_rejected", exchange_id, sym)
            return
        self._register_order(exchange_id, sym, order, kind="exit")
        # Anti-churn: после постановки выхода запрещаем повторный вход в исходную сторону позиции.
        blocked_entry_side = "buy" if pos > 0 else "sell"
        self._mark_exit_side(exchange_id, sym, blocked_entry_side, now_mono)
        self._mark(exchange_id, sym)

    def note_quote_for_vol_scale(self, exchange_id: str, q: Quote) -> None:
        """Обновить окно изменений mid (bps): vol-scale, калибровка rest/ws, всплеск σ → пауза."""
        need_vol = self._s.auto_trade_vol_scale_enabled
        need_spike = self._s.scalping_sigma_spike_cooldown_seconds > 0
        if not need_vol and not need_spike:
            return
        key = (exchange_id, q.symbol)
        mid = (q.bid + q.ask) / 2.0
        if mid <= 0:
            return
        w = max(3, self._s.scalping_vol_window)
        if key not in self._vol_scale_state:
            self._vol_scale_state[key] = {"last_mid": None, "dq": deque(maxlen=w)}
        st = self._vol_scale_state[key]
        if st["dq"].maxlen != w:
            st["dq"] = deque(st["dq"], maxlen=w)
        last = st["last_mid"]
        if last is None:
            st["last_mid"] = mid
            return
        ch = (mid / last - 1.0) * 10_000.0
        st["last_mid"] = mid
        st["dq"].append(ch)
        if need_spike:
            self._maybe_record_sigma_spike(exchange_id, q.symbol)

    def _sigma_ticks_bps(self, exchange_id: str, symbol: str) -> float | None:
        st = self._vol_scale_state.get((exchange_id, symbol))
        if not st:
            return None
        dq = st.get("dq")
        if not dq or len(dq) < 2:
            return None
        return statistics.pstdev(dq)

    def _effective_sigma_ticks_bps(self, exchange_id: str, symbol: str) -> float | None:
        raw = self._sigma_ticks_bps(exchange_id, symbol)
        if raw is None:
            return None
        dm = self._s.data_mode.strip().lower()
        cal = (
            self._s.scalping_sigma_calib_rest
            if dm == "rest"
            else self._s.scalping_sigma_calib_ws
        )
        return max(0.0, float(raw) * float(cal))

    def _maybe_record_sigma_spike(self, exchange_id: str, symbol: str) -> None:
        cd = float(self._s.scalping_sigma_spike_cooldown_seconds)
        if cd <= 0:
            return
        sig = self._effective_sigma_ticks_bps(exchange_id, symbol)
        if sig is None:
            return
        key = (exchange_id, symbol)
        prev = self._prev_eff_sigma.get(key)
        self._prev_eff_sigma[key] = sig
        abs_th = float(self._s.scalping_sigma_spike_abs_bps)
        ratio = float(self._s.scalping_sigma_spike_ratio)
        spike = False
        if abs_th > 0.0 and sig >= abs_th:
            spike = True
        if ratio > 0.0 and prev is not None and prev > 1e-9 and sig >= prev * ratio:
            spike = True
        if spike:
            self._sigma_spike_until_mono[key] = time.monotonic() + cd
            self._log.info(
                "auto_trade: всплеск σ≈%.2f bps (eff, после калибровки %s) → пауза %.0fs",
                sig,
                self._s.data_mode,
                cd,
            )

    def _sigma_spike_blocks(self, exchange_id: str, symbol: str) -> bool:
        key = (exchange_id, symbol)
        until = self._sigma_spike_until_mono.get(key)
        if until is None:
            return False
        now = time.monotonic()
        if now >= until:
            self._sigma_spike_until_mono.pop(key, None)
            return False
        return True

    def _notional_vol_scaled(self, exchange_id: str, symbol: str, base_notional: float) -> tuple[float, float | None]:
        """Базовый номинал × множитель; при отключении или без σ — base и None. σ — эффективная (калибровка rest/ws)."""
        if not self._s.auto_trade_vol_scale_enabled:
            return base_notional, None
        sig = self._effective_sigma_ticks_bps(exchange_id, symbol)
        if sig is None:
            return base_notional, None
        ref = float(self._s.scalping_auto_trade_vol_ref_bps)
        cap = float(self._s.scalping_auto_trade_vol_cap_bps)
        mn = float(self._s.scalping_auto_trade_vol_min_mult)
        if sig <= ref:
            return base_notional, sig
        if sig >= cap:
            return max(base_notional * mn, 1e-12), sig
        t = (sig - ref) / (cap - ref)
        mult = 1.0 - t * (1.0 - mn)
        return max(base_notional * mult, 1e-12), sig

    @staticmethod
    def _live_order_terminal(status: str, remaining: float) -> bool:
        if remaining <= 1e-18:
            return True
        s = status.lower()
        return s in ("closed", "canceled", "cancelled", "expired", "rejected")

    def _pop_live_meta(self, order_id: str) -> None:
        self._live_meta.pop(order_id, None)

    @staticmethod
    def _live_ttl_expired(meta: dict[str, Any], now_mono: float, ttl_sec: float) -> bool:
        """TTL: если есть created_wall (unix time) — от момента создания на бирже; иначе от monotonic сессии."""
        if ttl_sec <= 0:
            return False
        cw = meta.get("created_wall")
        if cw is not None:
            return time.time() - float(cw) >= ttl_sec
        created = float(meta.get("created_mono", now_mono))
        return now_mono - created >= ttl_sec

    def _order_id_known(self, order_id: str) -> bool:
        for ids in self._pending_order_ids.values():
            if order_id in ids:
                return True
        return False

    def symbols_with_pending_orders(self, exchange_id: str) -> set[str]:
        """Все пары, по которым есть непустой список активных заявок (paper и live)."""
        out: set[str] = set()
        for (ex, sym), ids in self._pending_order_ids.items():
            if ex == exchange_id and ids:
                out.add(sym)
        return out

    def _ingest_open_order_rows(
        self,
        exchange_id: str,
        orders: list[dict[str, Any]],
        seen: set[str],
        *,
        skip_existing: bool,
        now_mono: float,
        now_wall: float,
    ) -> int:
        count = 0
        for o in orders:
            ot = str(o.get("type") or "")
            if ot and ot != "limit":
                continue
            if self._live_order_terminal(str(o.get("status") or "open"), float(o.get("remaining") or 0.0)):
                continue
            remaining = float(o.get("remaining") or 0.0)
            if remaining <= 1e-18:
                continue
            oid = str(o.get("id") or "")
            if not oid or oid in seen:
                continue
            if skip_existing and self._order_id_known(oid):
                continue
            seen.add(oid)
            osym = str(o.get("symbol") or "")
            if not osym:
                continue
            price = float(o.get("price") or 0.0)
            if price <= 0:
                price = float(o.get("average") or 0.0)
            side = str(o.get("side") or "").lower()
            ts_ms = o.get("timestamp")
            if ts_ms is not None:
                created_wall = float(ts_ms) / 1000.0
            else:
                created_wall = now_wall
            key = (exchange_id, osym)
            self._pending_order_ids.setdefault(key, []).append(oid)
            self._pending_kind[oid] = "entry"
            self._live_meta[oid] = {
                "created_mono": now_mono,
                "last_reprice_mono": now_mono,
                "created_wall": created_wall,
                "side": side,
                "price": price,
                "symbol": osym,
                "exchange_id": exchange_id,
            }
            self._ex.update_open_notional_for_order(oid, remaining, price)
            count += 1
            self._log.info(
                "auto_trade: учтён ордер с биржи %s %s id=%s %s remaining=%s price=%s",
                osym,
                exchange_id,
                oid,
                side,
                remaining,
                price,
            )
        return count

    async def restore_live_orders_from_exchange(
        self, exchange_id: str, fallback_symbols: tuple[str, ...] = ()
    ) -> int:
        """Подтянуть все открытые лимитки (symbol=None); при ошибке — по списку fallback_symbols."""
        if not self._s.auto_trade or self._s.paper:
            return 0
        seen: set[str] = set()
        now_mono = time.monotonic()
        now_wall = time.time()
        orders: list[dict[str, Any]] = []
        try:
            orders = await self._ex.fetch_open_orders(exchange_id, None)
        except Exception as e:
            self._log.warning(
                "fetch_open_orders(все пары): %s — повтор по символам из настроек",
                e,
            )
            for sym in fallback_symbols:
                try:
                    orders.extend(await self._ex.fetch_open_orders(exchange_id, sym))
                except Exception as e2:
                    self._log.warning("fetch_open_orders %s: %s", sym, e2)
        count = self._ingest_open_order_rows(
            exchange_id, orders, seen, skip_existing=False, now_mono=now_mono, now_wall=now_wall
        )
        if count:
            self._log.info("auto_trade: всего восстановлено ордеров: %s", count)
        return count

    async def merge_open_orders_from_exchange(self, exchange_id: str) -> int:
        """Добавить в учёт новые лимитки с биржи (не перезапуск), без дубликатов id."""
        if not self._s.auto_trade or self._s.paper:
            return 0
        try:
            orders = await self._ex.fetch_open_orders(exchange_id, None)
        except Exception as e:
            self._log.warning("merge_open_orders: %s", e)
            return 0
        seen: set[str] = set()
        now_mono = time.monotonic()
        now_wall = time.time()
        return self._ingest_open_order_rows(
            exchange_id,
            orders,
            seen,
            skip_existing=True,
            now_mono=now_mono,
            now_wall=now_wall,
        )

    def _cooldown_key(self, exchange_id: str, symbol: str) -> tuple[str, str]:
        if self._s.auto_trade_cooldown_scope == "symbol":
            return ("*", symbol)
        return (exchange_id, symbol)

    def _cooldown_ok(self, exchange_id: str, symbol: str) -> bool:
        cd = self._s.auto_trade_cooldown_seconds
        if cd <= 0:
            return True
        key = self._cooldown_key(exchange_id, symbol)
        now = time.monotonic()
        prev = self._last_mono.get(key, 0.0)
        if now - prev < cd:
            return False
        return True

    def _mark(self, exchange_id: str, symbol: str) -> None:
        self._last_mono[self._cooldown_key(exchange_id, symbol)] = time.monotonic()

    def _reentry_cd_seconds(self, symbol: str) -> float:
        cd = float(getattr(self._s, "auto_trade_reentry_cooldown_seconds", 0.0) or 0.0)
        if symbol.upper().startswith("SOL/"):
            cd_sol = float(getattr(self._s, "auto_trade_reentry_cooldown_seconds_sol", 0.0) or 0.0)
            if cd_sol > 0:
                return cd_sol
        return cd

    def _reentry_ok(self, exchange_id: str, symbol: str, side: str, now_mono: float) -> bool:
        cd = self._reentry_cd_seconds(symbol)
        if cd <= 0:
            return True
        k = (*self._cooldown_key(exchange_id, symbol), side)
        prev = float(self._last_exit_side_mono.get(k, 0.0) or 0.0)
        return (now_mono - prev) >= cd

    def _mark_exit_side(self, exchange_id: str, symbol: str, blocked_entry_side: str, now_mono: float) -> None:
        k = (*self._cooldown_key(exchange_id, symbol), blocked_entry_side)
        self._last_exit_side_mono[k] = now_mono

    async def _cancel_pending(self, exchange_id: str, symbol: str) -> None:
        key = (exchange_id, symbol)
        ids = self._pending_order_ids.pop(key, [])
        for oid in ids:
            ok = await self._ex.cancel_order(exchange_id, symbol, oid)
            self._pending_kind.pop(oid, None)
            if ok and not self._s.paper:
                self._pop_live_meta(oid)

    async def on_tick(self, exchange_id: str, symbol: str, order_book: dict | None) -> None:
        """Paper: доливка по стакану, TTL, reprice. Live: fetch_order → остаток/риск, TTL, reprice."""
        if not self._s.auto_trade:
            return

        if self._s.paper:
            if not order_book:
                return
            updated = self._ex.paper_apply_order_book(exchange_id, symbol, order_book)

            bids = order_book.get("bids") or []
            asks = order_book.get("asks") or []
            if not bids or not asks:
                return
            best_bid = float(bids[0][0])
            best_ask = float(asks[0][0])

            now = time.monotonic()
            mid = (best_bid + best_ask) / 2.0 if (best_bid > 0 and best_ask > 0) else 0.0
            self._record_mid(exchange_id, symbol, mid, now)
            await self._maybe_exit_position_paper(
                exchange_id,
                symbol,
                best_bid=best_bid,
                best_ask=best_ask,
                order_book=order_book,
                now_mono=now,
            )
            # Минимальный "exit сразу после fill": если в этом тике был fill и ордер закрылся,
            # позиция/entry уже обновлены в PaperSessionPnl → повторно проверим выход немедленно.
            if any(str(o.get("status", "")).lower() == "closed" for o in (updated or [])):
                await self._maybe_exit_position_paper(
                    exchange_id,
                    symbol,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    order_book=order_book,
                    now_mono=now,
                )

            key = (exchange_id, symbol)
            ids = self._pending_order_ids.get(key, [])
            if not ids:
                return
            ttl = float(self._s.scalping_order_ttl_seconds)
            reprice_bps = float(self._s.scalping_reprice_bps)
            reprice_cd = float(self._s.scalping_reprice_cooldown_seconds)

            alive: list[str] = []
            for oid in ids:
                o = self._ex.get_paper_order(oid)
                if not o:
                    self._pending_kind.pop(oid, None)
                    continue
                status = str(o.get("status", "open"))
                remaining = float(o.get("remaining", 0.0))
                if status not in ("open", "partial") or remaining <= 1e-18:
                    self._pending_kind.pop(oid, None)
                    continue

                created = float(o.get("created_mono", now))
                last_rep = float(o.get("last_reprice_mono", created))

                if ttl > 0 and now - created >= ttl:
                    self._log.info("auto_trade: TTL cancel %s %s id=%s", symbol, exchange_id, oid)
                    await self._ex.cancel_order(exchange_id, symbol, oid)
                    continue

                if reprice_bps > 0 and now - last_rep >= reprice_cd:
                    side = str(o.get("side", ""))
                    limit_price = float(o.get("price", 0.0))
                    desired = best_bid if side == "buy" else best_ask
                    if limit_price > 0 and desired > 0:
                        dist = abs(desired / limit_price - 1.0) * 10_000.0
                        if dist >= reprice_bps and spread_bps(best_bid, best_ask) < 10_000:
                            self._log.info(
                                "auto_trade: reprice %s %s %s %.8g→%.8g (%.1f bps) id=%s",
                                symbol,
                                exchange_id,
                                side,
                                limit_price,
                                desired,
                                dist,
                                oid,
                            )
                            await self._ex.cancel_order(exchange_id, symbol, oid)
                            new_order = await self._ex.place_limit(
                                exchange_id,
                                symbol,
                                side,
                                remaining,
                                desired,
                                order_book=order_book,
                            )
                            if new_order:
                                new_id = str(new_order.get("id", ""))
                                if new_id:
                                    po = self._ex.get_paper_order(new_id)
                                    if po is not None:
                                        po["last_reprice_mono"] = now
                                    alive.append(new_id)
                            continue

                alive.append(oid)

            self._pending_order_ids[key] = alive
            return

        # --- live: fetch_order, TTL, reprice ---
        key = (exchange_id, symbol)
        ids = list(self._pending_order_ids.get(key, []))
        if not ids:
            return

        ob = order_book or {}
        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
        best_bid = float(bids[0][0]) if bids else 0.0
        best_ask = float(asks[0][0]) if asks else 0.0
        has_book = bool(bids and asks)

        now = time.monotonic()
        if has_book:
            mid = (best_bid + best_ask) / 2.0 if (best_bid > 0 and best_ask > 0) else 0.0
            self._record_mid(exchange_id, symbol, mid, now)
        ttl = float(self._s.scalping_order_ttl_seconds)
        reprice_bps = float(self._s.scalping_reprice_bps)
        reprice_cd = float(self._s.scalping_reprice_cooldown_seconds)

        alive: list[str] = []
        for oid in ids:
            fo = await self._ex.fetch_order(exchange_id, symbol, oid)
            if fo is None:
                alive.append(oid)
                continue

            status = str(fo.get("status") or "open")
            remaining = float(fo.get("remaining") or 0.0)
            price = float(fo.get("price") or 0.0)
            side = str(fo.get("side") or "")
            meta = self._live_meta.get(oid)
            if not side and meta:
                side = str(meta.get("side", ""))
            if price <= 0 and meta:
                price = float(meta.get("price", 0.0))
            if price <= 0:
                price = float(fo.get("average") or 0.0)

            if self._live_order_terminal(status, remaining):
                self._ex.update_open_notional_for_order(oid, 0.0, price)
                self._pop_live_meta(oid)
                self._log.info(
                    "auto_trade live: ордер снят/исполнен %s %s id=%s status=%s filled≈%s remaining=%s",
                    symbol,
                    exchange_id,
                    oid,
                    status,
                    fo.get("filled"),
                    remaining,
                )
                continue

            self._ex.update_open_notional_for_order(oid, remaining, price)

            if not meta:
                wall = time.time()
                meta = {
                    "created_mono": float(now),
                    "last_reprice_mono": float(now),
                    "created_wall": wall,
                    "side": side,
                    "price": price,
                    "symbol": symbol,
                    "exchange_id": exchange_id,
                }
                self._live_meta[oid] = meta

            created = float(meta.get("created_mono", now))
            last_rep = float(meta.get("last_reprice_mono", created))

            if self._live_ttl_expired(meta, now, ttl):
                self._log.info("auto_trade: TTL cancel (live) %s %s id=%s", symbol, exchange_id, oid)
                ok = await self._ex.cancel_order(exchange_id, symbol, oid)
                if ok:
                    self._pop_live_meta(oid)
                else:
                    alive.append(oid)
                continue

            if (
                has_book
                and reprice_bps > 0
                and now - last_rep >= reprice_cd
                and side in ("buy", "sell")
            ):
                limit_price = float(meta.get("price", price))
                desired = best_bid if side == "buy" else best_ask
                if limit_price > 0 and desired > 0:
                    dist = abs(desired / limit_price - 1.0) * 10_000.0
                    if dist >= reprice_bps and spread_bps(best_bid, best_ask) < 10_000:
                        self._log.info(
                            "auto_trade: reprice (live) %s %s %s %.8g→%.8g (%.1f bps) id=%s",
                            symbol,
                            exchange_id,
                            side,
                            limit_price,
                            desired,
                            dist,
                            oid,
                        )
                        ok = await self._ex.cancel_order(exchange_id, symbol, oid)
                        if not ok:
                            alive.append(oid)
                            continue
                        self._pop_live_meta(oid)
                        new_order = await self._ex.place_limit(
                            exchange_id,
                            symbol,
                            side,
                            remaining,
                            desired,
                            order_book=order_book,
                        )
                        if new_order:
                            new_id = str(new_order.get("id", ""))
                            if new_id:
                                np = float(new_order.get("price") or desired)
                                self._live_meta[new_id] = {
                                    "created_mono": now,
                                    "last_reprice_mono": now,
                                    "created_wall": time.time(),
                                    "side": side,
                                    "price": np,
                                    "symbol": symbol,
                                    "exchange_id": exchange_id,
                                }
                                alive.append(new_id)
                        continue

            alive.append(oid)

        self._pending_order_ids[key] = alive

    def _register_order(self, exchange_id: str, symbol: str, order: dict, *, kind: str = "entry") -> None:
        if order.get("status") == "closed":
            return
        rem = float(order.get("remaining", order.get("amount", 0)))
        if rem <= 1e-18:
            return
        oid = str(order.get("id", ""))
        if not oid:
            return
        key = (exchange_id, symbol)
        self._pending_order_ids.setdefault(key, []).append(oid)
        self._pending_kind[oid] = "exit" if str(kind) == "exit" else "entry"
        if not self._s.paper:
            now = time.monotonic()
            self._live_meta[oid] = {
                "created_mono": now,
                "last_reprice_mono": now,
                "created_wall": time.time(),
                "side": str(order.get("side", "")).lower(),
                "price": float(order.get("price") or 0.0),
                "symbol": symbol,
                "exchange_id": exchange_id,
            }

    async def on_signal(
        self,
        exchange_id: str,
        q: Quote,
        sig: ScalpSignal,
        order_book: dict | None = None,
    ) -> None:
        if not self._s.auto_trade:
            return
        if self._s.auto_trade_cooldown_scope == "symbol":
            lk = self._symbol_scope_locks.setdefault(q.symbol, asyncio.Lock())
            async with lk:
                await self._on_signal_impl(exchange_id, q, sig, order_book)
            return
        await self._on_signal_impl(exchange_id, q, sig, order_book)

    async def _on_signal_impl(
        self,
        exchange_id: str,
        q: Quote,
        sig: ScalpSignal,
        order_book: dict | None = None,
    ) -> None:
        now_mono = time.monotonic()
        # Smart MAX_HOLD: запоминаем "последний сигнал" даже если вход будет заблокирован кулдауном/guards —
        # это даёт выходу (MAX_HOLD) контекст направления.
        self._last_sig[(exchange_id, q.symbol)] = (now_mono, sig.side, getattr(sig, "impulse_bps", None))

        if self._sigma_spike_blocks(exchange_id, q.symbol):
            self._ana_inc("sigma_spike_cooldown", exchange_id, q.symbol)
            self._log.debug("auto_trade: пауза после всплеска σ %s %s", exchange_id, q.symbol)
            return
        if not self._cooldown_ok(exchange_id, q.symbol):
            self._ana_inc("cooldown", exchange_id, q.symbol)
            self._log.debug("auto_trade: кулдаун %s %s", exchange_id, q.symbol)
            return

        # Anti-churn: после выхода по символу не заходим повторно в ту же сторону.
        if not self._reentry_ok(exchange_id, q.symbol, sig.side, now_mono):
            self._ana_inc("reentry_cooldown", exchange_id, q.symbol)
            self._log.debug("auto_trade: reentry_cooldown %s %s %s", exchange_id, q.symbol, sig.side)
            return

        # Quiet-market guard: если рынок «тихий» (диапазон mid за окно слишком мал), не открываем новые входы.
        #
        # AUTO_TUNE: если включено, порог рассчитывается автоматически от fee/TP (и может работать даже при
        # SCALPING_MIN_MID_RANGE_BPS=0), чтобы не входить в режим «TP почти недостижим → MAX_HOLD/SL».
        thr_range_user = float(getattr(self._s, "scalping_min_mid_range_bps", 0.0) or 0.0)
        auto_tune = bool(getattr(self._s, "auto_trade_auto_tune", False))
        thr_range = thr_range_user
        if auto_tune:
            fee_side_bps = float(self._s.paper_trading_fee_bps() if self._s.paper else self._s.fee_bps_per_side)
            tp_net = float(self._s.auto_trade_tp_bps)
            rt_fee = 2.0 * fee_side_bps
            # Короткое окно range(mid) измеряет микродвижение, а не «дойдёт ли цена до TP» за время сделки
            # (особенно при ta_trend + длинном MAX_HOLD). Поэтому база — взвешенная доля round-trip fee и TP_net,
            # а не max(2fee, tp_net+fee) целиком (это давало thr≈15 bps при TP=8 и fee=6 и резало типичный BTC).
            rt_frac = float(getattr(self._s, "auto_trade_auto_tune_rt_fee_frac", 0.42) or 0.42)
            tp_frac = float(getattr(self._s, "auto_trade_auto_tune_tp_net_frac", 0.30) or 0.30)
            stiff_base = rt_frac * rt_fee + (tp_frac * max(0.0, tp_net) if tp_net > 0 else 0.0)
            mult = float(getattr(self._s, "auto_trade_auto_tune_mid_range_mult", 1.25) or 1.25)
            extra = float(getattr(self._s, "auto_trade_auto_tune_mid_range_extra_bps", 2.0) or 2.0)
            min_rng = float(getattr(self._s, "auto_trade_auto_tune_min_mid_range_bps", 0.0) or 0.0)
            rng_max = float(getattr(self._s, "auto_trade_auto_tune_range_max_bps", 0.0) or 0.0)
            thr_auto = max(min_rng, stiff_base * mult + extra)
            if rng_max > 0.0:
                thr_auto = min(thr_auto, rng_max)
            thr_range = max(thr_range, thr_auto)

        # -----------------------------------------------------------------
        # Volatility-adaptive entry strictness:
        # спокойный рынок → агрессивнее (ниже min_impulse и порог quiet-market),
        # волатильный → осторожнее (выше min_impulse и порог quiet-market, строже ATR×spread).
        #
        # σ_eff (tick σ) берём из vol-scale окна; ATR_bps — из TA-движка (если подключён).
        # -----------------------------------------------------------------
        eff_sig_bps = self._effective_sigma_ticks_bps(exchange_id, q.symbol)
        atr_bps = self._atr_bps_provider(exchange_id, q.symbol) if self._atr_bps_provider else None
        vol_ref = float(self._s.scalping_auto_trade_vol_ref_bps)
        vol_cap = float(self._s.scalping_auto_trade_vol_cap_bps)
        atr_ref = float(self._s.auto_trade_atr_notional_ref_bps)

        # Базовые "effective" значения, которые могут быть скорректированы режимом.
        eff_min_imp = float(self._s.scalping_auto_trade_min_impulse_bps)
        eff_thr_range = float(thr_range)
        eff_atr_spread_mult = float(self._s.auto_trade_atr_max_spread_mult)

        is_calm = False
        is_volatile = False
        if eff_sig_bps is not None:
            is_calm = eff_sig_bps <= vol_ref + 1e-9
            is_volatile = eff_sig_bps >= vol_cap - 1e-9
        if atr_ref > 0 and atr_bps is not None and atr_bps > 1e-9:
            is_calm = is_calm or (atr_bps <= atr_ref * 0.85)
            is_volatile = is_volatile or (atr_bps >= atr_ref * 2.0)

        if is_volatile and not is_calm:
            eff_min_imp *= 1.35
            eff_thr_range *= 1.25
            eff_atr_spread_mult *= 0.85
            self._ana_inc("vol_regime_volatile", exchange_id, q.symbol)
        elif is_calm and not is_volatile:
            # Calm → чуть агрессивнее: иначе даже trend-сигналы 8–12bps часто не проходят.
            eff_min_imp *= 0.70
            eff_thr_range *= 0.85
            eff_atr_spread_mult *= 1.10
            self._ana_inc("vol_regime_calm", exchange_id, q.symbol)

        if eff_thr_range > 0:
            now = time.monotonic()
            mid_now = (q.bid + q.ask) / 2.0
            rng = self._mid_range_bps(exchange_id, q.symbol, mid_now, now)
            if rng is None:
                warm_key = (
                    "quiet_market_warmup_auto" if auto_tune and thr_range_user <= 0 else "quiet_market_warmup"
                )
                self._ana_inc(warm_key, exchange_id, q.symbol)
                if self._quiet_skip_log_ok(exchange_id, q.symbol, now):
                    win_s = float(getattr(self._s, "scalping_min_mid_range_window_seconds", 30.0) or 30.0)
                    need_s = win_s * 0.9
                    self._log.info(
                        "auto_trade: quiet-market warmup — окно mid ещё не «созрело» (нужно покрытие времени "
                        "≈%.0fs из %.0fs по тикам; без этого min/max по 1–2 котировкам дал бы ложный range). "
                        "Не ошибка. «Диапазон mid» в WS-пульсе — фиксированный интервал сброса, цифры могут "
                        "слегка расходиться. (%s %s)",
                        need_s,
                        win_s,
                        q.symbol,
                        exchange_id,
                    )
                return
            if rng + 1e-9 < eff_thr_range:
                flat_key = (
                    "quiet_market_flat_auto" if auto_tune and thr_range_user <= 0 else "quiet_market_flat"
                )
                self._ana_inc(flat_key, exchange_id, q.symbol)
                if self._quiet_skip_log_ok(exchange_id, q.symbol, now):
                    win_s = float(getattr(self._s, "scalping_min_mid_range_window_seconds", 30.0) or 30.0)
                    if auto_tune and thr_range_user <= 0:
                        self._log.info(
                            "auto_trade: auto-tune quiet-market — range(mid)≈%.1f bps за скользящие %.0fs < "
                            "thr≈%.1f (effective): вход не открываем. (%s %s)",
                            rng,
                            win_s,
                            eff_thr_range,
                            q.symbol,
                            exchange_id,
                        )
                    else:
                        self._log.info(
                            "auto_trade: quiet-market — range(mid)≈%.1f bps за скользящие %.0fs < "
                            "SCALPING_MIN_MID_RANGE_BPS=%.1f (effective): вход не открываем. (%s %s)",
                            rng,
                            win_s,
                            eff_thr_range,
                            q.symbol,
                            exchange_id,
                        )
                return

        if eff_min_imp > 0 and sig.impulse_bps is not None and sig.impulse_bps + 1e-9 < eff_min_imp:
            self._ana_inc("min_impulse", exchange_id, q.symbol)
            self._log.info(
                "auto_trade: пропуск scalp — |импульс|=%.2f bps < SCALPING_AUTO_TRADE_MIN_IMPULSE_BPS≈%.1f (effective) "
                "(на ws тики часто 1–5 bps; ориентир 2× комиссия ≈ %.0f bps круг)",
                sig.impulse_bps,
                eff_min_imp,
                2.0 * float(self._s.paper_trading_fee_bps() if self._s.paper else self._s.fee_bps_per_side),
            )
            return

        base = min(self._s.auto_trade_notional, self._s.risk_max_notional_per_order)
        if base <= 0:
            return

        notional, sig_bps = self._notional_vol_scaled(exchange_id, q.symbol, base)
        if sig_bps is not None and notional < base - 1e-9:
            self._log.info(
                "auto_trade: vol-scale σ_eff≈%.2f bps → notional≈%.2f (база %.2f)",
                sig_bps,
                notional,
                base,
            )

        # ATR_bps уже запрошен выше (для вол-режима); повторно не считаем.
        ref_atr = float(self._s.auto_trade_atr_notional_ref_bps)
        fl = float(self._s.auto_trade_atr_notional_floor_mult)
        if ref_atr > 0 and atr_bps is not None and atr_bps > 1e-9:
            m_atr = min(1.0, ref_atr / atr_bps)
            if fl > 0:
                m_atr = max(fl, m_atr)
            if m_atr < 1.0 - 1e-9:
                notional = max(notional * m_atr, 1e-12)
                self._log.info(
                    "auto_trade: ATR к номиналу: ATR≈%.1f bps, ref=%.1f → ×%.2f",
                    atr_bps,
                    ref_atr,
                    m_atr,
                )

        mult_sp = float(eff_atr_spread_mult)
        if mult_sp > 0 and atr_bps is not None and atr_bps > 1e-9:
            sp_bps = spread_bps(q.bid, q.ask)
            lim = atr_bps * mult_sp
            if sp_bps > lim + 1e-9:
                self._ana_inc("atr_spread_filter", exchange_id, q.symbol)
                self._log.info(
                    "auto_trade: спред %.1f bps > ATR×%.2f (лимит %.1f, ATR≈%.1f) — пропуск",
                    sp_bps,
                    mult_sp,
                    lim,
                    atr_bps,
                )
                return

        mid = (q.bid + q.ask) / 2.0
        self._record_mid(exchange_id, q.symbol, mid, time.monotonic())
        # Reduce-only (по смыслу): если по паре уже есть позиция — не наращиваем, а только уменьшаем её.
        if self._s.auto_trade_reduce_only:
            if self._s.auto_trade_reduce_only_scope == "symbol":
                pos = self._ex.paper_net_position_base_symbol(q.symbol)
            else:
                pos = self._ex.paper_net_position_base(exchange_id, q.symbol)
            if pos is not None:
                if abs(pos) > 1e-12 and not self._pos_is_dust(pos, mid):
                    need_side = "sell" if pos > 0 else "buy"
                    if sig.side != need_side:
                        self._ana_inc("reduce_only", exchange_id, q.symbol)
                        pos_src = "symbol" if self._s.auto_trade_reduce_only_scope == "symbol" else "leg"
                        self._log.info(
                            "auto_trade: reduce-only[%s/%s] %s %s: pos≈%.8f base → пропуск %s (нужно %s)",
                            self._s.auto_trade_reduce_only_scope,
                            pos_src,
                            q.symbol,
                            exchange_id,
                            pos,
                            sig.side,
                            need_side,
                        )
                        return

        # Paper: "риск-слоты" по позициям вместо single-open.
        # - max_open_positions: лимит одновременных позиций (не заявок).
        # - correlated_same_dir: не держать BTC и SOL в одну сторону одновременно (корреляция).
        if self._s.paper:
            nowm = time.monotonic()
            single_open = bool(getattr(self._s, "auto_trade_single_open_position", False))
            max_pos = int(getattr(self._s, "auto_trade_max_open_positions", 0) or 0)
            block_corr = bool(getattr(self._s, "auto_trade_block_correlated_same_dir", False))
            max_same_dir = int(getattr(self._s, "auto_trade_max_positions_same_direction", 0) or 0)

            want_dir = 1 if sig.side == "buy" else -1
            corr_group = {"BTC/USDT", "SOL/USDT"}

            non_dust: list[tuple[str, str, float]] = []
            for ex, sym, o_pos, o_entry in self._ex.paper_open_positions() or []:
                if abs(o_pos) <= 1e-12:
                    continue
                ref_mid = float(self._last_mid.get((str(ex), str(sym)), 0.0) or 0.0)
                if ref_mid <= 0 and o_entry and float(o_entry) > 0:
                    ref_mid = float(o_entry)
                if ref_mid > 0 and self._pos_is_dust(o_pos, ref_mid):
                    continue
                non_dust.append((str(ex), str(sym), float(o_pos)))

            # Back-compat: single-open = max 1 позиция на все пары.
            eff_max_pos = 1 if single_open else max_pos
            if eff_max_pos > 0:
                other_pos = [
                    (ex, sym, o_pos)
                    for ex, sym, o_pos in non_dust
                    if not (ex == exchange_id and sym == q.symbol)
                ]
                if len(other_pos) >= eff_max_pos:
                    self._ana_inc("risk_slots_full", exchange_id, q.symbol)
                    if self._entry_skip_log_ok("risk_slots_full", exchange_id, q.symbol, nowm):
                        self._log.info(
                            "auto_trade: risk-slots — уже есть %d позиций (лимит %d), новый вход %s %s пропускаем",
                            len(other_pos),
                            eff_max_pos,
                            q.symbol,
                            exchange_id,
                        )
                    return

            if max_same_dir > 0:
                same_dir = 0
                for ex, sym, o_pos in non_dust:
                    if ex == exchange_id and sym == q.symbol:
                        continue
                    have_dir = 1 if o_pos > 0 else -1
                    if have_dir == want_dir:
                        same_dir += 1
                if same_dir >= max_same_dir:
                    self._ana_inc("risk_same_dir_full", exchange_id, q.symbol)
                    # region agent log
                    _dbg_log(
                        "H1",
                        "auto_trade.py:_on_signal_impl:risk_same_dir_full",
                        "blocked by same-direction limit",
                        {
                            "symbol": str(q.symbol),
                            "exchange_id": str(exchange_id),
                            "sig_side": str(sig.side),
                            "want_dir": int(want_dir),
                            "same_dir": int(same_dir),
                            "limit": int(max_same_dir),
                            "open_non_dust": [(ex, sym, 1 if pos > 0 else -1) for ex, sym, pos in non_dust],
                        },
                    )
                    # endregion agent log
                    if self._entry_skip_log_ok("risk_same_dir_full", exchange_id, q.symbol, nowm):
                        self._log.info(
                            "auto_trade: risk-dir — уже есть %d позиций dir=%s (лимит %d), новый вход %s %s пропускаем",
                            same_dir,
                            "long" if want_dir > 0 else "short",
                            max_same_dir,
                            q.symbol,
                            exchange_id,
                        )
                    return

            if block_corr and q.symbol in corr_group:
                for ex, sym, o_pos in non_dust:
                    if ex == exchange_id and sym == q.symbol:
                        continue
                    if sym not in corr_group:
                        continue
                    have_dir = 1 if o_pos > 0 else -1
                    if have_dir == want_dir:
                        self._ana_inc("corr_same_dir_block", exchange_id, q.symbol)
                        if self._entry_skip_log_ok("corr_same_dir", exchange_id, q.symbol, nowm):
                            self._log.info(
                                "auto_trade: corr-block — уже есть %s %s dir=%s (pos≈%.8f), новый вход %s %s dir=%s пропускаем",
                                sym,
                                ex,
                                "long" if have_dir > 0 else "short",
                                o_pos,
                                q.symbol,
                                exchange_id,
                                "long" if want_dir > 0 else "short",
                            )
                        return

        if self._s.scalping_cancel_previous_orders:
            await self._cancel_pending(exchange_id, q.symbol)

        if order_book:
            bids = order_book.get("bids") or []
            asks = order_book.get("asks") or []
            max_slip = self._s.scalping_max_slippage_bps

            if sig.side == "buy":
                _base, vwap, _spent, complete = vwap_buy_with_quote(asks, notional)
                if not complete:
                    self._ana_inc("liquidity_not_enough", exchange_id, q.symbol)
                    self._log.warning(
                        "auto_trade: мало ликвидности в ask для %s (нужно ≈%.2f quote)",
                        q.symbol,
                        notional,
                    )
                    return
                slip = simulated_slippage_bps_buy(q.ask, vwap, mid)
                if slip > max_slip:
                    self._ana_inc("slippage_too_high", exchange_id, q.symbol)
                    self._log.warning(
                        "auto_trade: симуляция slippage %.1f bps > лимит %.1f (%s)",
                        slip,
                        max_slip,
                        q.symbol,
                    )
                    return
            else:
                price_ref = q.ask if q.ask > 0 else mid
                base_amt = notional / price_ref
                _quote, vwap, _sold, complete = vwap_sell_base(bids, base_amt)
                if not complete:
                    self._ana_inc("liquidity_not_enough", exchange_id, q.symbol)
                    self._log.warning(
                        "auto_trade: мало ликвидности в bid для %s (нужно ≈%.8f base)",
                        q.symbol,
                        base_amt,
                    )
                    return
                slip = simulated_slippage_bps_sell(q.bid, vwap, mid)
                if slip > max_slip:
                    self._ana_inc("slippage_too_high", exchange_id, q.symbol)
                    self._log.warning(
                        "auto_trade: симуляция slippage %.1f bps > лимит %.1f (%s)",
                        slip,
                        max_slip,
                        q.symbol,
                    )
                    return

        if sig.side == "buy":
            # Paper debug mode: для демонстрации полного цикла (fill → exit) можно
            # выставлять лимитку "через спред" (рыночно-исполняемая).
            if self._s.paper and bool(getattr(self._s, "auto_trade_paper_cross_spread", False)):
                price = q.ask
            else:
                price = q.bid
        else:
            if self._s.paper and bool(getattr(self._s, "auto_trade_paper_cross_spread", False)):
                price = q.bid
            else:
                price = q.ask
        if price <= 0:
            return

        amount = notional / price
        if self._s.auto_trade_reduce_only:
            if self._s.auto_trade_reduce_only_scope == "symbol":
                pos = self._ex.paper_net_position_base_symbol(q.symbol)
            else:
                pos = self._ex.paper_net_position_base(exchange_id, q.symbol)
            if pos is not None and abs(pos) > 1e-12 and not self._pos_is_dust(pos, mid):
                cap = abs(pos)
                if amount > cap + 1e-12:
                    pos_src = "symbol" if self._s.auto_trade_reduce_only_scope == "symbol" else "leg"
                    self._log.info(
                        "auto_trade: reduce-only[%s/%s] cap %s %s %s amount %.8f→%.8f (pos≈%.8f base)",
                        self._s.auto_trade_reduce_only_scope,
                        pos_src,
                        q.symbol,
                        exchange_id,
                        sig.side,
                        amount,
                        cap,
                        pos,
                    )
                    amount = cap
                if amount <= 1e-12:
                    return
        order = await self._ex.place_limit(
            exchange_id,
            q.symbol,
            sig.side,
            amount,
            price,
            order_book=order_book,
        )
        if order:
            self._register_order(exchange_id, q.symbol, order, kind="entry")
            self._mark(exchange_id, q.symbol)
            self._log.info(
                "auto_trade scalp: %s %s %s req_notional≈%.2f filled=%s remaining=%s status=%s",
                sig.side,
                q.symbol,
                exchange_id,
                amount * price,
                order.get("filled"),
                order.get("remaining"),
                order.get("status"),
            )


class ArbAutoTrader:
    """Две ноги: покупка на бирже с лучшим ask, продажа на бирже с лучшим bid (одинаковый объём в базе)."""

    def __init__(self, settings: Settings, executor: OrderExecutor, logger: logging.Logger) -> None:
        self._s = settings
        self._ex = executor
        self._log = logger
        self._last_mono: dict[str, float] = {}

    def _cooldown_ok(self, symbol: str) -> bool:
        cd = self._s.auto_trade_cooldown_seconds
        if cd <= 0:
            return True
        now = time.monotonic()
        if now - self._last_mono.get(symbol, 0.0) < cd:
            return False
        return True

    def _mark(self, symbol: str) -> None:
        self._last_mono[symbol] = time.monotonic()

    async def on_opportunity(self, opp: Opportunity) -> None:
        if not self._s.auto_trade or not self._s.auto_trade_arbitrage:
            return
        if opp.edge_after_fees_bps < self._s.auto_trade_min_edge_bps:
            return
        if not self._cooldown_ok(opp.symbol):
            self._log.debug("auto_trade arb: кулдаун %s", opp.symbol)
            return

        notional_quote = min(self._s.auto_trade_notional, self._s.risk_max_notional_per_order)
        den = opp.buy_at + opp.sell_at
        if notional_quote <= 0 or den <= 0:
            return

        amount_base = notional_quote / den
        if amount_base <= 0:
            return

        buy_o = await self._ex.place_limit(
            opp.buy_exchange,
            opp.symbol,
            "buy",
            amount_base,
            opp.buy_at,
        )
        if not buy_o:
            return
        sell_o = await self._ex.place_limit(
            opp.sell_exchange,
            opp.symbol,
            "sell",
            amount_base,
            opp.sell_at,
        )
        if sell_o:
            self._mark(opp.symbol)
            self._log.info(
                "auto_trade arb: %s buy@%s sell@%s amount_base≈%s edge=%.1f bps",
                opp.symbol,
                opp.buy_exchange,
                opp.sell_exchange,
                amount_base,
                opp.edge_after_fees_bps,
            )
        else:
            self._log.warning(
                "auto_trade arb: вторая нога не выставлена, проверьте риск и отмените первую при необходимости"
            )
