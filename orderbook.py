"""Стакан: разбор CCXT order book и снимок глубины."""

from __future__ import annotations

from dataclasses import dataclass

from scanner import Quote


@dataclass(frozen=True)
class OrderBookSnapshot:
    """Верх стакана + ограниченная глубина для логов и стратегий."""

    exchange_id: str
    symbol: str
    best_bid: float
    best_ask: float
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]


def _levels(raw: list | None, max_levels: int) -> tuple[tuple[float, float], ...]:
    if not raw:
        return ()
    out: list[tuple[float, float]] = []
    for row in raw[:max_levels]:
        if len(row) < 2:
            continue
        out.append((float(row[0]), float(row[1])))
    return tuple(out)


def quote_from_order_book(exchange_id: str, symbol: str, ob: dict) -> Quote | None:
    """Лучший bid/ask из объекта CCXT order book."""
    bids = ob.get("bids") or []
    asks = ob.get("asks") or []
    if not bids or not asks:
        return None
    bid = float(bids[0][0])
    ask = float(asks[0][0])
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return Quote(exchange_id=exchange_id, symbol=symbol, bid=bid, ask=ask)


def snapshot_from_order_book(
    exchange_id: str,
    symbol: str,
    ob: dict,
    depth_levels: int,
) -> OrderBookSnapshot | None:
    q = quote_from_order_book(exchange_id, symbol, ob)
    if q is None:
        return None
    bids = _levels(ob.get("bids"), depth_levels)
    asks = _levels(ob.get("asks"), depth_levels)
    return OrderBookSnapshot(
        exchange_id=exchange_id,
        symbol=symbol,
        best_bid=q.bid,
        best_ask=q.ask,
        bids=bids,
        asks=asks,
    )


def spread_bps(bid: float, ask: float) -> float:
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return float("inf")
    return (ask - bid) / mid * 10_000.0


def format_spread_bid_ask(bid: float, ask: float) -> str:
    """Для лога: на BTC/USDT спред часто <0.1 bps — показываем bps с 3 знаками и абсолют в USDT."""
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return "—"
    bps = (ask - bid) / mid * 10_000.0
    return f"{bps:.3f}bps ({ask - bid:.2f} USDT)"


def symmetric_liquidity_quote(order_book: dict | None, levels: int) -> float | None:
    """Суммарный номинал в quote на bid и ask по верхним уровням; возвращает min( bid_notional, ask_notional ) — «узкое место» ликвидности."""
    if not order_book:
        return None
    bids = order_book.get("bids") or []
    asks = order_book.get("asks") or []
    if not bids or not asks:
        return None
    n = max(1, levels)
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
    return min(bq, aq)


def vwap_buy_with_quote(asks: list | None, quote_budget: float) -> tuple[float, float, float, bool]:
    """Покупка за quote_budget по уровням ask: (base, vwap, quote_spent, полностью уместилось)."""
    if not asks or quote_budget <= 0:
        return 0.0, 0.0, 0.0, False
    remaining = quote_budget
    total_base = 0.0
    spent = 0.0
    for row in asks:
        if remaining <= 0:
            break
        if len(row) < 2:
            continue
        price = float(row[0])
        qty = float(row[1])
        if price <= 0 or qty <= 0:
            continue
        max_spend = qty * price
        take_spend = min(remaining, max_spend)
        take_base = take_spend / price
        total_base += take_base
        spent += take_spend
        remaining -= take_spend
    if total_base <= 0:
        return 0.0, 0.0, 0.0, False
    complete = remaining <= max(1e-9, quote_budget * 1e-12)
    vwap = spent / total_base
    return total_base, vwap, spent, complete


