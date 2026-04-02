"""
Точка входа: цикл опроса бирж и вывод арбитражных возможностей.

По умолчанию только бумажный режим (логирование). Реальная торговля требует
ключей API, учёта вывода средств и задержек — это выходит за рамки MVP.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from config import Settings, load_settings
from scalping import ScalpingMeanReversion, ScalpingMomentum
from scanner import ArbitrageScanner

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stdout)


async def run_arbitrage_loop(scanner: ArbitrageScanner, settings: Settings, log: logging.Logger) -> None:
    while True:
        opportunities = await scanner.scan_once()
        for o in opportunities:
            if o.edge_after_fees_bps > 0:
                log.info(
                    "%s | купить %s @ %s → продать %s @ %s | "
                    "спред %.1f bps | после комиссий %.1f bps",
                    o.symbol,
                    o.buy_exchange,
                    o.buy_at,
                    o.sell_exchange,
                    o.sell_at,
                    o.spread_bps,
                    o.edge_after_fees_bps,
                )
            else:
                log.debug(
                    "%s лучший вариант после комиссий %.1f bps (нет профита)",
                    o.symbol,
                    o.edge_after_fees_bps,
                )
        await asyncio.sleep(settings.poll_seconds)


def _make_scalping_engine(settings: Settings):
    if settings.scalping_variant == "mean_reversion":
        return ScalpingMeanReversion(
            deviation_bps=settings.scalping_reversion_deviation_bps,
            ema_alpha=settings.scalping_ema_alpha,
            max_spread_bps=settings.scalping_max_spread_bps,
            reentry_bps=settings.scalping_reversion_reentry_bps,
        )
    return ScalpingMomentum(
        move_bps=settings.scalping_move_bps,
        max_spread_bps=settings.scalping_max_spread_bps,
    )


async def run_scalping_loop(scanner: ArbitrageScanner, settings: Settings, log: logging.Logger) -> None:
    engine = _make_scalping_engine(settings)
    exchange_id = settings.exchanges[0]
    while True:
        for sym in settings.symbols:
            q = await scanner.fetch_quote(exchange_id, sym)
            if q is None:
                continue
            msg = engine.on_quote(q)
            if msg:
                log.info("%s %s | %s", sym, exchange_id, msg)
        await asyncio.sleep(settings.poll_seconds)


async def run_loop() -> None:
    settings = load_settings()
    scanner = ArbitrageScanner(settings)
    log = logging.getLogger("arbitrage")

    log.info(
        "Старт: strategy=%s биржи=%s пары=%s комиссия=%s bps/сторона poll=%ss paper=%s",
        settings.strategy,
        settings.exchanges,
        settings.symbols,
        settings.fee_bps_per_side,
        settings.poll_seconds,
        settings.paper,
    )
    if settings.strategy == "scalping":
        if settings.scalping_variant == "mean_reversion":
            log.info(
                "Скальпинг mean_reversion: EMA α=%.3f отклонение>=%.1f bps, reentry<%.1f bps, max_spread=%s",
                settings.scalping_ema_alpha,
                settings.scalping_reversion_deviation_bps,
                settings.scalping_reversion_reentry_bps,
                settings.scalping_max_spread_bps,
            )
        else:
            log.info(
                "Скальпинг momentum: move>=%.1f bps, max_spread=%s",
                settings.scalping_move_bps,
                settings.scalping_max_spread_bps,
            )

    try:
        if settings.strategy == "scalping":
            await run_scalping_loop(scanner, settings, log)
        else:
            await run_arbitrage_loop(scanner, settings, log)
    finally:
        await scanner.close()


def main() -> None:
    setup_logging()
    try:
        asyncio.run(run_loop())
    except KeyboardInterrupt:
        logging.getLogger("arbitrage").info("Остановка по Ctrl+C")


if __name__ == "__main__":
    main()
