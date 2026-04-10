"""Проверки RSI (Wilder) и ATR (Wilder) на синтетике."""

from ta_core import atr_last, rsi_last


def test_rsi_wilder_monotone_up() -> None:
    cl = [100.0 + i * 0.5 for i in range(30)]
    r = rsi_last(cl, 14)
    assert r is not None and r > 95.0


def test_rsi_wilder_flat() -> None:
    """Нулевые изменения → avg_loss=0 → по нашей формуле RSI=100 (как и в многих реализациях при отсутствии убытков)."""
    cl = [100.0] * 30
    r = rsi_last(cl, 14)
    assert r is not None and r >= 99.0


def test_atr_wilder_positive() -> None:
    ohlcv = []
    base = 100.0
    for i in range(25):
        ts = i * 60_000
        o = base + i * 0.1
        h = o + 0.5
        lo = o - 0.2
        c = o + 0.05
        ohlcv.append([ts, o, h, lo, c, 1000.0])
    a = atr_last(ohlcv, 14)
    assert a is not None and a > 0


if __name__ == "__main__":
    test_rsi_wilder_monotone_up()
    test_rsi_wilder_flat()
    test_atr_wilder_positive()
    print("OK")
