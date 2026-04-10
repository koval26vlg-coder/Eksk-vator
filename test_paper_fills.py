import asyncio

from config import load_settings
from execution import OrderExecutor
from risk import RiskLimits, RiskManager
from auto_trade import ScalpAutoTrader


async def main() -> None:
    s = load_settings()
    limits = RiskLimits(
        max_notional_per_order=10_000,
        max_open_orders=100,
        max_total_notional_open=1_000_000,
    )
    risk = RiskManager(limits)
    ex = OrderExecutor(s, risk)
    trader = ScalpAutoTrader(s, ex, __import__("logging").getLogger("t"))

    # Лимитка buy по 100, хотим 2 base.
    ob1 = {"asks": [[101, 10]], "bids": [[99, 10]]}  # не исполняется
    o = await ex.place_limit("bybit", "BTC/USDT", "buy", 2.0, 100.0, order_book=ob1)
    assert o and o["filled"] == 0.0 and o["remaining"] == 2.0 and o["status"] == "open"
    trader._pending_order_ids[("bybit", "BTC/USDT")] = [o["id"]]

    # Следующий тик: часть стакана доступна по 100.
    ob2 = {"asks": [[100.0, 0.7], [100.0, 0.6], [101, 10]]}
    await trader.on_tick("bybit", "BTC/USDT", ob2)
    o2 = ex.get_paper_order(o["id"])
    assert o2 and 1.29 <= o2["filled"] <= 1.31 and 0.69 <= o2["remaining"] <= 0.71

    # Следующий тик: добиваем остаток.
    ob3 = {"asks": [[99.5, 10.0]]}
    await trader.on_tick("bybit", "BTC/USDT", ob3)
    o3 = ex.get_paper_order(o["id"])
    assert o3 and o3["status"] == "closed" and o3["remaining"] <= 1e-9

    print("OK paper fill progression", o3["filled"], o3["average"])


if __name__ == "__main__":
    asyncio.run(main())

