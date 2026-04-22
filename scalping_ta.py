"""Стратегии на OHLCV: SMA+RSI+BB (консерв.), SMA+VWAP+VP (агресс.), SMA+ADX+ATR (тренд), ta_regime — иерархия ADX/BB или выбор сильнейшего сигнала.

Свечи подгружаются REST (см. refresh_ohlcv в ws_stream / main). Индикаторы RSI/ATR — сглаживание Wilder; VWAP — скользящий по последним барам. Volume Profile — упрощённый POC по бинам OHLCV, не tape."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from config import Settings
from scanner import Quote
from scalping import ScalpSignal
from ta_core import (
    adx_dmi_last,
    atr_last,
    bb_width_bps,
    bollinger,
    closes,
    impulse_bps_mid_to_ref,
    impulse_bps_mid_to_sma,
    rsi_last,
    sma,
    vwap_session,
    volume_profile_poc,
)

log = logging.getLogger(__name__)

Mode = Literal["conservative", "aggressive", "trend"]

# При равном impulse_bps в режиме best_signal — приоритет режима
_BEST_SIGNAL_PRIO = {"trend": 3, "conservative": 2, "aggressive": 1}


def _spread_bps(q: Quote) -> float:
    mid = (q.bid + q.ask) / 2.0
    if mid <= 0:
        return float("inf")
    return (q.ask - q.bid) / mid * 10_000.0


@dataclass
class ScalpingTAOhlcv:
    """Кэш OHLCV с троттлингом REST; вызывать refresh_ohlcv перед on_quote."""

    settings: Settings
    _last_fetch: dict[tuple[str, str], float] = field(default_factory=dict)
    _ohlcv: dict[tuple[str, str], list[Any]] = field(default_factory=dict)
    _logged_ohlcv_ok: set[tuple[str, str]] = field(default_factory=set)
    _warned_ohlcv_fail: set[tuple[str, str]] = field(default_factory=set)
    #: Общий кэш пакета индикаторов на (биржа, пара) — по сигнатуре последней свечи (для ta_regime дети делят с родителем).
    _ta_ind_block: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    #: Дедуп сигналов: (ex, sym, side) → ts последней свечи.
    _emit_bar: dict[tuple[str, str, str], float] = field(default_factory=dict)

    async def refresh_ohlcv(self, scanner: Any, exchange_id: str, symbol: str) -> None:
        key = (exchange_id, symbol)
        now = time.monotonic()
        ttl = float(self.settings.ta_ohlcv_refresh_seconds)
        if now - self._last_fetch.get(key, 0.0) < ttl:
            return
        rows = await scanner.fetch_ohlcv(
            exchange_id, symbol, self.settings.ta_timeframe, self.settings.ta_ohlcv_limit
        )
        if rows:
            self._ohlcv[key] = rows
            self._last_fetch[key] = now
            if key not in self._logged_ohlcv_ok:
                self._logged_ohlcv_ok.add(key)
                log.info(
                    "TA OHLCV: %s свечей %s %s tf=%s (индикаторы обновляются по REST)",
                    len(rows),
                    exchange_id,
                    symbol,
                    self.settings.ta_timeframe,
                )
        else:
            if key not in self._warned_ohlcv_fail:
                self._warned_ohlcv_fail.add(key)
                log.warning(
                    "TA OHLCV: нет данных %s %s tf=%s — сигналов не будет, проверьте пару и сеть",
                    exchange_id,
                    symbol,
                    self.settings.ta_timeframe,
                )

    def get_ohlcv(self, exchange_id: str, symbol: str) -> list[Any] | None:
        return self._ohlcv.get((exchange_id, symbol))

    def get_last_atr_bps(self, exchange_id: str, symbol: str) -> float | None:
        """ATR в bps к цене закрытия последней свечи (для AUTO_TRADE фильтров); None если нет кэша TA."""
        ohlcv = self.get_ohlcv(exchange_id, symbol)
        if not ohlcv:
            return None
        key = (exchange_id, symbol)
        blk = self._ta_ind_block.get(key)
        sig = (len(ohlcv), float(ohlcv[-1][0]))
        if not blk or blk.get("_sig") != sig:
            return None
        atr = blk.get("atr")
        if atr is None:
            return None
        c = float(ohlcv[-1][4])
        if c <= 0:
            return None
        return atr / c * 10_000.0

    def _ta_bundle(self, exchange_id: str, symbol: str, ohlcv: list[Any]) -> dict[str, Any]:
        """Один расчёт RSI/BB/SMA/ADX/ATR/VWAP/VP на смену последней свечи (кэш по ts)."""
        key = (exchange_id, symbol)
        sig = (len(ohlcv), float(ohlcv[-1][0]))
        blk = self._ta_ind_block.get(key)
        if blk and blk.get("_sig") == sig:
            return blk
        s = self.settings
        cl = closes(ohlcv)
        blk = {
            "_sig": sig,
            "cl": cl,
            "bb": bollinger(cl, s.ta_bb_period, s.ta_bb_std),
            "rsi": rsi_last(cl, s.ta_rsi_period),
            "sma": sma(cl, s.ta_sma_period),
            "adx": adx_dmi_last(ohlcv, s.ta_adx_period),
            "atr": atr_last(ohlcv, s.ta_atr_period),
            "vw": vwap_session(ohlcv, s.ta_vwap_bars),
            "vp": volume_profile_poc(ohlcv, s.ta_vp_bins),
        }
        self._ta_ind_block[key] = blk
        return blk

    def _emit_signal(self, q: Quote, ohlcv: list[Any], sig: ScalpSignal | None) -> ScalpSignal | None:
        if sig is None:
            return None
        if self.settings.ta_signal_one_per_bar:
            bts = float(ohlcv[-1][0])
            ek = (q.exchange_id, q.symbol, sig.side)
            if self._emit_bar.get(ek) == bts:
                return None
            self._emit_bar[ek] = bts
        return sig


class ScalpingTAConservative(ScalpingTAOhlcv):
    """SMA + RSI + Bollinger: отскок от полос (mean reversion). Импульс — отклонение mid от середины BB."""

    def raw_signal(self, q: Quote, ohlcv: list[Any], blk: dict[str, Any]) -> ScalpSignal | None:
        bb = blk["bb"]
        rsi = blk["rsi"]
        if bb is None or rsi is None:
            return None
        upper, mid_bb, lower = bb
        mid = (q.bid + q.ask) / 2.0
        lo_th, hi_th = self.settings.ta_rsi_oversold, self.settings.ta_rsi_overbought
        near = float(self.settings.ta_band_near_bps)
        d_lower_bps = (mid - lower) / mid * 10_000.0 if mid > 0 else 9999.0
        d_upper_bps = (upper - mid) / mid * 10_000.0 if mid > 0 else 9999.0
        if rsi <= lo_th and abs(d_lower_bps) <= near:
            imp = max(
                impulse_bps_mid_to_ref(mid, mid_bb) or 0.0,
                abs(d_lower_bps),
                5.0,
            )
            return ScalpSignal(
                side="buy",
                detail=f"TA cons: LONG у lower BB (|Δ|={abs(d_lower_bps):.1f}≤{near:.0f} bps), RSI={rsi:.1f}",
                impulse_bps=float(imp),
            )
        return None

    def on_quote(self, q: Quote, order_book: dict | None = None) -> ScalpSignal | None:
        ms = self.settings.scalping_max_spread_bps
        if ms is not None and _spread_bps(q) > ms:
            return None
        ohlcv = self.get_ohlcv(q.exchange_id, q.symbol)
        if not ohlcv or len(ohlcv) < max(
            self.settings.ta_bb_period, self.settings.ta_rsi_period
        ) + 2:
            return None
        blk = self._ta_bundle(q.exchange_id, q.symbol, ohlcv)
        sig = self.raw_signal(q, ohlcv, blk)
        return self._emit_signal(q, ohlcv, sig)


class ScalpingTAAggressive(ScalpingTAOhlcv):
    """SMA + VWAP + Volume Profile (POC): импульс через уровни объёма и VWAP."""

    def raw_signal(self, q: Quote, ohlcv: list[Any], blk: dict[str, Any]) -> ScalpSignal | None:
        sma_s = blk["sma"]
        vw = blk["vw"]
        vp = blk["vp"]
        if sma_s is None or vw is None or vp is None:
            return None
        poc, _tot_v = vp
        mid = (q.bid + q.ask) / 2.0
        eps = self.settings.ta_aggr_eps_bps / 10_000.0
        req_poc = self.settings.ta_aggr_require_poc
        long_vw = mid > vw * (1 + eps) and mid > sma_s
        long_poc_ok = mid > poc * (1 + eps)
        short_vw = mid < vw * (1 - eps) and mid < sma_s
        short_poc_ok = mid < poc * (1 - eps)
        if long_vw and (long_poc_ok or not req_poc):
            imp = max(
                impulse_bps_mid_to_sma(mid, sma_s) or 0.0,
                abs(mid - vw) / mid * 10_000.0,
                5.0,
            )
            return ScalpSignal(
                side="buy",
                detail=f"TA aggr: LONG выше VWAP+SMA (VWAP≈{vw:.6g} POC≈{poc:.6g} req_poc={req_poc})",
                impulse_bps=float(imp),
            )
        if short_vw and (short_poc_ok or not req_poc):
            imp = max(
                impulse_bps_mid_to_sma(mid, sma_s) or 0.0,
                abs(mid - vw) / mid * 10_000.0,
                5.0,
            )
            return ScalpSignal(
                side="sell",
                detail=f"TA aggr: SHORT ниже VWAP+SMA (VWAP≈{vw:.6g} POC≈{poc:.6g} req_poc={req_poc})",
                impulse_bps=float(imp),
            )
        return None

    def on_quote(self, q: Quote, order_book: dict | None = None) -> ScalpSignal | None:
        ms = self.settings.scalping_max_spread_bps
        if ms is not None and _spread_bps(q) > ms:
            return None
        ohlcv = self.get_ohlcv(q.exchange_id, q.symbol)
        if not ohlcv or len(ohlcv) < self.settings.ta_sma_period + 2:
            return None
        blk = self._ta_bundle(q.exchange_id, q.symbol, ohlcv)
        sig = self.raw_signal(q, ohlcv, blk)
        return self._emit_signal(q, ohlcv, sig)


class ScalpingTATrend(ScalpingTAOhlcv):
    """SMA + ADX + ATR: направление по DI и SMA, сила тренда по ADX."""

    def raw_signal(self, q: Quote, ohlcv: list[Any], blk: dict[str, Any]) -> ScalpSignal | None:
        adx_pack = blk["adx"]
        sma_s = blk["sma"]
        atr = blk["atr"]
        if adx_pack is None or sma_s is None or atr is None:
            return None
        adx, pdi, mdi = adx_pack
        if adx < self.settings.ta_trend_adx_trigger:
            return None
        mind = float(self.settings.ta_trend_min_di_diff)
        if mind > 0 and abs(pdi - mdi) < mind:
            return None
        mid = (q.bid + q.ask) / 2.0
        atr_bps = atr / mid * 10_000.0 if mid > 0 else 0.0
        rsi = blk.get("rsi")
        if pdi > mdi and mid > sma_s:
            mx_rsi = float(self.settings.ta_trend_max_rsi_long)
            if mx_rsi > 0 and rsi is not None and float(rsi) > mx_rsi:
                return None
            mx_ext = float(self.settings.ta_trend_max_extend_bps_long)
            if mx_ext > 0 and mid > 0:
                extend_bps = (mid - sma_s) / mid * 10_000.0
                if extend_bps > mx_ext:
                    return None
            imp = max(
                impulse_bps_mid_to_sma(mid, sma_s) or 0.0,
                abs(pdi - mdi),
                atr_bps * 0.1,
                5.0,
            )
            return ScalpSignal(
                side="buy",
                detail=f"TA trend: LONG ADX={adx:.1f} +DI>{mdi:.1f} mid>SMA ATR≈{atr_bps:.1f}bps",
                impulse_bps=float(imp),
            )
        if mdi > pdi and mid < sma_s:
            mn_rsi = float(self.settings.ta_trend_min_rsi_short)
            if mn_rsi > 0 and rsi is not None and float(rsi) < mn_rsi:
                return None
            mx_ext_s = float(self.settings.ta_trend_max_extend_bps_short)
            if mx_ext_s > 0 and mid > 0:
                extend_bps = (sma_s - mid) / mid * 10_000.0
                if extend_bps > mx_ext_s:
                    return None
            imp = max(
                impulse_bps_mid_to_sma(mid, sma_s) or 0.0,
                abs(pdi - mdi),
                atr_bps * 0.1,
                5.0,
            )
            return ScalpSignal(
                side="sell",
                detail=f"TA trend: SHORT ADX={adx:.1f} mdi={mdi:.1f}>pdi={pdi:.1f} mid<SMA ATR≈{atr_bps:.1f}bps",
                impulse_bps=float(imp),
            )
        return None

    def on_quote(self, q: Quote, order_book: dict | None = None) -> ScalpSignal | None:
        ms = self.settings.scalping_max_spread_bps
        if ms is not None and _spread_bps(q) > ms:
            return None
        ohlcv = self.get_ohlcv(q.exchange_id, q.symbol)
        if not ohlcv or len(ohlcv) < 50:
            return None
        blk = self._ta_bundle(q.exchange_id, q.symbol, ohlcv)
        sig = self.raw_signal(q, ohlcv, blk)
        return self._emit_signal(q, ohlcv, sig)


class ScalpingTARegime(ScalpingTAOhlcv):
    """ta_regime: hierarchy — выбор режима по ADX/ширине BB; best_signal — один сигнал с макс. импульсом при согласованной стороне."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._cons = ScalpingTAConservative(settings)
        self._aggr = ScalpingTAAggressive(settings)
        self._trend = ScalpingTATrend(settings)
        self._last_mode: dict[tuple[str, str], Mode | None] = {}
        shared_blk: dict[tuple[str, str], dict[str, Any]] = {}
        shared_emit: dict[tuple[str, str, str], float] = {}
        for eng in (self, self._cons, self._aggr, self._trend):
            eng._ta_ind_block = shared_blk
            eng._emit_bar = shared_emit

    def _sync_children_ohlcv(self, exchange_id: str, symbol: str) -> None:
        ohlcv = self.get_ohlcv(exchange_id, symbol)
        if not ohlcv:
            return
        key = (exchange_id, symbol)
        self._cons._ohlcv[key] = ohlcv
        self._aggr._ohlcv[key] = ohlcv
        self._trend._ohlcv[key] = ohlcv
        self._cons._last_fetch[key] = self._last_fetch.get(key, 0.0)
        self._aggr._last_fetch[key] = self._last_fetch.get(key, 0.0)
        self._trend._last_fetch[key] = self._last_fetch.get(key, 0.0)

    def _on_quote_best_signal(self, q: Quote, ohlcv: list[Any], blk: dict[str, Any]) -> ScalpSignal | None:
        ms = self.settings.scalping_max_spread_bps
        if ms is not None and _spread_bps(q) > ms:
            return None
        sig_t = self._trend.raw_signal(q, ohlcv, blk)
        sig_c = self._cons.raw_signal(q, ohlcv, blk)
        sig_a = (
            self._aggr.raw_signal(q, ohlcv, blk)
            if bool(getattr(self.settings, "ta_regime_allow_aggressive", True))
            else None
        )
        cands: list[tuple[ScalpSignal, Mode]] = []
        if sig_t is not None:
            cands.append((sig_t, "trend"))
        if sig_c is not None:
            cands.append((sig_c, "conservative"))
        if sig_a is not None:
            cands.append((sig_a, "aggressive"))
        if not cands:
            return None
        sides = {s.side for s, _ in cands}
        if len(sides) > 1:
            log.debug(
                "TA regime best_signal: конфликт сторон (%s) — пропуск",
                sides,
            )
            return None
        best_sig, best_mode = max(
            cands,
            key=lambda x: (x[0].impulse_bps, _BEST_SIGNAL_PRIO[x[1]]),
        )
        return self._emit_signal(
            q,
            ohlcv,
            ScalpSignal(
                side=best_sig.side,
                detail=f"[best:{best_mode}] {best_sig.detail}",
                impulse_bps=best_sig.impulse_bps,
            ),
        )

    def on_quote(self, q: Quote, order_book: dict | None = None) -> ScalpSignal | None:
        ohlcv = self.get_ohlcv(q.exchange_id, q.symbol)
        if not ohlcv or len(ohlcv) < max(50, self.settings.ta_bb_period + 5):
            return None
        self._sync_children_ohlcv(q.exchange_id, q.symbol)
        blk = self._ta_bundle(q.exchange_id, q.symbol, ohlcv)

        if self.settings.ta_regime_mode == "best_signal":
            return self._on_quote_best_signal(q, ohlcv, blk)

        adx_pack = blk["adx"]
        bb = blk["bb"]
        key = (q.exchange_id, q.symbol)
        if adx_pack is None or bb is None:
            return None
        adx = adx_pack[0]
        u, m, lo = bb
        bw = bb_width_bps(u, lo, m)
        mode: Mode
        if adx >= self.settings.ta_regime_adx_trend:
            mode = "trend"
        elif bw is not None and bw <= self.settings.ta_regime_bb_squeeze_bps:
            mode = "conservative"
        else:
            mode = "aggressive"
        if mode == "aggressive" and not bool(getattr(self.settings, "ta_regime_allow_aggressive", True)):
            mode = "conservative"
        if self._last_mode.get(key) != mode:
            self._last_mode[key] = mode
            log.info("TA regime [%s]: режим → %s", q.symbol, mode)

        eng = {"conservative": self._cons, "aggressive": self._aggr, "trend": self._trend}[mode]
        sig = eng.on_quote(q, order_book)
        if sig is None:
            return None
        return ScalpSignal(
            side=sig.side,
            detail=f"[{mode}] {sig.detail}",
            impulse_bps=sig.impulse_bps,
        )
