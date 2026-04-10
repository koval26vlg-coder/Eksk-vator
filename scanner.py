"""Поиск межбиржевых арбитражных возможностей по лучшим ценам bid/ask."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import ccxt.async_support as ccxt

from config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Quote:
    exchange_id: str
    symbol: str
    bid: float
    ask: float


@dataclass(frozen=True)
class Opportunity:
    symbol: str
    buy_exchange: str
    sell_exchange: str
    buy_at: float
    sell_at: float
    spread_bps: float
    edge_after_fees_bps: float


def _fee_multiplier(fee_bps_per_side: float) -> float:
    # две сделки: покупка и продажа
    return (1 + fee_bps_per_side / 10_000.0) * (1 + fee_bps_per_side / 10_000.0)


def compute_best_arbitrage(
    symbol: str,
    quotes: list[Quote],
    fee_bps_per_side: float,
) -> Opportunity | None:
    """Общая логика межбиржевого спреда (REST или WS)."""
    if len(quotes) < 2:
        return None
    fee_mul = _fee_multiplier(fee_bps_per_side)
    best: Opportunity | None = None
    for i, q_buy in enumerate(quotes):
        for q_sell in quotes[i + 1 :]:
            for a, b in ((q_buy, q_sell), (q_sell, q_buy)):
                buy_ex, sell_ex = a, b
                buy_at, sell_at = buy_ex.ask, sell_ex.bid
                if sell_at <= buy_at:
                    continue
                gross_edge = (sell_at / buy_at - 1.0) * 10_000.0
                net_ratio = sell_at / (buy_at * fee_mul)
                edge_after = (net_ratio - 1.0) * 10_000.0
                if best is None or edge_after > best.edge_after_fees_bps:
                    best = Opportunity(
                        symbol=symbol,
                        buy_exchange=buy_ex.exchange_id,
                        sell_exchange=sell_ex.exchange_id,
                        buy_at=buy_at,
                        sell_at=sell_at,
                        spread_bps=gross_edge,
                        edge_after_fees_bps=edge_after,
                    )
    return best


class ArbitrageScanner:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._clients: dict[str, ccxt.Exchange] = {}

    async def _get_client(self, exchange_id: str) -> ccxt.Exchange:
        if exchange_id in self._clients:
            return self._clients[exchange_id]
        if not hasattr(ccxt, exchange_id):
            raise ValueError(f"Неизвестная биржа в CCXT: {exchange_id}")
        cls = getattr(ccxt, exchange_id)
        ex: ccxt.Exchange = cls({"enableRateLimit": True, "options": {"defaultType": "spot"}})
        self._clients[exchange_id] = ex
        return ex

    async def fetch_quote(self, exchange_id: str, symbol: str) -> Quote | None:
        ex = await self._get_client(exchange_id)
        if not ex.has.get("fetchTicker"):
            log.warning("%s: нет fetchTicker", exchange_id)
            return None
        try:
            if not ex.markets:
                await ex.load_markets()
            if symbol not in ex.symbols:
                log.debug("%s: нет пары %s", exchange_id, symbol)
                return None
            t = await ex.fetch_ticker(symbol)
            bid = t.get("bid")
            ask = t.get("ask")
            if bid is None or ask is None or bid <= 0 or ask <= 0:
                return None
            return Quote(exchange_id=exchange_id, symbol=symbol, bid=float(bid), ask=float(ask))
        except Exception as e:
            log.warning("%s %s: %s", exchange_id, symbol, e)
            return None

    async def fetch_ohlcv(
        self, exchange_id: str, symbol: str, timeframe: str, limit: int
    ) -> list | None:
        ex = await self._get_client(exchange_id)
        if not ex.has.get("fetchOHLCV", False):
            log.warning("%s: нет fetchOHLCV", exchange_id)
            return None
        try:
            if not ex.markets:
                await ex.load_markets()
            if symbol not in ex.symbols:
                return None
            return await ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        except Exception as e:
            log.warning("%s %s OHLCV %s: %s", exchange_id, symbol, timeframe, e)
            return None

    async def fetch_order_book(self, exchange_id: str, symbol: str, limit: int) -> dict | None:
        ex = await self._get_client(exchange_id)
        if not ex.has.get("fetchOrderBook"):
            log.warning("%s: нет fetchOrderBook", exchange_id)
            return None
        try:
            if not ex.markets:
                await ex.load_markets()
            if symbol not in ex.symbols:
                log.debug("%s: нет пары %s", exchange_id, symbol)
                return None
            return await ex.fetch_order_book(symbol, limit)
        except Exception as e:
            log.warning("%s %s order book: %s", exchange_id, symbol, e)
            return None

    async def collect_quotes(self, symbol: str) -> list[Quote]:
        tasks = [self.fetch_quote(eid, symbol) for eid in self._s.exchanges]
        results = await asyncio.gather(*tasks)
        return [q for q in results if q is not None]

    def find_best_opportunity(self, symbol: str, quotes: list[Quote]) -> Opportunity | None:
        return compute_best_arbitrage(symbol, quotes, self._s.fee_bps_per_side)

    async def scan_once(self) -> list[Opportunity]:
        out: list[Opportunity] = []
        for sym in self._s.symbols:
            quotes = await self.collect_quotes(sym)
            opp = self.find_best_opportunity(sym, quotes)
            if opp is not None:
                out.append(opp)
        return out

    async def close(self) -> None:
        for ex in self._clients.values():
            await ex.close()
        self._clients.clear()
