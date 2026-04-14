"""Поток котировок через CCXT Pro (WebSocket) и сводка по стакану."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import ccxt.pro as ccxtpro
from ccxt.base.errors import NetworkError

from auto_trade import ArbAutoTrader, ScalpAutoTrader
from protocols import ScalpingStrategy
from scalping import ScalpSignalLogDeduper
from scalping_orderflow import OrderflowTapeStore
from config import Settings
from execution import OrderExecutor
from orderbook import format_spread_bid_ask, quote_from_order_book, snapshot_from_order_book, spread_bps
from scanner import ArbitrageScanner, Quote, compute_best_arbitrage

log = logging.getLogger(__name__)

OnOrderBook = Callable[[Quote, dict], Awaitable[None]]

# Bybit spot WebSocket: иначе BadRequest «limit can be one of: [1,50,200,1000]»
_BYBIT_SPOT_WS_OB_LIMITS: tuple[int, ...] = (1, 50, 200, 1000)

def coerce_watch_order_book_limit(exchange_id: str, limit: int) -> int:
    """Подобрать допустимую глубину стакана для watch_order_book (spot)."""
    eid = (exchange_id or "").strip().lower()
    lim = int(limit)
    if eid == "okx":
        # CCXT Pro: только limit==50 → books50-l2-tbt (VIP + подпись). Публично: books (400), books5 (2–5), bbo (1).
        if lim == 50:
            log.info(
                "OKX WS: ORDERBOOK_LIMIT=50 → books50-l2-tbt (VIP); для публичного стакана подставляем 400. "
                "Чтобы не менять глубину: ORDERBOOK_LIMIT=5 (books5)."
            )
            return 400
        return lim
    if eid != "bybit":
        return lim
    for a in _BYBIT_SPOT_WS_OB_LIMITS:
        if a >= limit:
            return a
    return _BYBIT_SPOT_WS_OB_LIMITS[-1]


def make_pro_exchange(exchange_id: str) -> ccxtpro.Exchange:
    eid = (exchange_id or "").strip().lower()
    if not hasattr(ccxtpro, eid):
        raise ValueError(f"WS: биржа не найдена в ccxt.pro: {exchange_id}")
    cls = getattr(ccxtpro, eid)
    opts: dict[str, Any] = {"enableRateLimit": True, "options": {"defaultType": "spot"}}
    # Публичный стакан OKX по умолчанию (limit задаётся отдельно; 50 нельзя без VIP)
    if eid == "okx":
        opts["options"]["watchOrderBook"] = {"depth": "books"}
    return cls(opts)


async def _safe_close_exchange(ex: ccxtpro.Exchange) -> None:
    try:
        # На отмене задач (Ctrl+C) даём close() завершиться, иначе aiohttp-сессия может остаться незакрытой.
        await asyncio.shield(ex.close())
    except asyncio.CancelledError:
        return
    except Exception as e:
        log.warning("WS: exchange.close() — %s", e)


async def _gather_ws_watchers(watchers: list[asyncio.Task[Any]]) -> None:
    """Дождаться вотчеров; при остановке (Ctrl+C / cancel) отменить задачи и дождаться их finally (ccxt.pro close)."""
    try:
        await asyncio.gather(*watchers)
    finally:
        for t in watchers:
            if not t.done():
                t.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)


class LatestQuotes:
    """Последний Quote по (биржа, символ) для межбиржевого сравнения."""

    def __init__(self) -> None:
        self._q: dict[tuple[str, str], Quote] = {}
        self._lock = asyncio.Lock()

    async def update(self, quote: Quote) -> None:
        async with self._lock:
            self._q[(quote.exchange_id, quote.symbol)] = quote

    async def list_for_symbol(self, symbol: str, exchanges: tuple[str, ...]) -> list[Quote]:
        async with self._lock:
            out: list[Quote] = []
            for e in exchanges:
                k = (e, symbol)
                if k in self._q:
                    out.append(self._q[k])
            return out


async def run_watch_tasks(
    exchange_id: str,
    symbols: list[str],
    orderbook_limit: int,
    on_order_book: OnOrderBook,
    *extra_awaitables: Any,
    tape_store: OrderflowTapeStore | None = None,
) -> None:
    """Один процесс ccxt.pro: параллельный watch_order_book по символам.

    extra_awaitables — доп. корутины (например пульс), в одном gather с вотчерами,
    чтобы при остановке один finally закрыл exchange.
    tape_store — если задан, параллельно watch_trades и запись в буфер orderflow.
    """
    def _is_reconnectable_ws_error(e: BaseException) -> bool:
        msg = str(e)
        low = msg.lower()
        # OKX planned maintenance notice (ccxt.pro raises ExchangeError with JSON text)
        if "service upgrade" in low and "reconnect" in low:
            return True
        if '"code":"64008"' in msg or "code\":\"64008" in msg or " 64008" in msg or low.endswith("64008"):
            return True
        # Generic network/websocket hiccups
        if isinstance(e, NetworkError):
            return True
        return False

    ob_limit = coerce_watch_order_book_limit(exchange_id, int(orderbook_limit))
    if ob_limit != orderbook_limit:
        log.info(
            "WS %s: ORDERBOOK_LIMIT=%s для spot order book скорректирован → %s",
            exchange_id,
            orderbook_limit,
            ob_limit,
        )

    backoff_s = 1.0
    while True:
        ex = make_pro_exchange(exchange_id)
        try:
            await ex.load_markets()
            for s in symbols:
                if s not in ex.symbols:
                    log.warning("%s: нет рынка %s", exchange_id, s)

            async def one(sym: str) -> None:
                while True:
                    # Дублируем защиту OKX: в CCXT limit=50 → VIP-канал даже без срабатывания coerce выше
                    lim = int(ob_limit)
                    if (exchange_id or "").strip().lower() == "okx" and lim == 50:
                        lim = 400
                    ob = await ex.watch_order_book(sym, lim)
                    q = quote_from_order_book(exchange_id, sym, ob)
                    if q:
                        await on_order_book(q, ob)

            async def one_trades(sym: str) -> None:
                while True:
                    trades = await ex.watch_trades(sym)
                    if tape_store is not None:
                        for t in trades:
                            tape_store.push(exchange_id, sym, t)

            ob_tasks = [one(s) for s in symbols]
            trade_tasks = [one_trades(s) for s in symbols] if tape_store is not None else []
            await asyncio.gather(*ob_tasks, *trade_tasks, *extra_awaitables)
            backoff_s = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if _is_reconnectable_ws_error(e):
                log.warning(
                    "WS %s: соединение закрыто/ошибка (%s) — переподключаемся через %.1fs",
                    exchange_id,
                    e,
                    backoff_s,
                )
                await _safe_close_exchange(ex)
                await asyncio.sleep(backoff_s)
                backoff_s = min(backoff_s * 1.7, 30.0)
                continue
            raise
        finally:
            await _safe_close_exchange(ex)


async def run_scalping_ws(
    settings: Settings,
    log_cb: logging.Logger,
    engine: ScalpingStrategy,
    scalp_trader: ScalpAutoTrader | None = None,
    executor: OrderExecutor | None = None,
    depth_log_levels: int = 5,
    scanner: ArbitrageScanner | None = None,
    orderflow_tape_store: OrderflowTapeStore | None = None,
) -> None:
    """Скальпинг: по одному WS ccxt.pro на каждую биржу из settings.exchanges, общий пульс.

    Пары из настроек ∪ pending по всем биржам. Pionex в CCXT нет — не включайте в SCALPING_EXCHANGES.
    """
    pending_union: set[str] = set()
    if scalp_trader:
        for eid in settings.exchanges:
            pending_union |= scalp_trader.symbols_with_pending_orders(eid)
    watch_syms = sorted(set(settings.symbols) | pending_union)
    ws_sym_set = set(watch_syms)
    strat_syms = set(settings.symbols)
    hb_state: dict[str, Any] = {"ticks": 0, "last": {}, "pulse_range": {}}
    # Диагностика: сколько сигналов (и каких) было между пульсами
    hb_state["pulse_sig_total"] = 0
    hb_state["pulse_sig_by_leg"] = {}
    sig_log_deduper = ScalpSignalLogDeduper(stale_seconds=settings.scalping_signal_log_fp_stale_seconds)

    def _engine_active_label_and_obj() -> tuple[str | None, object | None]:
        """Для ta_rotate: активный label и объект под-движка (если доступно)."""
        lab = getattr(engine, "active_label", None)
        if not isinstance(lab, str):
            lab = None
        eng = getattr(engine, "active_engine", None)
        if eng is None:
            return lab, None
        return lab, eng

    def _ta_cons_gap_diag() -> str | None:
        """Коротко: насколько далеко до условий TA conservative (RSI + near-band).

        Считаем по последним данным в кэше TA-движка: RSI и BB, и по текущему mid из последнего стакана.
        """
        if not settings.scalping_variant.startswith("ta_"):
            return None
        lab, eng = _engine_active_label_and_obj()
        if lab is not None and "ta_conservative" not in lab:
            return None
        if eng is None:
            return None
        ind = getattr(eng, "_ta_ind_block", None)
        if not isinstance(ind, dict) or not ind:
            return None

        near = float(settings.ta_band_near_bps)
        rsi_os = float(settings.ta_rsi_oversold)
        rsi_ob = float(settings.ta_rsi_overbought)

        min_d = None
        rsi_min = None
        rsi_max = None
        n = 0
        last_map = hb_state.get("last") or {}
        for ek, (q, _ob) in last_map.items():
            ex_id, sym = ek
            blk = ind.get((ex_id, sym))
            if not isinstance(blk, dict):
                continue
            bb = blk.get("bb")
            rsi = blk.get("rsi")
            if bb is None or rsi is None:
                continue
            try:
                upper, mid_bb, lower = bb
                r = float(rsi)
                mid = (float(q.bid) + float(q.ask)) / 2.0
                if mid <= 0:
                    continue
                d_lower = abs((mid - float(lower)) / mid * 10_000.0)
                d_upper = abs((float(upper) - mid) / mid * 10_000.0)
                d = min(d_lower, d_upper)
            except Exception:
                continue
            n += 1
            min_d = d if min_d is None else min(min_d, d)
            rsi_min = r if rsi_min is None else min(rsi_min, r)
            rsi_max = r if rsi_max is None else max(rsi_max, r)

        if n <= 0 or min_d is None or rsi_min is None or rsi_max is None:
            return None

        # Подсказка: что именно "не дотягивает" чаще всего.
        band_ok = min_d <= near + 1e-9
        rsi_ok = (rsi_min <= rsi_os + 1e-9) or (rsi_max >= rsi_ob - 1e-9)
        missing = []
        if not band_ok:
            missing.append(f"band {min_d:.1f}>{near:.0f}bps")
        if not rsi_ok:
            missing.append(f"RSI [{rsi_min:.1f}..{rsi_max:.1f}] vs {rsi_os:.0f}/{rsi_ob:.0f}")
        miss = "; ".join(missing) if missing else "ok"
        return f"TA(cons) min|Δband|≈{min_d:.1f}bps near≤{near:.0f}; RSI≈[{rsi_min:.1f}..{rsi_max:.1f}] ({miss})"

    def make_on_ob(ex_id: str):
        async def on_ob(q: Quote, ob: dict) -> None:
            hb_state["ticks"] += 1
            ek = (ex_id, q.symbol)
            hb_state["last"][ek] = (q, ob)
            mid = (q.bid + q.ask) / 2.0
            if mid > 0:
                pr = hb_state["pulse_range"]
                r = pr.setdefault(ek, {"min": mid, "max": mid})
                r["min"] = min(r["min"], mid)
                r["max"] = max(r["max"], mid)
            if log_cb.isEnabledFor(logging.DEBUG):
                snap = snapshot_from_order_book(
                    ex_id, q.symbol, ob, min(depth_log_levels, settings.orderbook_limit)
                )
                if snap and snap.bids and snap.asks:
                    log_cb.debug(
                        "depth %s %s bid[0]=%.8f×%.6f ask[0]=%.8f×%.6f",
                        ex_id,
                        q.symbol,
                        snap.bids[0][0],
                        snap.bids[0][1],
                        snap.asks[0][0],
                        snap.asks[0][1],
                    )
            if scalp_trader:
                scalp_trader.note_quote_for_vol_scale(ex_id, q)
                await scalp_trader.on_tick(ex_id, q.symbol, ob)
            if q.symbol not in strat_syms:
                return
            if scanner is not None and hasattr(engine, "refresh_ohlcv"):
                await engine.refresh_ohlcv(scanner, ex_id, q.symbol)
            sig = engine.on_quote(q, ob)
            sk = (ex_id, q.symbol)
            now_m = time.monotonic()
            if not sig:
                sig_log_deduper.tick_no_signal(sk, now_m)
            else:
                hb_state["pulse_sig_total"] = int(hb_state.get("pulse_sig_total", 0)) + 1
                by_leg = hb_state.get("pulse_sig_by_leg")
                if isinstance(by_leg, dict):
                    by_leg[sk] = int(by_leg.get(sk, 0)) + 1
                if sig_log_deduper.tick_signal(sk, sig.detail, now_m):
                    log_cb.info("%s %s | %s", q.symbol, ex_id, sig.detail)
                if scalp_trader:
                    await scalp_trader.on_signal(ex_id, q, sig, ob)

        return on_ob

    async def rest_sidecar_pending() -> None:
        """Live: подтягивание ордеров и on_tick по REST для каждой биржи."""
        poll = max(1.0, float(settings.poll_seconds))
        while True:
            await asyncio.sleep(poll)
            if not scalp_trader or not settings.auto_trade or settings.paper or executor is None:
                continue
            for ex_id in settings.exchanges:
                await scalp_trader.merge_open_orders_from_exchange(ex_id)
                pending = scalp_trader.symbols_with_pending_orders(ex_id)
                for sym in sorted(pending - ws_sym_set):
                    ob = await executor.fetch_order_book(ex_id, sym, settings.orderbook_limit)
                    if ob:
                        await scalp_trader.on_tick(ex_id, sym, ob)

    if executor and scalp_trader and settings.auto_trade and not settings.paper:
        asyncio.create_task(rest_sidecar_pending())

    async def ws_heartbeat() -> None:
        iv = float(settings.scalping_ws_heartbeat_seconds)
        while True:
            await asyncio.sleep(iv)
            ticks = int(hb_state["ticks"])
            last_map = hb_state["last"]
            if not last_map:
                log_cb.info("WS: тиков=%s — ещё нет котировки из стакана", ticks)
                continue
            parts: list[str] = []
            pr = hb_state["pulse_range"]
            for ek in sorted(last_map.keys(), key=lambda x: (x[0], x[1])):
                ex_id, sym = ek
                q, _ob = last_map[ek]
                mid = (q.bid + q.ask) / 2.0
                span_bps = 0.0
                r = pr.get(ek)
                if r and mid > 0:
                    span_bps = (r["max"] - r["min"]) / mid * 10_000.0
                parts.append(
                    f"{ex_id} {sym} mid≈{mid:.2f} spread {format_spread_bid_ask(q.bid, q.ask)} "
                    f"за≈{iv:.0f}s: диапазон mid≈{span_bps:.1f}bps"
                )
                pr[ek] = {"min": mid, "max": mid}
            line = " | ".join(parts)
            if settings.paper and executor is not None:
                pl = executor.paper_pnl_running_line()
                if pl:
                    line = f"{line} | {pl}"
            if settings.strategy == "scalping" and settings.scalping_variant.startswith("ta_"):
                sig_n = int(hb_state.get("pulse_sig_total", 0))
                hb_state["pulse_sig_total"] = 0
                hb_state["pulse_sig_by_leg"] = {}
                lab, _ = _engine_active_label_and_obj()
                lab_s = lab or settings.scalping_variant
                gap = _ta_cons_gap_diag()
                line = (
                    f"{line} | TA[{lab_s}]: signals≈{sig_n}/{iv:.0f}s | OHLCV {settings.ta_timeframe} "
                    f"по REST ≤{settings.ta_ohlcv_refresh_seconds:.0f}s"
                )
                if gap:
                    line = f"{line} | {gap}"
            log_cb.info("WS: тиков=%s | %s", ticks, line)

    tape_for_ws = (
        orderflow_tape_store
        if (
            orderflow_tape_store is not None
            and settings.orderflow_ws_collect
            and settings.strategy == "scalping"
        )
        else None
    )
    if tape_for_ws is not None:
        log_cb.info(
            "WS orderflow: лента сделок watch_trades + буфер %.1fs (ORDERFLOW_WS_COLLECT)",
            settings.orderflow_tape_window_seconds,
        )

    watchers = [
        asyncio.create_task(
            run_watch_tasks(
                eid,
                watch_syms,
                settings.orderbook_limit,
                make_on_ob(eid),
                tape_store=tape_for_ws,
            )
        )
        for eid in settings.exchanges
    ]
    if settings.scalping_ws_heartbeat_seconds > 0:
        watchers.append(asyncio.create_task(ws_heartbeat()))
    await _gather_ws_watchers(watchers)


async def run_arbitrage_ws(
    settings: Settings,
    log_cb: logging.Logger,
    arb_trader: ArbAutoTrader | None = None,
) -> None:
    """Межбиржа: обновление лучших цен по WS и поиск арбитража."""
    hub = LatestQuotes()

    async def on_ob(q: Quote, ob: dict) -> None:
        await hub.update(q)
        quotes = await hub.list_for_symbol(q.symbol, settings.exchanges)
        opp = compute_best_arbitrage(q.symbol, quotes, settings.fee_bps_per_side)
        if opp is None:
            return
        if opp.edge_after_fees_bps > 0:
            log_cb.info(
                "%s | купить %s @ %s → продать %s @ %s | "
                "спред %.1f bps | после комиссий %.1f bps",
                opp.symbol,
                opp.buy_exchange,
                opp.buy_at,
                opp.sell_exchange,
                opp.sell_at,
                opp.spread_bps,
                opp.edge_after_fees_bps,
            )
            if arb_trader:
                await arb_trader.on_opportunity(opp)
        else:
            log_cb.debug(
                "%s WS: лучший край после комиссий %.1f bps",
                opp.symbol,
                opp.edge_after_fees_bps,
            )

    tasks = [
        asyncio.create_task(
            run_watch_tasks(
                eid,
                list(settings.symbols),
                settings.orderbook_limit,
                on_ob,
            )
        )
        for eid in settings.exchanges
    ]
    await _gather_ws_watchers(tasks)
