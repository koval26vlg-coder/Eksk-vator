"""Загрузка настроек арбитражного бота из переменных окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _split_csv(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    """strategy: arbitrage — межбиржа; scalping — импульс на одной бирже."""

    strategy: str
    exchanges: tuple[str, ...]
    symbols: tuple[str, ...]
    fee_bps_per_side: float
    poll_seconds: float
    paper: bool
    scalping_move_bps: float
    scalping_max_spread_bps: float | None
    scalping_variant: str
    scalping_ema_alpha: float
    scalping_reversion_deviation_bps: float
    scalping_reversion_reentry_bps: float


def load_settings() -> Settings:
    strategy = os.getenv("BOT_STRATEGY", "arbitrage").strip().lower()
    raw_ex = os.getenv("ARBITRAGE_EXCHANGES", "binance,bybit")
    raw_sym = os.getenv("ARBITRAGE_SYMBOLS", "BTC/USDT")
    fee = float(os.getenv("ARBITRAGE_FEE_BPS_PER_SIDE", "10"))
    poll = float(os.getenv("ARBITRAGE_POLL_SECONDS", "5"))
    paper = os.getenv("ARBITRAGE_PAPER", "true").lower() in ("1", "true", "yes", "on")

    move_bps = float(os.getenv("SCALPING_MOVE_BPS", "8"))
    raw_max_sp = os.getenv("SCALPING_MAX_SPREAD_BPS", "").strip()
    max_spread: float | None = float(raw_max_sp) if raw_max_sp else None

    variant = os.getenv("SCALPING_VARIANT", "momentum").strip().lower()
    ema_alpha = float(os.getenv("SCALPING_EMA_ALPHA", "0.15"))
    rev_dev = float(os.getenv("SCALPING_REVERSION_DEVIATION_BPS", "15"))
    raw_reentry = os.getenv("SCALPING_REVERSION_REENTRY_BPS", "").strip()
    rev_reentry = float(raw_reentry) if raw_reentry else max(rev_dev * 0.5, 3.0)

    sym_list = _split_csv(raw_sym)
    if not sym_list:
        raise ValueError("ARBITRAGE_SYMBOLS пусто")

    if strategy == "scalping":
        sex = os.getenv("SCALPING_EXCHANGE", "").strip()
        if sex:
            ex_list = [sex]
        else:
            ex_list = _split_csv(raw_ex)
            if not ex_list:
                raise ValueError("Для scalping укажите SCALPING_EXCHANGE или ARBITRAGE_EXCHANGES")
            ex_list = [ex_list[0]]
    else:
        ex_list = _split_csv(raw_ex)
        if not ex_list:
            raise ValueError("ARBITRAGE_EXCHANGES пусто")
        if len(ex_list) < 2:
            raise ValueError("Нужно минимум две биржи для межбиржевого арбитража")

    if strategy not in ("arbitrage", "scalping"):
        raise ValueError("BOT_STRATEGY должен быть arbitrage или scalping")
    if strategy == "scalping" and variant not in ("momentum", "mean_reversion"):
        raise ValueError("SCALPING_VARIANT: momentum или mean_reversion")

    return Settings(
        strategy=strategy,
        exchanges=tuple(ex_list),
        symbols=tuple(sym_list),
        fee_bps_per_side=fee,
        poll_seconds=poll,
        paper=paper,
        scalping_move_bps=move_bps,
        scalping_max_spread_bps=max_spread,
        scalping_variant=variant,
        scalping_ema_alpha=ema_alpha,
        scalping_reversion_deviation_bps=rev_dev,
        scalping_reversion_reentry_bps=rev_reentry,
    )