def vwap_buy_base(asks: list | None, base_amount: float) -> tuple[float, float, float, bool]:
    """Покупка base_amount по уровням ask: (quote_spent, vwap, base_bought, полностью уместилось)."""
    if not asks or base_amount <= 0:
        return 0.0, 0.0, 0.0, False
    remaining = base_amount
    spent = 0.0
    bought = 0.0
    for row in asks:
        if remaining <= 0:
            break
        if len(row) < 2:
            continue
        price = float(row[0])
        qty = float(row[1])
        if price <= 0 or qty <= 0:
            continue
        take_base = min(remaining, qty)
        spent += take_base * price
        bought += take_base
        remaining -= take_base
    if bought <= 0:
        return 0.0, 0.0, 0.0, False
    complete = remaining <= max(1e-12, base_amount * 1e-12)
    vwap = spent / bought
    return spent, vwap, bought, complete


def vwap_sell_base(bids: list | None, base_amount: float) -> tuple[float, float, float, bool]:
    """Продажа base_amount по уровням bid: (quote, vwap, base_sold, полностью уместилось)."""
    if not bids or base_amount <= 0:
        return 0.0, 0.0, 0.0, False
    remaining = base_amount
    total_quote = 0.0
    sold_base = 0.0
    for row in bids:
        if remaining <= 0:
            break
        if len(row) < 2:
            continue
        price = float(row[0])
        qty = float(row[1])
        if price <= 0 or qty <= 0:
            continue
        take_base = min(remaining, qty)
        total_quote += take_base * price
        sold_base += take_base
        remaining -= take_base
    if sold_base <= 0:
        return 0.0, 0.0, 0.0, False
    complete = remaining <= max(1e-12, base_amount * 1e-12)
    vwap = total_quote / sold_base
    return total_quote, vwap, sold_base, complete


def simulated_slippage_bps_buy(best_ask: float, vwap: float, mid: float) -> float:
    """Насколько VWAP хуже лучшего ask (bps от mid), при покупке съедением ask."""
    if mid <= 0 or best_ask <= 0:
        return float("inf")
    return max(0.0, (vwap - best_ask) / mid * 10_000.0)


def simulated_slippage_bps_sell(best_bid: float, vwap: float, mid: float) -> float:
    """Насколько VWAP хуже лучшего bid (bps от mid), при продаже съедением bid."""
    if mid <= 0 or best_bid <= 0:
        return float("inf")
    return max(0.0, (best_bid - vwap) / mid * 10_000.0)


def immediate_limit_buy_fill(
    limit_price: float,
    amount_base: float,
    asks: list | None,
) -> tuple[float, float, float]:
    """Лимит buy: уровни ask с ценой <= limit_price (как IOC по доступной глубине).

    Возвращает (filled_base, quote_spent, avg_price).
    """
    if not asks or amount_base <= 0 or limit_price <= 0:
        return 0.0, 0.0, 0.0
    rem = amount_base
    spent = 0.0
    filled = 0.0
    for row in asks:
        if rem <= 1e-18:
            break
        if len(row) < 2:
            continue
        p = float(row[0])
        q = float(row[1])
        if p <= 0 or q <= 0:
            continue
        if p > limit_price + 1e-12:
            break
        take = min(rem, q)
        filled += take
        spent += take * p
        rem -= take
    avg = spent / filled if filled > 0 else 0.0
    return filled, spent, avg


def immediate_limit_sell_fill(
    limit_price: float,
    amount_base: float,
    bids: list | None,
) -> tuple[float, float, float]:
    """Лимит sell: уровни bid с ценой >= limit_price (лучшая цена первой — как в CCXT).

    Возвращает (filled_base, quote_received, avg_price).
    """
    if not bids or amount_base <= 0 or limit_price <= 0:
        return 0.0, 0.0, 0.0
    rem = amount_base
    recv = 0.0
    filled = 0.0
    for row in bids:
        if rem <= 1e-18:
            break
        if len(row) < 2:
            continue
        p = float(row[0])
        q = float(row[1])
        if p <= 0 or q <= 0:
            continue
        if p < limit_price - 1e-12:
            break
        take = min(rem, q)
        filled += take
        recv += take * p
        rem -= take
    avg = recv / filled if filled > 0 else 0.0
    return filled, recv, avg
