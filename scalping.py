"""Простая скальпинг-логика на коротком импульсе mid-цены (bid+ask)/2.

Работает поверх тех же REST-опросов, что и арбитраж: это учебный сигнал,
не HFT. Реальный скальпинг обычно требует WebSocket, глубины стакана и
исполнения лимитных заявок у спреда."""

from __future__ import annotations

from dataclasses import dataclass, field

from scanner import Quote


@dataclass
class _Bar:
    last_mid: float | None = None


@dataclass
class ScalpingMomentum:
    """Сигнал при движении mid за один тик не меньше move_bps (в базисных пунктах)."""

    move_bps: float
    max_spread_bps: float | None = None
    _state: dict[tuple[str, str], _Bar] = field(default_factory=dict)

    def _spread_bps(self, q: Quote) -> float:
        mid = (q.bid + q.ask) / 2.0
        if mid <= 0:
            return float("inf")
        return (q.ask - q.bid) / mid * 10_000.0

    def on_quote(self, q: Quote) -> str | None:
        if self.max_spread_bps is not None and self._spread_bps(q) > self.max_spread_bps:
            return None

        mid = (q.bid + q.ask) / 2.0
        key = (q.exchange_id, q.symbol)
        bar = self._state.setdefault(key, _Bar())
        if bar.last_mid is None:
            bar.last_mid = mid
            return None

        prev = bar.last_mid
        bar.last_mid = mid
        ch_bps = (mid / prev - 1.0) * 10_000.0

        if ch_bps >= self.move_bps:
            return f"импульс LONG +{ch_bps:.1f} bps (mid {prev:.6g} → {mid:.6g})"
        if ch_bps <= -self.move_bps:
            return f"импульс SHORT {ch_bps:.1f} bps (mid {prev:.6g} → {mid:.6g})"
        return None


@dataclass
class _RevState:
    ema: float | None = None
    armed: bool = True


@dataclass
class ScalpingMeanReversion:
    """Отскок к средней: EMA(mid), сигнал «фейда», когда цена ушла от EMA на deviation_bps.

    После сигнала ждём возврата ближе к EMA (reentry_bps), чтобы не спамить в тренде."""

    deviation_bps: float
    ema_alpha: float
    max_spread_bps: float | None = None
    reentry_bps: float = 8.0
    _state: dict[tuple[str, str], _RevState] = field(default_factory=dict, repr=False)

    def _spread_bps(self, q: Quote) -> float:
        mid = (q.bid + q.ask) / 2.0
        if mid <= 0:
            return float("inf")
        return (q.ask - q.bid) / mid * 10_000.0

    def on_quote(self, q: Quote) -> str | None:
        if self.max_spread_bps is not None and self._spread_bps(q) > self.max_spread_bps:
            return None

        mid = (q.bid + q.ask) / 2.0
        key = (q.exchange_id, q.symbol)
        st = self._state.setdefault(key, _RevState())
        if st.ema is None:
            st.ema = mid
            return None

        st.ema = self.ema_alpha * mid + (1.0 - self.ema_alpha) * st.ema
        dev_bps = (mid / st.ema - 1.0) * 10_000.0

        if not st.armed:
            if abs(dev_bps) < self.reentry_bps:
                st.armed = True
            return None

        if dev_bps >= self.deviation_bps:
            st.armed = False
            return (
                f"отскок SHORT (фейд роста) dev=+{dev_bps:.1f} bps к EMA, mid={mid:.6g}"
            )
        if dev_bps <= -self.deviation_bps:
            st.armed = False
            return (
                f"отскок LONG (фейд падения) dev={dev_bps:.1f} bps к EMA, mid={mid:.6g}"
            )
        return None
