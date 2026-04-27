"""Лимиты риска на открытые заявки и номинал."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RiskLimits:
    max_notional_per_order: float
    max_open_orders: int
    max_total_notional_open: float
    # Optional fine-grained caps. If None/0 -> disabled.
    max_open_orders_per_symbol: int | None = None
    max_open_orders_per_exchange: int | None = None


@dataclass
class _OpenOrder:
    notional: float
    exchange_id: str | None = None
    symbol: str | None = None


@dataclass
class RiskManager:
    """Учёт открытого номинала по заявкам (paper и live)."""

    limits: RiskLimits
    _open: dict[str, _OpenOrder] = field(default_factory=dict)

    def _count_open_symbol(self, symbol: str) -> int:
        return sum(1 for o in self._open.values() if o.symbol == symbol)

    def _count_open_exchange(self, exchange_id: str) -> int:
        return sum(1 for o in self._open.values() if o.exchange_id == exchange_id)

    def _total_open_notional(self) -> float:
        return sum(o.notional for o in self._open.values())

    def can_open(self, notional: float, *, exchange_id: str | None = None, symbol: str | None = None) -> bool:
        if notional <= 0:
            return False
        if notional > self.limits.max_notional_per_order:
            return False
        if len(self._open) >= self.limits.max_open_orders:
            return False
        if (
            symbol
            and self.limits.max_open_orders_per_symbol is not None
            and self.limits.max_open_orders_per_symbol > 0
            and self._count_open_symbol(symbol) >= int(self.limits.max_open_orders_per_symbol)
        ):
            return False
        if (
            exchange_id
            and self.limits.max_open_orders_per_exchange is not None
            and self.limits.max_open_orders_per_exchange > 0
            and self._count_open_exchange(exchange_id) >= int(self.limits.max_open_orders_per_exchange)
        ):
            return False
        total = self._total_open_notional()
        if total + notional > self.limits.max_total_notional_open + 1e-9:
            return False
        return True

    def can_open_exit(self, notional: float, *, exchange_id: str | None = None, symbol: str | None = None) -> bool:
        """Как can_open(), но не блокировать по лимиту числа открытых заявок.

        Используется для "закрывающих" ордеров: пусть проходят даже когда слоты забиты,
        но ограничения по номиналу на ордер и суммарному номиналу сохраняются.

        exchange_id/symbol принимаются для совместимости интерфейса и логов (на лимиты не влияют).
        """
        if notional <= 0:
            return False
        if notional > self.limits.max_notional_per_order:
            return False
        total = self._total_open_notional()
        if total + notional > self.limits.max_total_notional_open + 1e-9:
            return False
        return True

    def register(
        self,
        order_id: str,
        notional: float,
        *,
        exchange_id: str | None = None,
        symbol: str | None = None,
    ) -> None:
        self._open[order_id] = _OpenOrder(notional=float(notional), exchange_id=exchange_id, symbol=symbol)

    def release(self, order_id: str) -> None:
        self._open.pop(order_id, None)

    def set_notional(self, order_id: str, notional: float) -> None:
        """Обновить открытый номинал по ордеру (<=0 удаляет)."""
        if notional <= 0:
            self._open.pop(order_id, None)
            return
        cur = self._open.get(order_id)
        if cur is None:
            self._open[order_id] = _OpenOrder(notional=float(notional))
        else:
            cur.notional = float(notional)

    @property
    def open_count(self) -> int:
        return len(self._open)

    @property
    def total_open_notional(self) -> float:
        return self._total_open_notional()
