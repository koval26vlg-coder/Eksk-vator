"""
Проверка Bybit: публично (рынки + тикер), затем приватно (если ключи в .env).

Запуск: .venv\\Scripts\\python test_bybit.py

Ошибка 33004 от Bybit = ключ истёк или отозван — создайте новый в разделе API.
"""

from __future__ import annotations

import asyncio
import logging
import os

import ccxt.async_support as ccxt

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("test_bybit")


def _load_keys() -> tuple[str, str]:
    from dotenv import load_dotenv

    load_dotenv()
    key = (
        os.getenv("EXCHANGE_API_KEY", "").strip()
        or os.getenv("API_KEY", "").strip()
        or os.getenv("BYBIT_API_KEY", "").strip()
    )
    secret = (
        os.getenv("EXCHANGE_API_SECRET", "").strip()
        or os.getenv("API_SECRET", "").strip()
        or os.getenv("BYBIT_API_SECRET", "").strip()
    )
    return key, secret


async def main() -> None:
    sym = "BTC/USDT"

    ex_pub = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    try:
        await ex_pub.load_markets()
        if sym not in ex_pub.symbols:
            log.error("На spot нет пары %s", sym)
            return
        t = await ex_pub.fetch_ticker(sym)
        log.info("[публично] %s last=%s bid=%s ask=%s", sym, t.get("last"), t.get("bid"), t.get("ask"))
    finally:
        await ex_pub.close()

    key, secret = _load_keys()
    if not key or not secret:
        log.info("Приватный тест пропущен: нет BYBIT_API_KEY / BYBIT_API_SECRET в .env")
        return

    log.info("Приватный тест: ключ из .env задан (детали не логируются)")

    ex = ccxt.bybit(
        {
            "apiKey": key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
    )
    try:
        try:
            await ex.load_markets()
        except ccxt.AuthenticationError as e:
            log.error(
                "Bybit отклонил ключ (часто 33004 = ключ истёк). "
                "Создайте новый API key с правами Read (+ Trade если нужны ордера). Детали: %s",
                e,
            )
            return

        t = await ex.fetch_ticker(sym)
        log.info("[с ключом] тикер %s last=%s", sym, t.get("last"))

        if ex.has.get("fetchBalance"):
            try:
                bal = await ex.fetch_balance()
                usdt = bal.get("USDT") or bal.get("free", {}).get("USDT")
                if isinstance(usdt, dict):
                    log.info("[с ключом] USDT free=%s", usdt.get("free"))
                else:
                    log.info("[с ключом] баланс (фрагмент): %s", str(bal)[:200])
            except ccxt.AuthenticationError as e:
                log.error("Приватный запрос отклонён (ключ истёк или нет прав): %s", e)
            except Exception as e:
                log.warning("fetch_balance: %s", e)

        log.info("Приватная часть завершена без фатальной ошибки")
    finally:
        await ex.close()


if __name__ == "__main__":
    asyncio.run(main())
