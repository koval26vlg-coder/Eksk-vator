"""Сборка исполнения и риска (паттерн Facade): одна точка входа для OrderExecutor + RiskManager + CapitalGuard."""

from __future__ import annotations

from capital import CapitalGuard
from config import Settings
from execution import OrderExecutor
from risk import RiskLimits, RiskManager


def build_trading_execution(settings: Settings) -> tuple[RiskManager, OrderExecutor]:
    limits = RiskLimits(
        max_notional_per_order=settings.risk_max_notional_per_order,
        max_open_orders=settings.risk_max_open_orders,
        max_total_notional_open=settings.risk_max_total_notional,
    )
    risk = RiskManager(limits=limits)
    capital = CapitalGuard.from_settings(settings)
    return risk, OrderExecutor(settings, risk, capital)
