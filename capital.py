"""Ограничение убытков за сессию: потолок по реализованному PnL, cooldown, серия убыточных сделок.

Полноценный учёт реализованного PnL в боте есть в paper-режиме (`PaperSessionPnl`). Для live
лимит по сессии и цепочка убытков не заполняются из биржи — остаются только уже заданные
номинальные лимиты (`RiskManager`)."""

from __future__ import annotations

from dataclasses import dataclass, field

from config import Settings


@dataclass
class CapitalGuard:
    """Блокирует новые заявки при превышении порогов убытка (paper) или в окне cooldown."""

    max_session_loss_quote: float
    cooldown_after_loss_seconds: float
    max_consecutive_losses: int
    _cooldown_until_mono: float = field(default=0.0, repr=False)
    _consecutive_losses: int = field(default=0, repr=False)

    @classmethod
    def from_settings(cls, s: Settings) -> CapitalGuard:
        return cls(
            max_session_loss_quote=float(s.capital_max_session_loss_quote),
            cooldown_after_loss_seconds=float(s.capital_cooldown_after_loss_seconds),
            max_consecutive_losses=int(s.capital_max_consecutive_losses),
        )

    def reset(self) -> None:
        self._cooldown_until_mono = 0.0
        self._consecutive_losses = 0

    def on_sell_realized_delta(self, dpnl: float, *, now_mono: float) -> None:
        """Вызов после закрытия sell с приростом реализованного PnL (paper)."""
        if self.cooldown_after_loss_seconds <= 0 and self.max_consecutive_losses <= 0:
            return
        if dpnl < -1e-12:
            self._consecutive_losses += 1
            if self.cooldown_after_loss_seconds > 0:
                self._cooldown_until_mono = now_mono + self.cooldown_after_loss_seconds
        else:
            self._consecutive_losses = 0

    def can_open(
        self,
        *,
        realized_pnl_quote: float,
        now_mono: float,
        paper: bool,
    ) -> tuple[bool, str]:
        """Проверка перед новой позицией/лимиткой."""
        if self.cooldown_after_loss_seconds > 0 and now_mono < self._cooldown_until_mono:
            left = self._cooldown_until_mono - now_mono
            return False, f"capital: cooldown ещё {left:.1f}s"

        if (
            self.max_consecutive_losses > 0
            and self._consecutive_losses >= self.max_consecutive_losses
        ):
            return (
                False,
                f"capital: серия убытков {self._consecutive_losses} ≥ {self.max_consecutive_losses}",
            )

        if paper and self.max_session_loss_quote > 0:
            if realized_pnl_quote <= -self.max_session_loss_quote + 1e-12:
                return (
                    False,
                    f"capital: лимит сессии — реализ. PnL {realized_pnl_quote:.4f} ≤ "
                    f"-{self.max_session_loss_quote:.4f}",
                )

        return True, ""
