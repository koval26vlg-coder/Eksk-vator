"""Простая скальпинг-логика на коротком импульсе mid-цены (bid+ask)/2.

Работает поверх тех же REST-опросов, что и арбитраж: это учебный сигнал,
не HFT. Реальный скальпинг обычно требует WebSocket, глубины стакана и
исполнения лимитных заявок у спреда."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import logging
import re
import statistics
import time
from typing import Any, Literal

from orderbook import symmetric_liquidity_quote
from scanner import Quote

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScalpSignal:
    """Направление сделки и текст для лога."""

    side: Literal["buy", "sell"]
    detail: str
    #: |Δmid| за тик (momentum/filtered) или |dev| к EMA (mean_reversion); для фильтра авто-торговли по комиссиям
    impulse_bps: float | None = None


def signal_log_fingerprint(detail: str) -> str:
    """Ключ для дедупликации INFO: для TA cons убираем скобки с числами — один сценарий, один INFO до stale."""
    if "TA cons:" not in detail:
        return detail
    s = re.sub(r"\([^)]*\)", "", detail)
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class ScalpSignalLogDeduper:
    """Дедуп INFO-логов сигналов (REST и WS): отпечаток + устойчивость к мерцанию сигнала."""

    #: Секунд без сигнала до сброса отпечатка (из Settings.scalping_signal_log_fp_stale_seconds).
    stale_seconds: float = 30.0
    _state: dict[tuple[str, str], tuple[str, float]] = field(default_factory=dict)

    def tick_no_signal(self, sk: tuple[str, str], now_m: float) -> None:
        st = self._state.get(sk)
        if st is not None and (now_m - st[1] > self.stale_seconds):
            self._state.pop(sk, None)

    def tick_signal(self, sk: tuple[str, str], detail: str, now_m: float) -> bool:
        """True — вывести INFO с этим detail (сменился отпечаток)."""
        fp = signal_log_fingerprint(detail)
        prev_fp = self._state.get(sk, (None, 0.0))[0]
        self._state[sk] = (fp, now_m)
        return fp != prev_fp


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

    def on_quote(self, q: Quote, order_book: dict | None = None) -> ScalpSignal | None:
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
            return ScalpSignal(
                side="buy",
                detail=f"импульс LONG +{ch_bps:.1f} bps (mid {prev:.6g} → {mid:.6g})",
                impulse_bps=abs(ch_bps),
            )
        if ch_bps <= -self.move_bps:
            return ScalpSignal(
                side="sell",
                detail=f"импульс SHORT {ch_bps:.1f} bps (mid {prev:.6g} → {mid:.6g})",
                impulse_bps=abs(ch_bps),
            )
        return None


@dataclass
class ScalpingFilteredMomentum:
    """Импульс mid, как momentum, плюс фильтр режима по микроволатильности последних тиков.

    По скользящему окну из последних изменений mid (bps) считается σ (pstdev).
    Сигнал возможен только если σ в диапазоне [min_micro_vol_bps, max_micro_vol_bps]
    (границы 0 = без ограничения с этой стороны). Так отсекаются «мёртвый» рынок и хаотичные всплески.
    """

    move_bps: float
    max_spread_bps: float | None = None
    vol_window: int = 15
    min_micro_vol_bps: float = 0.0
    max_micro_vol_bps: float = 0.0
    _state: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict, repr=False)

    def _spread_bps(self, q: Quote) -> float:
        mid = (q.bid + q.ask) / 2.0
        if mid <= 0:
            return float("inf")
        return (q.ask - q.bid) / mid * 10_000.0

    def on_quote(self, q: Quote, order_book: dict | None = None) -> ScalpSignal | None:
        if self.max_spread_bps is not None and self._spread_bps(q) > self.max_spread_bps:
            return None

        mid = (q.bid + q.ask) / 2.0
        key = (q.exchange_id, q.symbol)
        if key not in self._state:
            self._state[key] = {"last_mid": None, "dq": deque(maxlen=max(3, self.vol_window))}
        st = self._state[key]
        if st["dq"].maxlen != max(3, self.vol_window):
            st["dq"] = deque(st["dq"], maxlen=max(3, self.vol_window))

        last_mid = st["last_mid"]
        if last_mid is None:
            st["last_mid"] = mid
            return None

        ch_bps = (mid / last_mid - 1.0) * 10_000.0
        st["last_mid"] = mid
        st["dq"].append(ch_bps)

        dq = st["dq"]
        use_vol = self.min_micro_vol_bps > 0.0 or self.max_micro_vol_bps > 0.0
        if use_vol:
            if len(dq) < 2:
                return None
            micro_vol = statistics.pstdev(dq)
            if self.min_micro_vol_bps > 0.0 and micro_vol < self.min_micro_vol_bps:
                return None
            if self.max_micro_vol_bps > 0.0 and micro_vol > self.max_micro_vol_bps:
                return None
        else:
            micro_vol = statistics.pstdev(dq) if len(dq) >= 2 else 0.0

        if ch_bps >= self.move_bps:
            return ScalpSignal(
                side="buy",
                detail=(
                    f"filtered LONG +{ch_bps:.1f} bps σ_ticks={micro_vol:.2f} "
                    f"(mid {last_mid:.6g} → {mid:.6g})"
                ),
                impulse_bps=abs(ch_bps),
            )
        if ch_bps <= -self.move_bps:
            return ScalpSignal(
                side="sell",
                detail=(
                    f"filtered SHORT {ch_bps:.1f} bps σ_ticks={micro_vol:.2f} "
                    f"(mid {last_mid:.6g} → {mid:.6g})"
                ),
                impulse_bps=abs(ch_bps),
            )
        return None

    def rest_tick_diag(self, key: tuple[str, str]) -> str | None:
        """Кратко для REST-пульса: последний шаг Δ (bps) и σ по окну vs пороги стратегии."""
        st = self._state.get(key)
        if not st:
            return None
        dq = st["dq"]
        if not dq:
            return None
        last_ch = float(dq[-1])
        max_abs = max(abs(float(x)) for x in dq) if dq else 0.0
        sig = statistics.pstdev(dq) if len(dq) >= 2 else 0.0
        lo, hi = self.min_micro_vol_bps, self.max_micro_vol_bps
        if lo <= 0 and hi <= 0:
            band = "σ без фильтра"
        else:
            lo_s = f"{lo:.1f}" if lo > 0 else "0"
            hi_s = f"{hi:.1f}" if hi > 0 else "∞"
            band = f"σ∈[{lo_s}…{hi_s}] bps"
        return (
            f"Δ_last={last_ch:.2f}bps max|Δ|={max_abs:.2f} σ={sig:.2f} "
            f"| нужно |Δ|≥{self.move_bps:.1f}, {band}"
        )


@dataclass
class _RevState:
    ema: float | None = None
    armed: bool = True
    prev_ema: float | None = None  # Для детекции тренда


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

    def on_quote(self, q: Quote, order_book: dict | None = None) -> ScalpSignal | None:
        if self.max_spread_bps is not None and self._spread_bps(q) > self.max_spread_bps:
            return None

        mid = (q.bid + q.ask) / 2.0
        key = (q.exchange_id, q.symbol)
        st = self._state.setdefault(key, _RevState())
        if st.ema is None:
            st.ema = mid
            return None

        # Сохраняем предыдущую EMA для детекции тренда
        if st.prev_ema is None:
            st.prev_ema = st.ema

        prev_ema = st.prev_ema
        st.ema = self.ema_alpha * mid + (1.0 - self.ema_alpha) * st.ema
        dev_bps = (mid / st.ema - 1.0) * 10_000.0

        # Детектор тренда: если EMA растет/падает более чем на deviation_bps/2, это тренд
        ema_slope_bps = (st.ema / prev_ema - 1.0) * 10_000.0 if prev_ema > 0 else 0.0
        trend_threshold = self.deviation_bps * 0.5
        in_uptrend = ema_slope_bps > trend_threshold
        in_downtrend = ema_slope_bps < -trend_threshold

        st.prev_ema = st.ema

        if not st.armed:
            if abs(dev_bps) < self.reentry_bps:
                st.armed = True
            return None

        if dev_bps >= self.deviation_bps:
            # Не фейдим восходящий тренд
            if in_uptrend:
                return None
            st.armed = False
            return ScalpSignal(
                side="sell",
                detail=f"отскок SHORT (фейд роста) dev=+{dev_bps:.1f} bps к EMA, mid={mid:.6g}",
                impulse_bps=abs(dev_bps),
            )
        if dev_bps <= -self.deviation_bps:
            # Не фейдим нисходящий тренд
            if in_downtrend:
                return None
            st.armed = False
            return ScalpSignal(
                side="buy",
                detail=f"отскок LONG (фейд падения) dev={dev_bps:.1f} bps к EMA, mid={mid:.6g}",
                impulse_bps=abs(dev_bps),
            )
        return None


class ScalpingRotate:
    """Composite: единый интерфейс on_quote над несколькими ScalpingStrategy; смена активного листа по таймеру."""

    def __init__(
        self,
        engines: list[Any],
        labels: list[str],
        interval_sec: float,
    ) -> None:
        if len(engines) != len(labels) or not engines:
            raise ValueError("rotate: нужен непустой список engines и совпадающие labels")
        self._engines = engines
        self._labels = labels
        self._interval = interval_sec
        self._idx = 0
        self._last_switch = time.monotonic()
        log.info("Скальпинг rotate: старт с %s", self._labels[0])

    @property
    def active_label(self) -> str:
        return self._labels[self._idx]

    @property
    def active_engine(self) -> Any:
        return self._engines[self._idx]

    async def refresh_ohlcv(self, scanner: Any, exchange_id: str, symbol: str) -> None:
        """Для TA: один REST fetch на первом движке с refresh_ohlcv, затем копия во все под-стратегии."""
        key = (exchange_id, symbol)
        fetcher = None
        for eng in self._engines:
            if getattr(eng, "refresh_ohlcv", None) is not None:
                fetcher = eng
                break
        if fetcher is None:
            return
        await fetcher.refresh_ohlcv(scanner, exchange_id, symbol)
        rows = None
        gf = getattr(fetcher, "get_ohlcv", None)
        if gf is not None:
            rows = gf(exchange_id, symbol)
        lf = getattr(fetcher, "_last_fetch", {}).get(key, 0.0)
        for eng in self._engines:
            if eng is fetcher:
                continue
            ostore = getattr(eng, "_ohlcv", None)
            if ostore is not None and rows:
                ostore[key] = rows
            lt = getattr(eng, "_last_fetch", None)
            if lt is not None:
                lt[key] = lf

    def get_last_atr_bps(self, exchange_id: str, symbol: str) -> float | None:
        g = getattr(self._engines[self._idx], "get_last_atr_bps", None)
        return g(exchange_id, symbol) if callable(g) else None

    def on_quote(self, q: Quote, order_book: dict | None = None) -> ScalpSignal | None:
        now = time.monotonic()
        if self._interval > 0 and len(self._engines) > 1 and now - self._last_switch >= self._interval:
            self._idx = (self._idx + 1) % len(self._engines)
            self._last_switch = now
            log.info("Скальпинг rotate → активна %s", self._labels[self._idx])
        return self._engines[self._idx].on_quote(q, order_book)


class ScalpingAdaptive:
    """Выбор под-стратегии по σ тиков (pstdev изменений mid, bps) и симметричной глубине стакана (quote).

    Без стакана маршрутизация только по σ. Первые тики — осторожный режим filtered_momentum.
    """

    def __init__(
        self,
        momentum: ScalpingMomentum,
        filtered: ScalpingFilteredMomentum,
        mean_rev: ScalpingMeanReversion,
        *,
        vol_window: int,
        depth_levels: int,
        depth_thin: float,
        depth_thick: float,
        vol_low_bps: float,
        vol_high_bps: float,
    ) -> None:
        self._m = momentum
        self._f = filtered
        self._r = mean_rev
        self._vol_window = max(3, vol_window)
        self._depth_levels = max(1, depth_levels)
        self._depth_thin = float(depth_thin)
        self._depth_thick = float(depth_thick)
        self._vol_low = float(vol_low_bps)
        self._vol_high = float(vol_high_bps)
        self._state: dict[tuple[str, str], dict[str, Any]] = {}
        self._last_pick: dict[tuple[str, str], str] = {}

    def _update_sigma(self, key: tuple[str, str], q: Quote) -> float:
        mid = (q.bid + q.ask) / 2.0
        if mid <= 0:
            return 0.0
        if key not in self._state:
            self._state[key] = {"last_mid": None, "dq": deque(maxlen=self._vol_window)}
        st = self._state[key]
        if st["dq"].maxlen != self._vol_window:
            st["dq"] = deque(st["dq"], maxlen=self._vol_window)
        last = st["last_mid"]
        if last is None:
            st["last_mid"] = mid
            return 0.0
        ch = (mid / last - 1.0) * 10_000.0
        st["last_mid"] = mid
        st["dq"].append(ch)
        dq = st["dq"]
        if len(dq) < 2:
            return 0.0
        return statistics.pstdev(dq)

    def _pick_engine_name(self, sigma: float, depth: float | None) -> str:
        vl, vh = self._vol_low, self._vol_high
        dt, dk = self._depth_thin, self._depth_thick

        v_cat = "mid"
        if sigma <= vl:
            v_cat = "low"
        elif sigma >= vh:
            v_cat = "high"

        if depth is None:
            if v_cat == "low":
                return "mean_reversion"
            if v_cat == "high":
                return "momentum"
            return "filtered_momentum"

        d_cat = "mid"
        if depth <= dt:
            d_cat = "thin"
        elif depth >= dk:
            d_cat = "thick"

        if v_cat == "low" and d_cat == "thick":
            return "mean_reversion"
        if v_cat == "low" and d_cat == "thin":
            return "filtered_momentum"
        if v_cat == "high" and d_cat == "thin":
            return "filtered_momentum"
        if v_cat == "high":
            return "momentum"
        if d_cat == "thin":
            return "filtered_momentum"
        return "momentum"

    def on_quote(self, q: Quote, order_book: dict | None = None) -> ScalpSignal | None:
        key = (q.exchange_id, q.symbol)
        sigma = self._update_sigma(key, q)
        st = self._state[key]
        if len(st["dq"]) < 3:
            return self._f.on_quote(q, order_book)

        depth = symmetric_liquidity_quote(order_book, self._depth_levels)
        name = self._pick_engine_name(sigma, depth)
        if self._last_pick.get(key) != name:
            self._last_pick[key] = name
            d_str = f"{depth:.0f}" if depth is not None else "—"
            log.info(
                "Скальпинг adaptive [%s]: %s (σ≈%.2f bps, min_depth_quote≈%s)",
                q.symbol,
                name,
                sigma,
                d_str,
            )

        eng = {"momentum": self._m, "filtered_momentum": self._f, "mean_reversion": self._r}[name]
        sig = eng.on_quote(q, order_book)
        if sig is not None:
            return ScalpSignal(
                side=sig.side,
                detail=f"[{name}] {sig.detail}",
                impulse_bps=sig.impulse_bps,
            )
        return None
