"""Контракты (Protocol) для структурной типизации: Strategy, опциональное обновление OHLCV, источник рыночных данных.

Паттерны: Strategy (скальпинг-логика), Repository / Data access (сканер котировок)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from scalping import ScalpSignal
from scanner import Opportunity, Quote


@runtime_checkable
class ScalpingStrategy(Protocol):
    """Стратегия скальпинга: котировка и опционально стакан → сигнал или None (паттерн Strategy)."""

    def on_quote(self, q: Quote, order_book: dict | None = None) -> ScalpSignal | None:
        ...


class SupportsOhlcvRefresh(Protocol):
    """TA-движки: перед on_quote подгружают свечи через сканер (асинхронное обновление OHLCV)."""

    async def refresh_ohlcv(self, scanner: Any, exchange_id: str, symbol: str) -> None:
        ...


class MarketDataSource(Protocol):
    """Доступ к публичным котировкам и стакану (реализация — ArbitrageScanner; паттерн Repository / Gateway)."""

    async def fetch_quote(self, exchange_id: str, symbol: str) -> Quote | None:
        ...

    async def fetch_order_book(self, exchange_id: str, symbol: str, limit: int) -> dict | None:
        ...


class ArbitrageScannerProtocol(MarketDataSource, Protocol):
    """Полный цикл арбитражного сканера: котировки + scan_once + close."""

    async def scan_once(self) -> list[Opportunity]:
        ...

    async def close(self) -> None:
        ...
