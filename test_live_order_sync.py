"""Смоук-тест live on_tick: fetch_order → снятие из очереди, TTL cancel (без сети)."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from auto_trade import ScalpAutoTrader


async def _test_terminal_removes_pending() -> None:
    s = SimpleNamespace(
        auto_trade=True,
        paper=False,
        scalping_order_ttl_seconds=99999.0,
        scalping_reprice_bps=0.0,
        scalping_reprice_cooldown_seconds=1.0,
    )
    ex = MagicMock()
    ex.fetch_order = AsyncMock(
        return_value={
            "id": "1",
            "status": "closed",
            "remaining": 0.0,
            "filled": 0.1,
            "price": 100.0,
            "side": "buy",
        }
    )
    ex.update_open_notional_for_order = MagicMock()

    trader = ScalpAutoTrader(s, ex, logging.getLogger("t"))
    key = ("bybit", "BTC/USDT")
    trader._pending_order_ids[key] = ["1"]
    trader._live_meta["1"] = {
        "created_mono": 0.0,
        "last_reprice_mono": 0.0,
        "side": "buy",
        "price": 100.0,
        "symbol": key[1],
        "exchange_id": key[0],
    }

    ob = {"bids": [[99.0, 1.0]], "asks": [[101.0, 1.0]]}
    await trader.on_tick(key[0], key[1], ob)

    assert trader._pending_order_ids.get(key) == []
    assert "1" not in trader._live_meta
    ex.update_open_notional_for_order.assert_called()


async def _test_ttl_cancel() -> None:
    s = SimpleNamespace(
        auto_trade=True,
        paper=False,
        scalping_order_ttl_seconds=5.0,
        scalping_reprice_bps=0.0,
        scalping_reprice_cooldown_seconds=1.0,
    )
    ex = MagicMock()
    ex.fetch_order = AsyncMock(
        return_value={
            "id": "1",
            "status": "open",
            "remaining": 0.5,
            "filled": 0.0,
            "price": 100.0,
            "side": "buy",
        }
    )
    ex.cancel_order = AsyncMock(return_value=True)
    ex.update_open_notional_for_order = MagicMock()

    trader = ScalpAutoTrader(s, ex, logging.getLogger("t"))
    key = ("bybit", "BTC/USDT")
    trader._pending_order_ids[key] = ["1"]
    trader._live_meta["1"] = {
        "created_mono": 0.0,
        "last_reprice_mono": 0.0,
        "side": "buy",
        "price": 100.0,
        "symbol": key[1],
        "exchange_id": key[0],
    }

    ob = {"bids": [[99.0, 1.0]], "asks": [[101.0, 1.0]]}
    with patch("auto_trade.time.monotonic", return_value=10.0):
        await trader.on_tick(key[0], key[1], ob)

    assert trader._pending_order_ids.get(key) == []
    ex.cancel_order.assert_awaited_once()


async def _test_restore_from_exchange() -> None:
    s = SimpleNamespace(auto_trade=True, paper=False)
    ex = MagicMock()
    ex.fetch_open_orders = AsyncMock(
        return_value=[
            {
                "id": "a1",
                "status": "open",
                "remaining": 0.01,
                "filled": 0.0,
                "price": 50000.0,
                "side": "buy",
                "symbol": "BTC/USDT",
                "type": "limit",
                "timestamp": 1_700_000_000_000,
            }
        ]
    )
    ex.update_open_notional_for_order = MagicMock()

    trader = ScalpAutoTrader(s, ex, logging.getLogger("t"))
    n = await trader.restore_live_orders_from_exchange("bybit", ("BTC/USDT",))
    assert n == 1
    key = ("bybit", "BTC/USDT")
    assert trader._pending_order_ids[key] == ["a1"]
    assert trader._live_meta["a1"]["created_wall"] == 1_700_000_000_000 / 1000.0
    ex.update_open_notional_for_order.assert_called()
    ex.fetch_open_orders.assert_awaited()


async def _test_merge_skips_known_id() -> None:
    s = SimpleNamespace(auto_trade=True, paper=False)
    row = {
        "id": "a1",
        "status": "open",
        "remaining": 0.01,
        "filled": 0.0,
        "price": 50000.0,
        "side": "buy",
        "symbol": "BTC/USDT",
        "type": "limit",
        "timestamp": 1_700_000_000_000,
    }
    ex = MagicMock()
    ex.fetch_open_orders = AsyncMock(return_value=[row])
    ex.update_open_notional_for_order = MagicMock()

    trader = ScalpAutoTrader(s, ex, logging.getLogger("t"))
    key = ("bybit", "BTC/USDT")
    trader._pending_order_ids[key] = ["a1"]
    n = await trader.merge_open_orders_from_exchange("bybit")
    assert n == 0
    ex.update_open_notional_for_order.assert_not_called()


async def main() -> None:
    await _test_terminal_removes_pending()
    await _test_ttl_cancel()
    await _test_restore_from_exchange()
    await _test_merge_skips_known_id()
    print("OK live on_tick smoke")


if __name__ == "__main__":
    asyncio.run(main())
