"""Лимиты риска на открытые заявки и номинал."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RiskLimits:
    max_notional_per_order: float
    max_open_orders: int
    max_total_notional_open: float


@dataclass
class RiskManager:
    """Учёт открытого номинала по заявкам (paper и live)."""

    limits: RiskLimits
    _open_notional: dict[str, float] = field(default_factory=dict)

    def can_open(self, notional: float) -> bool:
        if notional <= 0:
            return False
        if notional > self.limits.max_notional_per_order:
            return False
        if len(self._open_notional) >= self.limits.max_open_orders:
            return False
        total = sum(self._open_notional.values())
        if total + notional > self.limits.max_total_notional_open + 1e-9:
            return False
        return True

    def can_open_exit(self, notional: float) -> bool:
        """Как can_open(), но не блокировать по лимиту числа открытых заявок.

        Используется для "закрывающих" ордеров: пусть проходят даже когда слоты забиты,
        но ограничения по номиналу на ордер и суммарному номиналу сохраняются.
        """
        if notional <= 0:
            return False
        if notional > self.limits.max_notional_per_order:
            return False
        total = sum(self._open_notional.values())
        if total + notional > self.limits.max_total_notional_open + 1e-9:
            return False
        return True

    def register(self, order_id: str, notional: float) -> None:
        self._open_notional[order_id] = notional

    def release(self, order_id: str) -> None:
        self._open_notional.pop(order_id, None)

    def set_notional(self, order_id: str, notional: float) -> None:
        """Обновить открытый номинал по ордеру (<=0 удаляет)."""
        if notional <= 0:
            self._open_notional.pop(order_id, None)
            return
        self._open_notional[order_id] = notional

    @property
    def open_count(self) -> int:
        return len(self._open_notional)

    @property
    def total_open_notional(self) -> float:
        return sum(self._open_notional.values())
