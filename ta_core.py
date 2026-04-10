"""Чистые функции для SMA, RSI, Bollinger, VWAP, VP (POC), ATR, ADX по рядам OHLCV CCXT.

Формат свечи: [timestamp_ms, open, high, low, close, volume]."""

from __future__ import annotations

import statistics
from typing import Sequence

Row = Sequence[float]


def closes(ohlcv: list[Row]) -> list[float]:
    return [float(r[4]) for r in ohlcv]


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period or period < 1:
        return None
    return sum(values[-period:]) / float(period)


def rsi_last(closes_: list[float], period: int = 14) -> float | None:
    """RSI (Wilder / RMA сглаживание приростов и убытков), последнее значение — как в классических терминалах."""
    n = len(closes_)
    if n < period + 1 or period < 2:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, n):
        ch = closes_[i] - closes_[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    if len(gains) < period:
        return None
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for j in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[j]) / period
        avg_l = (avg_l * (period - 1) + losses[j]) / period
    if avg_l < 1e-18:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


def bollinger(
    closes_: list[float], period: int = 20, num_std: float = 2.0
) -> tuple[float, float, float] | None:
    if len(closes_) < period or period < 2:
        return None
    window = closes_[-period:]
    mid = sum(window) / period
    sd = statistics.pstdev(window) if len(window) > 1 else 0.0
    u = mid + num_std * sd
    lo = mid - num_std * sd
    return (u, mid, lo)


def true_range(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr_last(ohlcv: list[Row], period: int = 14) -> float | None:
    """ATR (Wilder / RMA по TR), последнее значение."""
    if len(ohlcv) < period + 1 or period < 1:
        return None
    trs: list[float] = []
    for i in range(1, len(ohlcv)):
        h, lo = float(ohlcv[i][2]), float(ohlcv[i][3])
        pc = float(ohlcv[i - 1][4])
        trs.append(true_range(h, lo, pc))
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for j in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[j]) / period
    return atr


def _smooth_wilder(series: list[float], period: int) -> list[float]:
    if len(series) < period:
        return []
    out = [sum(series[:period]) / period]
    for i in range(period, len(series)):
        out.append((out[-1] * (period - 1) + series[i]) / period)
    return out


def adx_dmi_last(ohlcv: list[Row], period: int = 14) -> tuple[float, float, float] | None:
    """Возвращает (ADX, +DI, -DI) по последней закрытой позиции в серии."""
    if len(ohlcv) < 2 * period + 3:
        return None
    highs = [float(r[2]) for r in ohlcv]
    lows = [float(r[3]) for r in ohlcv]
    closes_ = [float(r[4]) for r in ohlcv]

    tr_list: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, len(ohlcv)):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        p_dm = up_move if up_move > down_move and up_move > 0 else 0.0
        m_dm = down_move if down_move > up_move and down_move > 0 else 0.0
        tr = true_range(highs[i], lows[i], closes_[i - 1])
        tr_list.append(tr)
        plus_dm.append(p_dm)
        minus_dm.append(m_dm)

    if len(tr_list) < period * 2:
        return None

    tr14 = _smooth_wilder(tr_list, period)
    p14 = _smooth_wilder(plus_dm, period)
    m14 = _smooth_wilder(minus_dm, period)
    if not tr14 or len(tr14) < 2:
        return None

    idx = -1
    tr_s = tr14[idx]
    if tr_s < 1e-18:
        return None
    p_di = 100.0 * p14[idx] / tr_s
    m_di = 100.0 * m14[idx] / tr_s
    dx = abs(p_di - m_di) / max(p_di + m_di, 1e-18) * 100.0

    dx_series: list[float] = []
    for j in range(len(tr14)):
        t = tr14[j]
        if t < 1e-18:
            continue
        pdi = 100.0 * p14[j] / t
        mdi = 100.0 * m14[j] / t
        dx_series.append(abs(pdi - mdi) / max(pdi + mdi, 1e-18) * 100.0)
    if len(dx_series) < period:
        adx = dx
    else:
        adx_vals = _smooth_wilder(dx_series, period)
        adx = adx_vals[-1] if adx_vals else dx

    return (adx, p_di, m_di)


def vwap_session(ohlcv: list[Row], bars: int | None = None) -> float | None:
    """Скользящий VWAP по типичной цене × объём за последние `bars` свечей (не календарная сессия биржи)."""
    if not ohlcv:
        return None
    rows = ohlcv[-bars:] if bars and bars > 0 else ohlcv
    if not rows:
        return None
    num = 0.0
    den = 0.0
    for r in rows:
        h, lo, c, v = float(r[2]), float(r[3]), float(r[4]), float(r[5])
        if v <= 0:
            continue
        tp = (h + lo + c) / 3.0
        num += tp * v
        den += v
    if den < 1e-18:
        return None
    return num / den


def volume_profile_poc(ohlcv: list[Row], bins: int = 24) -> tuple[float, float] | None:
    """(POC price, total volume в окне). POC — центр бина с макс. объёмом."""
    if not ohlcv or bins < 4:
        return None
    lows = [float(r[3]) for r in ohlcv]
    highs = [float(r[2]) for r in ohlcv]
    vols = [float(r[5]) for r in ohlcv]
    lo = min(lows)
    hi = max(highs)
    if hi <= lo:
        return ((lo + hi) / 2.0, sum(vols))

    step = (hi - lo) / float(bins)
    if step < 1e-18:
        return ((lo + hi) / 2.0, sum(vols))

    acc = [0.0] * bins
    for i, r in enumerate(ohlcv):
        tp = (float(r[2]) + float(r[3]) + float(r[4])) / 3.0
        b = int((tp - lo) / step)
        b = max(0, min(bins - 1, b))
        acc[b] += vols[i]
    mx = max(acc)
    if mx < 1e-18:
        return ((lo + hi) / 2.0, sum(vols))
    ib = acc.index(mx)
    poc = lo + (ib + 0.5) * step
    return (poc, sum(vols))


def bb_width_bps(upper: float, lower: float, mid: float) -> float | None:
    if mid <= 0:
        return None
    return (upper - lower) / mid * 10_000.0


def impulse_bps_mid_to_ref(mid: float, ref_price: float | None) -> float | None:
    """Отклонение mid котировки от опорной цены (SMA, середина BB, VWAP, …) в bps."""
    if ref_price is None or mid <= 0 or ref_price <= 0:
        return None
    return abs(mid / ref_price - 1.0) * 10_000.0


def impulse_bps_mid_to_sma(mid: float, sma_val: float | None) -> float | None:
    """Совместимость: то же, что impulse_bps_mid_to_ref к SMA."""
    return impulse_bps_mid_to_ref(mid, sma_val)
