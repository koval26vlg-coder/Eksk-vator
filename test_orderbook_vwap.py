"""Тесты VWAP по стакану для paper exit / MAX_HOLD gate."""

from orderbook import vwap_buy_base, vwap_sell_base


def test_vwap_buy_base_complete_partial():
    asks = [[100.0, 0.5], [101.0, 0.5]]
    spent, vwap, bought, ok = vwap_buy_base(asks, 0.8)
    assert ok
    assert abs(bought - 0.8) < 1e-9
    assert abs(spent - (0.5 * 100 + 0.3 * 101)) < 1e-9
    assert abs(vwap - spent / bought) < 1e-9


def test_vwap_buy_base_incomplete():
    asks = [[100.0, 0.2]]
    spent, vwap, bought, ok = vwap_buy_base(asks, 1.0)
    assert not ok
    assert abs(bought - 0.2) < 1e-9


def test_vwap_sell_base_symmetric():
    bids = [[99.0, 0.5], [98.0, 1.0]]
    quote, vwap, sold, ok = vwap_sell_base(bids, 0.8)
    assert ok
    assert abs(sold - 0.8) < 1e-9
    assert abs(quote - (0.5 * 99 + 0.3 * 98)) < 1e-9
    assert abs(vwap - quote / sold) < 1e-9
