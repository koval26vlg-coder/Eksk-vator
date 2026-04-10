"""Реализованный PnL в quote (USDT) для paper-ордеров: FIFO long + FIFO short (шорт без long — не «пустой» PnL)."""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

log = logging.getLogger(__name__)


class PaperSessionPnl:
    """Long FIFO; избыток продажи — шорт; покупка сначала закрывает шорт, остаток — в long."""

    def __init__(self, fee_bps_per_side: float) -> None:
        self._fee = float(fee_bps_per_side)
        self._long: dict[tuple[str, str], deque[tuple[float, float]]] = {}
        self._short: dict[tuple[str, str], deque[tuple[float, float]]] = {}
        self.realized_pnl_quote = 0.0
        self.fees_paid_quote = 0.0
        self._seen_order_ids: set[str] = set()
        self.closed_buys = 0
        self.closed_sells = 0

    def reset(self) -> None:
        self._long.clear()
        self._short.clear()
        self.realized_pnl_quote = 0.0
        self.fees_paid_quote = 0.0
        self._seen_order_ids.clear()
        self.closed_buys = 0
        self.closed_sells = 0

    def try_record_closed_order(self, order: dict[str, Any]) -> float | None:
        """Один раз на закрытый ордер. Возвращает прирост реализованного PnL (только для sell), иначе None."""
        oid = str(order.get("id", ""))
        if not oid or oid in self._seen_order_ids:
            return None
        st = str(order.get("status", "")).lower()
        if st != "closed":
            return None
        filled = float(order.get("filled") or 0.0)
        if filled <= 1e-18:
            return None

        exchange_id = str(order.get("exchange_id", ""))
        symbol = str(order.get("symbol", ""))
        side = str(order.get("side", "")).lower()
        cum_quote = float(order.get("cum_quote", 0.0))
        if cum_quote <= 0.0:
            avg = float(order.get("average") or 0.0)
            cum_quote = avg * filled

        self._seen_order_ids.add(oid)
        key = (exchange_id, symbol)

        if side == "buy":
            self._record_buy(key, filled, cum_quote)
            self.closed_buys += 1
            return None

        if side == "sell":
            delta = self._realize_sell(key, filled, cum_quote)
            self.closed_sells += 1
            return delta

        return None

    def _record_buy(self, key: tuple[str, str], filled: float, cum_quote: float) -> None:
        fee = cum_quote * self._fee / 10_000.0
        cost = cum_quote + fee
        self.fees_paid_quote += fee
        avg_cost = cost / filled if filled > 1e-18 else 0.0

        rem = filled
        sdeque = self._short.setdefault(key, deque())
        while rem > 1e-18 and sdeque:
            sb, sentry = sdeque[0]
            take = min(sb, rem)
            # Открыли шорт по net sell (sentry); закрываем покупкой по avg_cost за base.
            self.realized_pnl_quote += (sentry - avg_cost) * take
            sb -= take
            rem -= take
            if sb <= 1e-18:
                sdeque.popleft()
            else:
                sdeque[0] = (sb, sentry)

        if rem > 1e-18:
            self._long.setdefault(key, deque()).append((rem, avg_cost))

    def _realize_sell(self, key: tuple[str, str], filled: float, cum_quote: float) -> float:
        fee = cum_quote * self._fee / 10_000.0
        proceeds = cum_quote - fee
        self.fees_paid_quote += fee
        avg_sell = proceeds / filled if filled > 1e-18 else 0.0
        before = self.realized_pnl_quote
        rem = filled
        dq = self._long.setdefault(key, deque())
        while rem > 1e-18 and dq:
            lot_b, lot_c = dq[0]
            take = min(lot_b, rem)
            self.realized_pnl_quote += (avg_sell - lot_c) * take
            lot_b -= take
            rem -= take
            if lot_b <= 1e-18:
                dq.popleft()
            else:
                dq[0] = (lot_b, lot_c)
        if rem > 1e-12:
            # Шорт: запись — net proceeds за 1 base (как «цена» входа в шорт для последующего покрытия).
            self._short.setdefault(key, deque()).append((rem, avg_sell))
        return self.realized_pnl_quote - before

    def summary_line(self) -> str:
        inv: list[str] = []
        for (ex, sym), dq in self._long.items():
            b = sum(lb for lb, _ in dq)
            if b > 1e-12:
                inv.append(f"{sym}@{ex} long≈{b:.6f} base")
        for (ex, sym), dq in self._short.items():
            b = sum(lb for lb, _ in dq)
            if b > 1e-12:
                inv.append(f"{sym}@{ex} short≈{b:.6f} base")
        tail = f" | незакрытый инвентарь: {'; '.join(inv)}" if inv else ""
        return (
            f"реализованный PnL ≈ {self.realized_pnl_quote:.4f} USDT, "
            f"комиссии (оценка) ≈ {self.fees_paid_quote:.4f} USDT, "
            f"закрыто ордеров buy/sell: {self.closed_buys}/{self.closed_sells}{tail}"
        )

    def net_position_base(self, exchange_id: str, symbol: str) -> float:
        """Текущий net-инвентарь по базе (base): long>0, short<0."""
        key = (exchange_id, symbol)
        long_b = sum(lb for lb, _ in self._long.get(key, ()))
        short_b = sum(lb for lb, _ in self._short.get(key, ()))
        return float(long_b - short_b)

    def net_position_base_symbol(self, symbol: str) -> float:
        """Текущий net-инвентарь по базе (base) суммарно по всем биржам для symbol."""
        sym = str(symbol)
        long_b = 0.0
        short_b = 0.0
        for (ex, s), dq in self._long.items():
            if s == sym:
                long_b += sum(lb for lb, _ in dq)
        for (ex, s), dq in self._short.items():
            if s == sym:
                short_b += sum(lb for lb, _ in dq)
        return float(long_b - short_b)

    def position_entry_vwap(self, exchange_id: str, symbol: str) -> tuple[float, float | None]:
        """Средняя цена входа (VWAP) по открытой позиции в base.

        Возвращает (pos_base, entry_vwap):
        - pos_base>0 → long, entry_vwap по avg_cost (включая комиссию buy)
        - pos_base<0 → short, entry_vwap по avg_sell (после комиссии sell)
        - pos_base≈0 → (0, None)
        """
        key = (exchange_id, symbol)
        long_dq = self._long.get(key) or ()
        short_dq = self._short.get(key) or ()
        long_b = sum(lb for lb, _ in long_dq)
        short_b = sum(lb for lb, _ in short_dq)
        pos = float(long_b - short_b)
        if abs(pos) <= 1e-12:
            return 0.0, None
        if pos > 0:
            num = sum(lb * c for lb, c in long_dq)
            den = long_b
            return pos, (float(num / den) if den > 1e-18 else None)
        num = sum(lb * p for lb, p in short_dq)
        den = short_b
        return pos, (float(num / den) if den > 1e-18 else None)

    def position_entry_vwap_symbol(self, symbol: str) -> tuple[float, float | None]:
        """Как position_entry_vwap, но суммарно по всем биржам для symbol."""
        sym = str(symbol)
        long_b = 0.0
        short_b = 0.0
        long_num = 0.0
        short_num = 0.0
        for (_ex, s), dq in self._long.items():
            if s != sym:
                continue
            for lb, c in dq:
                long_b += lb
                long_num += lb * c
        for (_ex, s), dq in self._short.items():
            if s != sym:
                continue
            for lb, p in dq:
                short_b += lb
                short_num += lb * p
        pos = float(long_b - short_b)
        if abs(pos) <= 1e-12:
            return 0.0, None
        if pos > 0:
            return pos, (float(long_num / long_b) if long_b > 1e-18 else None)
        return pos, (float(short_num / short_b) if short_b > 1e-18 else None)
