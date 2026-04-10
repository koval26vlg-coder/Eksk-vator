"""Paper PnL: шорт без предварительного long и покрытие покупкой."""

from paper_pnl import PaperSessionPnl


def test_short_then_cover_profitable() -> None:
    fee = 10.0
    p = PaperSessionPnl(fee)
    # Голая продажа 1 base @ 100 quote notional
    p.try_record_closed_order(
        {
            "id": "s1",
            "status": "closed",
            "filled": 1.0,
            "cum_quote": 100.0,
            "side": "sell",
            "exchange_id": "bybit",
            "symbol": "ETH/USDT",
        }
    )
    assert p.closed_sells == 1
    assert p.realized_pnl_quote == 0.0
    assert p.fees_paid_quote > 0
    # Покупка ниже — прибыль по шорту (net sell − net buy)
    p.try_record_closed_order(
        {
            "id": "b1",
            "status": "closed",
            "filled": 1.0,
            "cum_quote": 99.0,
            "side": "buy",
            "exchange_id": "bybit",
            "symbol": "ETH/USDT",
        }
    )
    assert p.closed_buys == 1
    # sell fee on 100 + buy fee on 99; PnL ≈ (proceeds_sell/base - cost_buy/base) * 1
    assert p.realized_pnl_quote > 0, "short cover at lower price should realize positive PnL"


def test_long_round_trip_unchanged() -> None:
    fee = 10.0
    p = PaperSessionPnl(fee)
    p.try_record_closed_order(
        {
            "id": "b1",
            "status": "closed",
            "filled": 1.0,
            "cum_quote": 100.0,
            "side": "buy",
            "exchange_id": "x",
            "symbol": "BTC/USDT",
        }
    )
    p.try_record_closed_order(
        {
            "id": "s1",
            "status": "closed",
            "filled": 1.0,
            "cum_quote": 101.0,
            "side": "sell",
            "exchange_id": "x",
            "symbol": "BTC/USDT",
        }
    )
    assert p.realized_pnl_quote > 0


if __name__ == "__main__":
    test_short_then_cover_profitable()
    test_long_round_trip_unchanged()
    print("OK")
