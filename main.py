"""
Точка входа: REST или WebSocket (стакан), арбитраж / скальпинг, риск и исполнение.

По умолчанию бумажный режим. Live-ордера только с ключами и ARBITRAGE_PAPER=false.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

from auto_trade import ArbAutoTrader, ScalpAutoTrader
from config import Settings, load_settings
from execution_bootstrap import build_trading_execution
from execution import OrderExecutor
from scalping import ScalpSignalLogDeduper, ScalpingFilteredMomentum
from scalping_factory import make_scalping_engine
from scalping_orderflow import OrderflowTapeStore
from orderbook import format_spread_bid_ask, spread_bps
from scanner import ArbitrageScanner
from ws_stream import run_arbitrage_ws, run_scalping_ws

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _atr_bps_provider_from_engine(engine: object):
    """Для AUTO_TRADE: ATR (bps) с TA-движка, если есть get_last_atr_bps."""

    def _f(exchange_id: str, symbol: str) -> float | None:
        g = getattr(engine, "get_last_atr_bps", None)
        if callable(g):
            return g(exchange_id, symbol)
        return None

    return _f


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stdout)


async def run_arbitrage_loop(
    scanner: ArbitrageScanner,
    settings: Settings,
    log: logging.Logger,
    arb_trader: ArbAutoTrader | None,
) -> None:
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
                if arb_trader:
                    await arb_trader.on_opportunity(o)
            else:
                log.debug(
                    "%s лучший вариант после комиссий %.1f bps (нет профита)",
                    o.symbol,
                    o.edge_after_fees_bps,
                )
        await asyncio.sleep(settings.poll_seconds)


def _scalping_uses_ta(settings: Settings) -> bool:
    return settings.scalping_variant.startswith("ta_")


def _settings_need_orderflow_store(settings: Settings) -> bool:
    """Нужен буфер ленты (и движок orderflow), если вариант или шаг ротации — orderflow."""
    if settings.scalping_variant == "orderflow":
        return True
    if settings.scalping_variant == "rotate" and "orderflow" in settings.scalping_rotate_variants:
        return True
    if settings.scalping_variant == "ta_rotate" and "orderflow" in settings.scalping_ta_rotate_variants:
        return True
    return False


def _settings_need_scalping_order_book(settings: Settings) -> bool:
    """Глубина стакана для auto_trade/adaptive или для под-варианта orderflow (в т.ч. в ротации)."""
    if settings.auto_trade or settings.scalping_variant == "adaptive":
        return True
    return _settings_need_orderflow_store(settings)


def _rest_filtered_diag(settings: Settings, engine: object, exchange_id: str, sym: str) -> str | None:
    key = (exchange_id, sym)
    if settings.scalping_variant != "filtered_momentum":
        return None
    if isinstance(engine, ScalpingFilteredMomentum):
        return engine.rest_tick_diag(key)
    return None


async def run_scalping_loop(
    scanner: ArbitrageScanner,
    settings: Settings,
    log: logging.Logger,
    scalp_trader: ScalpAutoTrader | None,
    engine: object,
) -> None:
    strat_syms = set(settings.symbols)
    last_hb = 0.0
    sig_log_deduper = ScalpSignalLogDeduper(stale_seconds=settings.scalping_signal_log_fp_stale_seconds)
    while True:
        snap: list[str] = []
        diag: list[str] = []
        for exchange_id in settings.exchanges:
            if scalp_trader and settings.auto_trade and not settings.paper:
                await scalp_trader.merge_open_orders_from_exchange(exchange_id)
            pending_syms = (
                scalp_trader.symbols_with_pending_orders(exchange_id) if scalp_trader else set()
            )
            for sym in sorted(strat_syms | pending_syms):
                q = await scanner.fetch_quote(exchange_id, sym)
                if q is None:
                    continue
                mid = (q.bid + q.ask) / 2.0
                snap.append(
                    f"{exchange_id} {sym} mid≈{mid:.2f} spread {format_spread_bid_ask(q.bid, q.ask)}"
                )
                if scalp_trader:
                    scalp_trader.note_quote_for_vol_scale(exchange_id, q)
                ob = None
                need_book = _settings_need_scalping_order_book(settings)
                if need_book:
                    ob = await scanner.fetch_order_book(exchange_id, sym, settings.orderbook_limit)
                if settings.auto_trade and scalp_trader and ob:
                    await scalp_trader.on_tick(exchange_id, sym, ob)
                if sym not in strat_syms:
                    continue
                if hasattr(engine, "refresh_ohlcv"):
                    await engine.refresh_ohlcv(scanner, exchange_id, sym)
                sig = engine.on_quote(q, ob)
                d = _rest_filtered_diag(settings, engine, exchange_id, sym)
                if d:
                    diag.append(f"{exchange_id} {sym}: {d}")
                sk = (exchange_id, sym)
                now_m = time.monotonic()
                if not sig:
                    sig_log_deduper.tick_no_signal(sk, now_m)
                else:
                    if sig_log_deduper.tick_signal(sk, sig.detail, now_m):
                        log.info("%s %s | %s", sym, exchange_id, sig.detail)
                    if scalp_trader:
                        await scalp_trader.on_signal(exchange_id, q, sig, ob)
        if settings.scalping_rest_heartbeat_seconds > 0:
            now = time.monotonic()
            if now - last_hb >= settings.scalping_rest_heartbeat_seconds:
                last_hb = now
                if snap:
                    parts = [" | ".join(snap)]
                    if diag:
                        parts.append("стратегия: " + " || ".join(diag))
                    log.info("REST: %s", " — ".join(parts))
                else:
                    log.warning("REST: нет котировок (fetch_quote вернул None)")
        await asyncio.sleep(settings.poll_seconds)


async def run_loop() -> None:
    settings = load_settings()
    log = logging.getLogger("arbitrage")
    _, executor = build_trading_execution(settings)

    orderflow_tape: OrderflowTapeStore | None = None
    if settings.strategy == "scalping" and _settings_need_orderflow_store(settings):
        orderflow_tape = OrderflowTapeStore(
            window_seconds=settings.orderflow_tape_window_seconds,
            max_events=settings.orderflow_tape_max_events,
        )

    scalp_engine: object | None = None
    if settings.strategy == "scalping":
        scalp_engine = make_scalping_engine(settings, orderflow_tape_store=orderflow_tape)
    atr_prov = _atr_bps_provider_from_engine(scalp_engine) if scalp_engine is not None else None
    scalp_trader = (
        ScalpAutoTrader(settings, executor, log, atr_bps_provider=atr_prov)
        if settings.strategy == "scalping"
        else None
    )
    arb_trader = ArbAutoTrader(settings, executor, log) if settings.strategy == "arbitrage" else None

    log.info(
        "Старт: strategy=%s data=%s биржи=%s пары=%s комиссия=%s bps poll=%ss paper=%s",
        settings.strategy,
        settings.data_mode,
        settings.exchanges,
        settings.symbols,
        settings.fee_bps_per_side,
        settings.poll_seconds,
        settings.paper,
    )
    if settings.paper and settings.paper_fee_bps_per_side is not None:
        log.info(
            "Paper: PnL лимиток — комиссия стороны %.2f bps (PAPER_FEE_BPS_PER_SIDE); ARBITRAGE_FEE_BPS_PER_SIDE=%.2f — для live/арбитража",
            settings.paper_trading_fee_bps(),
            settings.fee_bps_per_side,
        )
    if settings.strategy == "scalping" and len(settings.exchanges) > 1:
        log.info(
            "Скальпинг: несколько бирж (%s) — отдельный WS на каждую; в paper ключи не нужны",
            ", ".join(settings.exchanges),
        )
        if not settings.paper and settings.auto_trade:
            log.warning(
                "Live на нескольких биржах: сейчас один набор EXCHANGE_API_* / BYBIT_* подставляется во все CCXT-клиенты. "
                "Для реальных ордеров на Bybit и OKX одновременно нужны разные ключи — доработайте secrets под каждую биржу."
            )
    if not settings.paper:
        log.warning(
            "БОЕВОЙ РЕЖИМ (ARBITRAGE_PAPER=false): ордера уходят на биржу через CCXT. "
            "Проверьте secrets.env, комиссию ARBITRAGE_FEE_BPS_PER_SIDE (как в аккаунте), лимиты RISK_*, "
            "права ключа (spot, trade). Алгоритм не гарантирует прибыль; возможны убытки и проскальзывание."
        )
    log.info(
        "Риск: notional/ордер≤%.2f открытых≤%s суммарно≤%.2f | strict_risk_limits=%s | auto_trade=%s notional=%.2f cooldown=%ss arb=%s edge≥%.1f bps",
        settings.risk_max_notional_per_order,
        settings.risk_max_open_orders,
        settings.risk_max_total_notional,
        settings.risk_strict_risk_limits,
        settings.auto_trade,
        settings.auto_trade_notional,
        settings.auto_trade_cooldown_seconds,
        settings.auto_trade_arbitrage,
        settings.auto_trade_min_edge_bps,
    )
    if settings.auto_trade:
        log.info(
            "AUTO_TRADE: кулдаун по %s",
            "символу (одна сделка на пару; mutex между WS-биржами)" if settings.auto_trade_cooldown_scope == "symbol" else "биржа+символ",
        )
        if settings.strategy == "scalping" and settings.auto_trade_max_open_orders is not None:
            log.info(
                "AUTO_TRADE: потолок одновременных заявок=%s (AUTO_TRADE_MAX_OPEN_ORDERS; итог в «открытых≤» выше)",
                settings.auto_trade_max_open_orders,
            )
        if settings.strategy == "scalping" and settings.auto_trade_reduce_only:
            log.info(
                "AUTO_TRADE: reduce-only (%s)",
                "глобально по паре (все биржи)" if settings.auto_trade_reduce_only_scope == "symbol" else "по бирже+паре",
            )
        if settings.auto_trade_max_hold_seconds > 0:
            log.info(
                "AUTO_TRADE: MAX_HOLD=%.0fs; min net pnl для таймера — long≥%.1f bps, short≥%.1f bps (0=выкл.); HARD=%.0fs",
                settings.auto_trade_max_hold_seconds,
                settings.auto_trade_max_hold_min_pnl_bps_long,
                settings.auto_trade_max_hold_min_pnl_bps_short,
                settings.auto_trade_max_hold_hard_seconds,
            )
    if settings.auto_trade and settings.strategy == "scalping":
        streams = len(settings.exchanges) * len(settings.symbols)
        if settings.risk_strict_risk_limits and settings.risk_max_open_orders < streams:
            log.warning(
                "Риск: RISK_MAX_OPEN_ORDERS=%s < бирж×пар=%s при RISK_STRICT_RISK_LIMITS=true — возможны отказы; "
                "увеличьте лимит, сократите биржи/пары или снимите строгий режим.",
                settings.risk_max_open_orders,
                streams,
            )
        need_total = settings.auto_trade_notional * min(settings.risk_max_open_orders, streams)
        if (
            settings.risk_strict_risk_limits
            and settings.risk_max_total_notional + 1e-9 < need_total
        ):
            log.warning(
                "Риск: RISK_MAX_TOTAL_NOTIONAL=%.2f мало относительно notional×слоты≈%.2f (AUTO_TRADE_NOTIONAL=%.2f) — "
                "возможны отказы; поднимите лимит или отключите RISK_STRICT_RISK_LIMITS.",
                settings.risk_max_total_notional,
                need_total,
                settings.auto_trade_notional,
            )
    if (
        settings.strategy == "scalping"
        and settings.auto_trade
        and settings.scalping_auto_trade_min_impulse_bps > 0
    ):
        log.info(
            "Скальпинг: заявки только если |импульс сигнала|≥%.1f bps (см. SCALPING_AUTO_TRADE_MIN_IMPULSE_BPS; 0=выкл.)",
            settings.scalping_auto_trade_min_impulse_bps,
        )
    if settings.auto_trade and settings.auto_trade_vol_scale_enabled:
        log.info(
            "Vol-scale номинала: σ_eff≤%.1f bps — полный AUTO_TRADE_NOTIONAL, σ_eff≥%.1f bps — ×%.2f; окно σ=%s тиков; "
            "калибровка rest=%.2f ws=%.2f",
            settings.scalping_auto_trade_vol_ref_bps,
            settings.scalping_auto_trade_vol_cap_bps,
            settings.scalping_auto_trade_vol_min_mult,
            settings.scalping_vol_window,
            settings.scalping_sigma_calib_rest,
            settings.scalping_sigma_calib_ws,
        )
    if settings.auto_trade and settings.strategy == "scalping":
        if settings.scalping_sigma_spike_cooldown_seconds > 0:
            log.info(
                "Всплеск σ: пауза %.0fs при σ_eff≥%.1f bps и/или ≥%.2f× к предыдущему",
                settings.scalping_sigma_spike_cooldown_seconds,
                settings.scalping_sigma_spike_abs_bps,
                settings.scalping_sigma_spike_ratio,
            )
        if settings.auto_trade_atr_max_spread_mult > 0 or settings.auto_trade_atr_notional_ref_bps > 0:
            log.info(
                "ATR×auto_trade: max_spread_mult=%.2f notional_ref_bps=%.1f floor_mult=%.2f (нужен TA-движок)",
                settings.auto_trade_atr_max_spread_mult,
                settings.auto_trade_atr_notional_ref_bps,
                settings.auto_trade_atr_notional_floor_mult,
            )
    if settings.strategy == "scalping":
        if settings.scalping_variant == "rotate":
            log.info(
                "Скальпинг rotate: по кругу [%s], смена каждые %.0f с (общие move/vol/EMA из .env)",
                ", ".join(settings.scalping_rotate_variants),
                settings.scalping_rotate_interval_seconds,
            )
            if "orderflow" in settings.scalping_rotate_variants:
                log.info(
                    "Шаг orderflow: см. ORDERFLOW_* (.env.example); лента WS при ORDERFLOW_WS_COLLECT=true"
                )
        elif settings.scalping_variant == "ta_rotate":
            log.info(
                "Скальпинг ta_rotate: по кругу [%s], смена каждые %.0f с (SCALPING_ROTATE_INTERVAL_SECONDS; TA_* общие для всех шагов)",
                ", ".join(settings.scalping_ta_rotate_variants),
                settings.scalping_rotate_interval_seconds,
            )
            if "orderflow" in settings.scalping_ta_rotate_variants:
                log.info(
                    "Шаг orderflow: лента (WS, ORDERFLOW_WS_COLLECT=%s) + верх стакана; режим сигнала=%s; см. ORDERFLOW_* в .env.example",
                    settings.orderflow_ws_collect,
                    settings.orderflow_signal_mode,
                )
        elif settings.scalping_variant == "adaptive":
            log.info(
                "Скальпинг adaptive: σ-тики окно=%s, глубина верх.%s уровней thin<%.0f thick>%.0f quote, vol low<%.1f high>%.1f bps",
                settings.scalping_vol_window,
                settings.scalping_adaptive_depth_levels,
                settings.scalping_adaptive_depth_thin,
                settings.scalping_adaptive_depth_thick,
                settings.scalping_adaptive_vol_low_bps,
                settings.scalping_adaptive_vol_high_bps,
            )
        elif settings.scalping_variant == "mean_reversion":
            log.info(
                "Скальпинг mean_reversion: EMA α=%.3f отклонение>=%.1f bps, reentry<%.1f bps, max_spread=%s, max_slip≤%.1f bps, cancel_prev=%s",
                settings.scalping_ema_alpha,
                settings.scalping_reversion_deviation_bps,
                settings.scalping_reversion_reentry_bps,
                settings.scalping_max_spread_bps,
                settings.scalping_max_slippage_bps,
                settings.scalping_cancel_previous_orders,
            )
        elif settings.scalping_variant == "filtered_momentum":
            mv_hi = (
                "∞"
                if settings.scalping_max_micro_vol_bps <= 0
                else f"{settings.scalping_max_micro_vol_bps:.1f}"
            )
            log.info(
                "Скальпинг filtered_momentum: move>=%.1f bps, окно σ=%s тиков, micro_vol [%.1f…%s] bps, max_spread=%s, max_slip≤%.1f bps, cancel_prev=%s",
                settings.scalping_move_bps,
                settings.scalping_vol_window,
                settings.scalping_min_micro_vol_bps,
                mv_hi,
                settings.scalping_max_spread_bps,
                settings.scalping_max_slippage_bps,
                settings.scalping_cancel_previous_orders,
            )
        elif settings.scalping_variant == "orderflow":
            log.info(
                "Скальпинг orderflow: окно ленты %.1fs, книга top %s ур., сигнал=%s; WS-лента %s",
                settings.orderflow_tape_window_seconds,
                settings.orderflow_book_levels,
                settings.orderflow_signal_mode,
                "вкл." if settings.orderflow_ws_collect else "выкл. (только стакан)",
            )
        elif settings.scalping_variant.startswith("ta_"):
            log.info(
                "Скальпинг TA (%s): OHLCV %s×%s, REST каждые %.0fs (TA_OHLCV_*); ta_regime: ADX≥%.1f→тренд, узкие BB≤%.0f bps→консерв.; "
                "RSI/ATR Wilder; дедуп на свечу=%s; TA_TREND_MIN_DI_DIFF=%.1f%s",
                settings.scalping_variant,
                settings.ta_timeframe,
                settings.ta_ohlcv_limit,
                settings.ta_ohlcv_refresh_seconds,
                settings.ta_regime_adx_trend,
                settings.ta_regime_bb_squeeze_bps,
                settings.ta_signal_one_per_bar,
                settings.ta_trend_min_di_diff,
                (
                    f"; TA_REGIME_MODE={settings.ta_regime_mode}"
                    if settings.scalping_variant == "ta_regime"
                    else ""
                ),
            )
            if settings.auto_trade and settings.scalping_auto_trade_min_impulse_bps >= 3:
                log.info(
                    "TA: при отсутствии сделок снизьте SCALPING_AUTO_TRADE_MIN_IMPULSE_BPS (напр. 0–2) или ослабьте TA_RSI_* / TA_BAND_NEAR_BPS"
                )
        else:
            log.info(
                "Скальпинг momentum: move>=%.1f bps, max_spread=%s, max_slip≤%.1f bps, cancel_prev=%s",
                settings.scalping_move_bps,
                settings.scalping_max_spread_bps,
                settings.scalping_max_slippage_bps,
                settings.scalping_cancel_previous_orders,
            )
    if settings.strategy == "scalping" and settings.data_mode == "rest":
        if settings.scalping_rest_heartbeat_seconds > 0:
            log.info(
                "REST: пульс в лог каждые %.0f с (mid/spread); сигналы стратегии — отдельными строками при срабатывании",
                settings.scalping_rest_heartbeat_seconds,
            )
        else:
            log.info(
                "REST: пульс отключён (SCALPING_REST_HEARTBEAT_SECONDS=0); при отсутствии сигналов лог может быть пустым"
            )
        if settings.scalping_variant == "filtered_momentum":
            log.info(
                "REST+filtered_momentum: один «тик» = один опрос (каждые %.1fs). Пороги move и σ обычно реже "
                "достигаются, чем на WebSocket — снизьте SCALPING_MOVE_BPS и SCALPING_MIN_MICRO_VOL_BPS (или 0), "
                "или поставьте DATA_MODE=ws.",
                settings.poll_seconds,
            )
    if settings.data_mode == "ws":
        log.info("WebSocket: глубина стакана ORDERBOOK_LIMIT=%s", settings.orderbook_limit)
        if settings.strategy == "scalping":
            if settings.scalping_ws_heartbeat_seconds > 0:
                log.info(
                    "WS: пульс каждые %.0f с — обновления стакана, mid/spread, диапазон mid за интервал; 0=выкл.",
                    settings.scalping_ws_heartbeat_seconds,
                )
            else:
                log.info(
                    "WS: пульс отключён (SCALPING_WS_HEARTBEAT_SECONDS=0) — при тихом рынке в логе только сигналы стратегии"
                )
        if settings.strategy == "scalping" and settings.scalping_variant == "filtered_momentum":
            log.info(
                "WS+filtered_momentum: сигнал только если |Δmid|≥%.1f bps между двумя подряд тиками стакана "
                "(строка «диапазон mid≈…» в пульсе — за ~30s, это другое). Нет сигналов — снизьте SCALPING_MOVE_BPS.",
                settings.scalping_move_bps,
            )

    if scalp_trader and settings.auto_trade and not settings.paper:
        for ex_id in settings.exchanges:
            restored = await scalp_trader.restore_live_orders_from_exchange(ex_id, settings.symbols)
            log.info(
                "Синхронизация с биржей %s при старте (открытые лимитки): %s",
                ex_id,
                restored,
            )

    ta_scanner: ArbitrageScanner | None = None
    try:
        if settings.data_mode == "ws":
            if settings.strategy == "scalping":
                if scalp_engine is None:
                    raise RuntimeError("scalp_engine: ожидается при strategy=scalping")
                if _scalping_uses_ta(settings):
                    ta_scanner = ArbitrageScanner(settings)
                try:
                    await run_scalping_ws(
                        settings,
                        log,
                        scalp_engine,
                        scalp_trader,
                        executor,
                        scanner=ta_scanner,
                        orderflow_tape_store=orderflow_tape,
                    )
                finally:
                    if ta_scanner is not None:
                        await asyncio.shield(ta_scanner.close())
            else:
                await run_arbitrage_ws(settings, log, arb_trader)
            return

        scanner = ArbitrageScanner(settings)
        try:
            if settings.strategy == "scalping":
                if scalp_engine is None:
                    raise RuntimeError("scalp_engine: ожидается при strategy=scalping")
                await run_scalping_loop(scanner, settings, log, scalp_trader, scalp_engine)
            else:
                await run_arbitrage_loop(scanner, settings, log, arb_trader)
        finally:
            await scanner.close()
    finally:
        if settings.paper:
            pnl_line = executor.paper_session_pnl_summary()
            if pnl_line:
                log.info("Paper-сессия: %s", pnl_line)
            rep = executor.paper_session_report_lines()
            if rep:
                log.info("Paper-отчёт:\n%s", "\n".join(rep))
            if scalp_trader and settings.strategy == "scalping" and settings.auto_trade:
                arep = scalp_trader.paper_session_report_lines()
                if arep:
                    log.info("AutoTrade-отчёт:\n%s", "\n".join(arep))
                urep = scalp_trader.paper_unrealized_report_lines()
                if urep:
                    log.info("Unrealized-отчёт:\n%s", "\n".join(urep))
            totals = executor.paper_totals_banner_lines()
            if totals:
                log.info("%s", "\n".join(totals))
        await asyncio.shield(executor.close())


def main() -> None:
    setup_logging()
    try:
        asyncio.run(run_loop())
    except KeyboardInterrupt:
        logging.getLogger("arbitrage").info("Остановка по Ctrl+C")


if __name__ == "__main__":
    main()
