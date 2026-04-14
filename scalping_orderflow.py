"""Order-flow: краткое окно ленты сделок (WS) + давление по верхним уровням стакана.

Отдельный под-вариант `orderflow` для ScalpingRotate / ta_rotate: активен по очереди с TA/momentum,
не вызывается одновременно с ними — не конкурирует за on_quote."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Literal

from config import Settings
from scalping import ScalpSignal
from scanner import Quote


@dataclass
class TapeWindowStats:
    """Суммы в quote-валюте за скользящее окно по времени (monotonic)."""

    buy_quote: float
    sell_quote: float
    t_span_sec: float

    @property
    def total(self) -> float:
        return self.buy_quote + self.sell_quote

    @property
    def buy_share(self) -> float:
        t = self.total
        if t <= 0:
            return 0.5
        return self.buy_quote / t


class OrderflowTapeStore:
    """Потокобезопасное кольцо сделок для подписки WS; чтение из синхронного on_quote."""

    def __init__(self, *, window_seconds: float, max_events: int) -> None:
        self._window = max(0.05, float(window_seconds))
        self._max = max(16, int(max_events))
        self._lock = threading.Lock()
        self._deques: dict[tuple[str, str], deque[tuple[float, str, float]]] = defaultdict(deque)

    def push(self, exchange_id: str, symbol: str, trade: dict) -> None:
        """Добавить сделку в формате CCXT unified (side, amount, price, cost?)."""
        side = (trade.get("side") or "").strip().lower()
        if side not in ("buy", "sell"):
            return
        try:
            amount = float(trade.get("amount") or 0.0)
            price = float(trade.get("price") or 0.0)
        except (TypeError, ValueError):
            return
        if amount <= 0 or price <= 0:
            return
        cost = trade.get("cost")
        try:
            quote_vol = float(cost) if cost is not None else amount * price
        except (TypeError, ValueError):
            quote_vol = amount * price
        if quote_vol <= 0:
            return
        key = (exchange_id, symbol)
        now = time.monotonic()
        with self._lock:
            dq = self._deques[key]
            dq.append((now, side, quote_vol))
            while len(dq) > self._max:
                dq.popleft()

    def window_stats(self, exchange_id: str, symbol: str) -> TapeWindowStats | None:
        now = time.monotonic()
        t0 = now - self._window
        with self._lock:
            dq = self._deques.get((exchange_id, symbol))
            if not dq:
                return None
            buy = 0.0
            sell = 0.0
            oldest: float | None = None
            for ts, side, qv in dq:
                if ts < t0:
                    continue
                if oldest is None:
                    oldest = ts
                if side == "buy":
                    buy += qv
                else:
                    sell += qv
        if oldest is None:
            return None
        span = max(0.0, now - oldest)
        return TapeWindowStats(buy_quote=buy, sell_quote=sell, t_span_sec=span)


def _depth_pressure(ob: dict | None, levels: int) -> tuple[float, float] | None:
    """Суммарный номинал bid и ask по верхним уровням (price×amount)."""
    if not ob:
        return None
    bids = ob.get("bids") or []
    asks = ob.get("asks") or []
    if not bids or not asks:
        return None
    n = max(1, int(levels))
    bq = 0.0
    aq = 0.0
    for row in bids[:n]:
        if len(row) >= 2:
            bq += float(row[0]) * float(row[1])
    for row in asks[:n]:
        if len(row) >= 2:
            aq += float(row[0]) * float(row[1])
    if bq <= 0 or aq <= 0:
        return None
    return bq, aq


@dataclass
class ScalpingOrderflow:
    """Сигнал при согласовании ленты и/или перекоса глубины стакана (настраивается)."""

    settings: Settings
    tape_store: OrderflowTapeStore | None
    _last_signal_mono: dict[tuple[str, str], float] = field(default_factory=dict)

    def _spread_bps(self, q: Quote) -> float:
        mid = (q.bid + q.ask) / 2.0
        if mid <= 0:
            return float("inf")
        return (q.ask - q.bid) / mid * 10_000.0

    def _tape_leg_long(self, st: TapeWindowStats | None) -> tuple[bool, str]:
        s = self.settings
        if st is None or st.total + 1e-12 < s.orderflow_tape_min_total_quote:
            return False, "tape∅"
        sh = st.buy_share
        if sh >= s.orderflow_tape_long_share:
            return True, f"tape buy {sh * 100:.0f}% ({st.total:.0f} quote / {st.t_span_sec:.1f}s)"
        return False, f"tape buy {sh * 100:.0f}%"

    def _tape_leg_short(self, st: TapeWindowStats | None) -> tuple[bool, str]:
        s = self.settings
        if st is None or st.total + 1e-12 < s.orderflow_tape_min_total_quote:
            return False, "tape∅"
        sh = st.buy_share
        if sh <= s.orderflow_tape_short_share:
            return True, f"tape buy {sh * 100:.0f}% ({st.total:.0f} quote / {st.t_span_sec:.1f}s)"
        return False, f"tape buy {sh * 100:.0f}%"

    def _book_leg_long(self, ob: dict | None) -> tuple[bool, str]:
        s = self.settings
        pr = _depth_pressure(ob, s.orderflow_book_levels)
        if pr is None:
            return False, "book∅"
        bq, aq = pr
        if bq < s.orderflow_book_min_side_quote or aq < s.orderflow_book_min_side_quote:
            return False, f"book thin ({bq:.0f}/{aq:.0f} quote)"
        ratio = bq / aq
        if ratio >= s.orderflow_book_ratio_long:
            return True, f"bid/ask {ratio:.2f}× (top {s.orderflow_book_levels})"
        return False, f"bid/ask {ratio:.2f}×"

    def _book_leg_short(self, ob: dict | None) -> tuple[bool, str]:
        s = self.settings
        pr = _depth_pressure(ob, s.orderflow_book_levels)
        if pr is None:
            return False, "book∅"
        bq, aq = pr
        if bq < s.orderflow_book_min_side_quote or aq < s.orderflow_book_min_side_quote:
            return False, f"book thin ({bq:.0f}/{aq:.0f} quote)"
        ratio = bq / aq
        if ratio <= s.orderflow_book_ratio_short:
            return True, f"bid/ask {ratio:.2f}× (top {s.orderflow_book_levels})"
        return False, f"bid/ask {ratio:.2f}×"

    def _combine(self, tape_ok: bool, book_ok: bool) -> bool:
        if self.settings.orderflow_signal_mode == "either":
            return tape_ok or book_ok
        return tape_ok and book_ok

    def _impulse_bps(
        self,
        side: Literal["buy", "sell"],
        st: TapeWindowStats | None,
        ob: dict | None,
    ) -> float:
        dev = 0.0
        if st is not None and st.total > 0:
            sh = st.buy_share
            dev = abs(sh - 0.5) * 2.0
        br_excess = 0.0
        pr = _depth_pressure(ob, self.settings.orderflow_book_levels)
        if pr is not None:
            bq, aq = pr
            r = bq / aq
            if side == "buy":
                br_excess = max(0.0, min(2.0, r - 1.0)) / 2.0
            else:
                br_excess = max(0.0, min(2.0, 1.0 / max(r, 1e-12) - 1.0)) / 2.0
        raw = 6.0 + dev * 28.0 + br_excess * 22.0
        return max(5.0, min(55.0, raw))

    def on_quote(self, q: Quote, order_book: dict | None = None) -> ScalpSignal | None:
        s = self.settings
        if s.scalping_max_spread_bps is not None and self._spread_bps(q) > s.scalping_max_spread_bps:
            return None
        key = (q.exchange_id, q.symbol)
        now = time.monotonic()
        cd = s.orderflow_cooldown_seconds
        if cd > 0:
            last = self._last_signal_mono.get(key, 0.0)
            if now - last < cd:
                return None

        st = self.tape_store.window_stats(q.exchange_id, q.symbol) if self.tape_store else None

        t_long, t_long_d = self._tape_leg_long(st)
        t_short, t_short_d = self._tape_leg_short(st)
        b_long, b_long_d = self._book_leg_long(order_book)
        b_short, b_short_d = self._book_leg_short(order_book)

        long_ok = self._combine(t_long, b_long)
        short_ok = self._combine(t_short, b_short)

        if long_ok and short_ok:
            return None
        if long_ok:
            self._last_signal_mono[key] = now
            detail = f"orderflow LONG: {t_long_d} | {b_long_d}"
            return ScalpSignal(
                side="buy",
                detail=detail,
                impulse_bps=self._impulse_bps("buy", st, order_book),
            )
        if short_ok:
            self._last_signal_mono[key] = now
            detail = f"orderflow SHORT: {t_short_d} | {b_short_d}"
            return ScalpSignal(
                side="sell",
                detail=detail,
                impulse_bps=self._impulse_bps("sell", st, order_book),
            )
        return None
