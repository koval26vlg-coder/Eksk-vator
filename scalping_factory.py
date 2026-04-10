"""Фабрика движков скальпинга (паттерн Factory Method / Simple Factory).

Варианты из SCALPING_VARIANT и составные режимы rotate / ta_rotate / adaptive собираются здесь."""

from __future__ import annotations

from typing import Any

from config import Settings
from scalping import (
    ScalpingAdaptive,
    ScalpingFilteredMomentum,
    ScalpingMeanReversion,
    ScalpingMomentum,
    ScalpingRotate,
)
from scalping_ta import (
    ScalpingTAAggressive,
    ScalpingTAConservative,
    ScalpingTARegime,
    ScalpingTATrend,
)


def engine_by_variant(settings: Settings, variant: str) -> Any:
    """Один именованный под-вариант (momentum, ta_regime, mean_reversion, …)."""
    v = variant.strip().lower()
    if v == "ta_regime":
        return ScalpingTARegime(settings)
    if v == "ta_conservative":
        return ScalpingTAConservative(settings)
    if v == "ta_aggressive":
        return ScalpingTAAggressive(settings)
    if v == "ta_trend":
        return ScalpingTATrend(settings)
    if v == "mean_reversion":
        return ScalpingMeanReversion(
            deviation_bps=settings.scalping_reversion_deviation_bps,
            ema_alpha=settings.scalping_ema_alpha,
            max_spread_bps=settings.scalping_max_spread_bps,
            reentry_bps=settings.scalping_reversion_reentry_bps,
        )
    if v == "filtered_momentum":
        return ScalpingFilteredMomentum(
            move_bps=settings.scalping_move_bps,
            max_spread_bps=settings.scalping_max_spread_bps,
            vol_window=settings.scalping_vol_window,
            min_micro_vol_bps=settings.scalping_min_micro_vol_bps,
            max_micro_vol_bps=settings.scalping_max_micro_vol_bps,
        )
    return ScalpingMomentum(
        move_bps=settings.scalping_move_bps,
        max_spread_bps=settings.scalping_max_spread_bps,
    )


def make_scalping_engine(settings: Settings) -> Any:
    """Движок по `settings.scalping_variant`: простой, rotate, ta_rotate или adaptive."""
    if settings.scalping_variant == "rotate":
        engines = [engine_by_variant(settings, name) for name in settings.scalping_rotate_variants]
        return ScalpingRotate(
            engines=engines,
            labels=list(settings.scalping_rotate_variants),
            interval_sec=settings.scalping_rotate_interval_seconds,
        )
    if settings.scalping_variant == "ta_rotate":
        engines = [engine_by_variant(settings, name) for name in settings.scalping_ta_rotate_variants]
        return ScalpingRotate(
            engines=engines,
            labels=list(settings.scalping_ta_rotate_variants),
            interval_sec=settings.scalping_rotate_interval_seconds,
        )
    if settings.scalping_variant == "adaptive":
        return ScalpingAdaptive(
            momentum=engine_by_variant(settings, "momentum"),
            filtered=engine_by_variant(settings, "filtered_momentum"),
            mean_rev=engine_by_variant(settings, "mean_reversion"),
            vol_window=settings.scalping_vol_window,
            depth_levels=settings.scalping_adaptive_depth_levels,
            depth_thin=settings.scalping_adaptive_depth_thin,
            depth_thick=settings.scalping_adaptive_depth_thick,
            vol_low_bps=settings.scalping_adaptive_vol_low_bps,
            vol_high_bps=settings.scalping_adaptive_vol_high_bps,
        )
    return engine_by_variant(settings, settings.scalping_variant)


class ScalpingEngineFactory:
    """ОО-обёртка над функциями фабрики (удобно для тестов и расширения)."""

    @staticmethod
    def from_variant(settings: Settings, variant: str) -> Any:
        return engine_by_variant(settings, variant)

    @staticmethod
    def from_settings(settings: Settings) -> Any:
        return make_scalping_engine(settings)
