"""Загрузка настроек арбитражного бота из переменных окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

import ccxt.async_support as ccxt

# Загружаем локальные переменные окружения.
# 1) `.env` — для безопасной (не секретной) конфигурации
# 2) `secrets.env` — для секретов (ключи API), который должен быть добавлен в `.gitignore`
# Важно: `override=False`, чтобы не перетирать уже заданные переменные окружения.
load_dotenv(".env", override=False)
load_dotenv("secrets.env", override=False)


def _split_csv(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def _split_exchanges(value: str) -> list[str]:
    """Идентификаторы бирж в CCXT — в нижнем регистре (bybit, okx, …)."""
    return [x.strip().lower() for x in value.split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    """strategy: arbitrage — межбиржа; scalping — импульс на одной бирже."""

    strategy: str
    exchanges: tuple[str, ...]
    symbols: tuple[str, ...]
    fee_bps_per_side: float
    #: Только paper: комиссия стороны при моделировании лимиток (maker обычно ниже ARBITRAGE_FEE_BPS_PER_SIDE). None = как fee_bps_per_side.
    paper_fee_bps_per_side: float | None
    poll_seconds: float
    paper: bool
    scalping_move_bps: float
    scalping_max_spread_bps: float | None
    scalping_variant: str
    scalping_ema_alpha: float
    scalping_reversion_deviation_bps: float
    scalping_reversion_reentry_bps: float
    scalping_vol_window: int
    scalping_min_micro_vol_bps: float
    scalping_max_micro_vol_bps: float
    data_mode: str
    orderbook_limit: int
    risk_max_notional_per_order: float
    risk_max_open_orders: int
    risk_max_total_notional: float
    #: Если false — при scalping+AUTO_TRADE лимиты в load_settings поднимаются до бирж×пар (см. .env.example).
    risk_strict_risk_limits: bool
    #: Управление капиталом / ограничение убытков (см. CAPITAL_* в .env.example).
    capital_max_session_loss_quote: float
    capital_cooldown_after_loss_seconds: float
    capital_max_consecutive_losses: int
    auto_trade: bool
    auto_trade_notional: float
    auto_trade_cooldown_seconds: float
    #: "exchange_symbol" — кулдаун по (биржа, пара); "symbol" — одна сделка на пару на всех биржах (меньше дублей).
    auto_trade_cooldown_scope: str
    #: Скальпинг+AUTO_TRADE: потолок одновременных заявок (None = только RISK_*).
    auto_trade_max_open_orders: int | None
    #: Скальпинг+AUTO_TRADE: не наращивать позицию; если позиция есть — только уменьшающие ордера (reduce-only по смыслу).
    auto_trade_reduce_only: bool
    #: Reduce-only область: exchange_symbol/leg (по бирже+паре) или symbol (глобально по паре на всех биржах).
    auto_trade_reduce_only_scope: str
    #: Reduce-only/exit: считать позицию "пылью", если |pos_base|×mid < порога (в quote, напр. USDT). 0 = выкл.
    auto_trade_position_dust_quote: float
    #: Авто-выход: take-profit в bps от цены входа; 0 = выкл.
    auto_trade_tp_bps: float
    #: Авто-выход: stop-loss в bps от цены входа; 0 = выкл.
    auto_trade_sl_bps: float
    #: Авто-выход (paper): SL как max(SL_BPS, SL_ATR_MULT × ATR_bps). 0 = выключить ATR-надбавку.
    auto_trade_sl_atr_mult: float
    #: Авто-выход: макс. время удержания позиции (сек); 0 = выкл.
    auto_trade_max_hold_seconds: float
    #: Авто-выход (paper): при MAX_HOLD закрывать только если pnl_bps >= порога; 0 = без порога.
    #: Базовое значение; если заданы *_LONG / *_SHORT — для соответствующей стороны берутся они.
    auto_trade_max_hold_min_pnl_bps: float
    #: MIN_PNL для long при MAX_HOLD (пустой env → как auto_trade_max_hold_min_pnl_bps).
    auto_trade_max_hold_min_pnl_bps_long: float
    #: MIN_PNL для short при MAX_HOLD (пустой env → как auto_trade_max_hold_min_pnl_bps).
    auto_trade_max_hold_min_pnl_bps_short: float
    #: Авто-выход (paper): "жёсткий" MAX_HOLD — закрыть позицию при достижении времени, независимо от pnl/min_pnl. 0 = выкл.
    auto_trade_max_hold_hard_seconds: float
    #: Авто-выход (paper): разрешить TP даже если есть pending-ордера по symbol (обычно лучше false).
    auto_trade_tp_allow_with_pending: bool
    auto_trade_arbitrage: bool
    auto_trade_min_edge_bps: float
    api_key: str | None
    api_secret: str | None
    api_passphrase: str | None
    trading_exchange: str | None
    scalping_max_slippage_bps: float
    scalping_cancel_previous_orders: bool
    scalping_order_ttl_seconds: float
    scalping_reprice_bps: float
    scalping_reprice_cooldown_seconds: float
    scalping_rotate_variants: tuple[str, ...]
    scalping_ta_rotate_variants: tuple[str, ...]
    scalping_rotate_interval_seconds: float
    scalping_adaptive_depth_levels: int
    scalping_adaptive_depth_thin: float
    scalping_adaptive_depth_thick: float
    scalping_adaptive_vol_low_bps: float
    scalping_adaptive_vol_high_bps: float
    auto_trade_vol_scale_enabled: bool
    scalping_auto_trade_vol_ref_bps: float
    scalping_auto_trade_vol_cap_bps: float
    scalping_auto_trade_vol_min_mult: float
    #: Множитель к σ тиков при DATA_MODE=rest / ws (калибровка vol-scale и всплеска).
    scalping_sigma_calib_rest: float
    scalping_sigma_calib_ws: float
    #: Всплеск σ: пауза AUTO_TRADE (сек); 0 — выкл.
    scalping_sigma_spike_abs_bps: float
    scalping_sigma_spike_ratio: float
    scalping_sigma_spike_cooldown_seconds: float
    #: Связь с TA-ATR: 0 — выкл.
    auto_trade_atr_max_spread_mult: float
    auto_trade_atr_notional_ref_bps: float
    auto_trade_atr_notional_floor_mult: float
    scalping_rest_heartbeat_seconds: float
    scalping_ws_heartbeat_seconds: float
    #: Сброс дедупа INFO по сигналу после стольки секунд без сигнала (мерцание WS).
    scalping_signal_log_fp_stale_seconds: float
    scalping_auto_trade_min_impulse_bps: float
    # Quiet-market guard: не входить, если диапазон mid за окно слишком мал (bps). 0 = выкл.
    scalping_min_mid_range_bps: float
    # Окно для quiet-market guard (сек). Используется только если scalping_min_mid_range_bps > 0.
    scalping_min_mid_range_window_seconds: float
    # TA (OHLCV): ta_regime, ta_conservative, ta_aggressive, ta_trend
    ta_timeframe: str
    ta_ohlcv_limit: int
    ta_ohlcv_refresh_seconds: float
    ta_sma_period: int
    ta_rsi_period: int
    ta_rsi_oversold: float
    ta_rsi_overbought: float
    ta_bb_period: int
    ta_bb_std: float
    ta_adx_period: int
    ta_atr_period: int
    ta_trend_adx_trigger: float
    ta_vwap_bars: int
    ta_vp_bins: int
    ta_aggr_eps_bps: float
    ta_regime_adx_trend: float
    #: Для ta_regime: hierarchy — ADX/BB как раньше; best_signal — среди трёх стратегий по силе импульса (одна сторона).
    ta_regime_mode: str
    ta_regime_bb_squeeze_bps: float
    ta_band_near_bps: float
    ta_aggr_require_poc: bool
    #: Не повторять тот же side на той же закрытой свече (меньше ложных входов при WS).
    ta_signal_one_per_bar: bool
    #: Мин. |+DI − −DI| для ta_trend; 0 — выкл.
    ta_trend_min_di_diff: float
    #: Под-вариант orderflow: окно ленты сделок (сек), для WS.
    orderflow_tape_window_seconds: float
    orderflow_tape_max_events: int
    #: Мин. суммарный объём сделок в окне (quote), чтобы учитывать ленту.
    orderflow_tape_min_total_quote: float
    orderflow_tape_long_share: float
    orderflow_tape_short_share: float
    orderflow_book_levels: int
    #: bid_notional / ask_notional по верхним уровням — порог «давления».
    orderflow_book_ratio_long: float
    orderflow_book_ratio_short: float
    orderflow_book_min_side_quote: float
    #: both — лента и книга; either — достаточно одного условия.
    orderflow_signal_mode: str
    orderflow_cooldown_seconds: float
    #: DATA_MODE=ws: подписываться на watch_trades, если orderflow в списке вариантов (или явно в env).
    orderflow_ws_collect: bool

    def paper_trading_fee_bps(self) -> float:
        """Paper: комиссия одной стороны для лимиток (часто maker < taker). Иначе — ARBITRAGE_FEE_BPS_PER_SIDE."""
        p = self.paper_fee_bps_per_side
        if p is not None:
            return float(p)
        return float(self.fee_bps_per_side)


def load_settings() -> Settings:
    strategy = os.getenv("BOT_STRATEGY", "arbitrage").strip().lower()
    raw_ex = os.getenv("ARBITRAGE_EXCHANGES", "binance,bybit")
    raw_sym = os.getenv("ARBITRAGE_SYMBOLS", "BTC/USDT")
    fee = float(os.getenv("ARBITRAGE_FEE_BPS_PER_SIDE", "10"))
    _raw_pfee = os.getenv("PAPER_FEE_BPS_PER_SIDE", "").strip()
    paper_fee_opt: float | None = float(_raw_pfee) if _raw_pfee else None
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

    vol_window = int(os.getenv("SCALPING_VOL_WINDOW", "15"))
    min_micro_vol = float(os.getenv("SCALPING_MIN_MICRO_VOL_BPS", "0"))
    max_micro_vol = float(os.getenv("SCALPING_MAX_MICRO_VOL_BPS", "0"))

    data_mode = os.getenv("DATA_MODE", "rest").strip().lower()
    orderbook_limit = int(os.getenv("ORDERBOOK_LIMIT", "20"))
    risk_per_order = float(os.getenv("RISK_MAX_NOTIONAL_PER_ORDER", "100"))
    _raw_risk_open = os.getenv("RISK_MAX_OPEN_ORDERS", "").strip()
    _raw_risk_total = os.getenv("RISK_MAX_TOTAL_NOTIONAL", "").strip()
    risk_max_orders: int | None = int(_raw_risk_open) if _raw_risk_open else None
    risk_total: float | None = float(_raw_risk_total) if _raw_risk_total else None
    risk_strict_limits = os.getenv("RISK_STRICT_RISK_LIMITS", "false").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    auto_trade = os.getenv("AUTO_TRADE", "false").lower() in ("1", "true", "yes", "on")
    auto_trade_notional = float(os.getenv("AUTO_TRADE_NOTIONAL", "50"))
    auto_trade_cooldown = float(os.getenv("AUTO_TRADE_COOLDOWN_SECONDS", "3"))
    _raw_cd_scope = os.getenv("AUTO_TRADE_COOLDOWN_SCOPE", "exchange_symbol").strip().lower()
    if _raw_cd_scope in ("symbol", "global", "per_symbol"):
        auto_trade_cd_scope = "symbol"
    elif _raw_cd_scope in ("exchange_symbol", "exchange", "per_exchange"):
        auto_trade_cd_scope = "exchange_symbol"
    else:
        raise ValueError("AUTO_TRADE_COOLDOWN_SCOPE: symbol | exchange_symbol")
    _raw_at_moo = os.getenv("AUTO_TRADE_MAX_OPEN_ORDERS", "").strip()
    auto_trade_max_open: int | None = int(_raw_at_moo) if _raw_at_moo else None
    auto_trade_reduce_only = os.getenv("AUTO_TRADE_REDUCE_ONLY", "false").lower() in ("1", "true", "yes", "on")
    _raw_ro_scope = os.getenv("AUTO_TRADE_REDUCE_ONLY_SCOPE", "exchange_symbol").strip().lower()
    if _raw_ro_scope in ("symbol", "global", "per_symbol"):
        auto_trade_ro_scope = "symbol"
    elif _raw_ro_scope in ("exchange_symbol", "exchange", "per_exchange", "leg"):
        auto_trade_ro_scope = "exchange_symbol"
    else:
        raise ValueError("AUTO_TRADE_REDUCE_ONLY_SCOPE: symbol | exchange_symbol | leg")
    pos_dust_quote = float(os.getenv("AUTO_TRADE_POSITION_DUST_QUOTE", "0"))
    at_tp = float(os.getenv("AUTO_TRADE_TP_BPS", "0"))
    at_sl = float(os.getenv("AUTO_TRADE_SL_BPS", "0"))
    at_hold = float(os.getenv("AUTO_TRADE_MAX_HOLD_SECONDS", "0"))
    at_hold_min_pnl = float(os.getenv("AUTO_TRADE_MAX_HOLD_MIN_PNL_BPS", "0"))
    _hml_raw = os.getenv("AUTO_TRADE_MAX_HOLD_MIN_PNL_BPS_LONG", "").strip()
    _hms_raw = os.getenv("AUTO_TRADE_MAX_HOLD_MIN_PNL_BPS_SHORT", "").strip()
    at_hold_min_pnl_long = float(_hml_raw) if _hml_raw else at_hold_min_pnl
    at_hold_min_pnl_short = float(_hms_raw) if _hms_raw else at_hold_min_pnl
    at_hold_hard = float(os.getenv("AUTO_TRADE_MAX_HOLD_HARD_SECONDS", "0"))
    at_sl_atr_mult = float(os.getenv("AUTO_TRADE_SL_ATR_MULT", "0"))
    at_tp_allow_pending = os.getenv("AUTO_TRADE_TP_ALLOW_WITH_PENDING", "false").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    auto_trade_arbitrage = os.getenv("AUTO_TRADE_ARBITRAGE", "false").lower() in ("1", "true", "yes", "on")
    auto_trade_min_edge = float(os.getenv("AUTO_TRADE_MIN_EDGE_BPS", "5"))

    scalping_max_slip = float(os.getenv("SCALPING_MAX_SLIPPAGE_BPS", "50"))
    scalping_cancel_prev = os.getenv("SCALPING_CANCEL_PREVIOUS_ORDERS", "true").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    scalping_ttl = float(os.getenv("SCALPING_ORDER_TTL_SECONDS", "30"))
    scalping_reprice_bps = float(os.getenv("SCALPING_REPRICE_BPS", "10"))
    scalping_reprice_cd = float(os.getenv("SCALPING_REPRICE_COOLDOWN_SECONDS", "5"))

    raw_rot_v = os.getenv("SCALPING_ROTATE_VARIANTS", "").strip()
    rotate_variants = tuple(x.strip().lower() for x in raw_rot_v.split(",") if x.strip())
    raw_ta_rot = os.getenv("SCALPING_TA_ROTATE_VARIANTS", "").strip()
    ta_rotate_variants = tuple(x.strip().lower() for x in raw_ta_rot.split(",") if x.strip())
    rotate_interval = float(os.getenv("SCALPING_ROTATE_INTERVAL_SECONDS", "3600"))

    of_tape_win = float(os.getenv("ORDERFLOW_TAPE_WINDOW_SECONDS", "2.5"))
    of_tape_max = int(os.getenv("ORDERFLOW_TAPE_MAX_EVENTS", "800"))
    of_tape_min_q = float(os.getenv("ORDERFLOW_TAPE_MIN_TOTAL_QUOTE", "400"))
    of_tape_long = float(os.getenv("ORDERFLOW_TAPE_LONG_SHARE", "0.58"))
    of_tape_short = float(os.getenv("ORDERFLOW_TAPE_SHORT_SHARE", "0.42"))
    of_book_lv = int(os.getenv("ORDERFLOW_BOOK_LEVELS", "8"))
    of_br_long = float(os.getenv("ORDERFLOW_BOOK_RATIO_LONG", "1.14"))
    _raw_br_s = os.getenv("ORDERFLOW_BOOK_RATIO_SHORT", "").strip()
    of_br_short = float(_raw_br_s) if _raw_br_s else (1.0 / of_br_long if of_br_long > 0 else 0.88)
    of_book_min_side = float(os.getenv("ORDERFLOW_BOOK_MIN_SIDE_QUOTE", "80"))
    _of_mode = os.getenv("ORDERFLOW_SIGNAL_MODE", "both").strip().lower()
    if _of_mode not in ("both", "either"):
        raise ValueError("ORDERFLOW_SIGNAL_MODE: both | either")
    orderflow_signal_mode = _of_mode
    of_cd = float(os.getenv("ORDERFLOW_COOLDOWN_SECONDS", "2.5"))
    of_in_rot = "orderflow" in rotate_variants or "orderflow" in ta_rotate_variants
    _raw_of_ws = os.getenv("ORDERFLOW_WS_COLLECT", "").strip().lower()
    if _raw_of_ws in ("1", "true", "yes", "on"):
        orderflow_ws_collect = True
    elif _raw_of_ws in ("0", "false", "no", "off"):
        orderflow_ws_collect = False
    else:
        orderflow_ws_collect = bool(variant == "orderflow" or of_in_rot)

    ad_depth_lv = int(os.getenv("SCALPING_ADAPTIVE_DEPTH_LEVELS", "5"))
    ad_depth_thin = float(os.getenv("SCALPING_ADAPTIVE_DEPTH_THIN", "300"))
    ad_depth_thick = float(os.getenv("SCALPING_ADAPTIVE_DEPTH_THICK", "3000"))
    ad_vol_low = float(os.getenv("SCALPING_ADAPTIVE_VOL_LOW_BPS", "4"))
    ad_vol_high = float(os.getenv("SCALPING_ADAPTIVE_VOL_HIGH_BPS", "22"))

    vol_scale_on = os.getenv("SCALPING_AUTO_TRADE_VOL_SCALE", "false").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    at_vol_ref = float(os.getenv("SCALPING_AUTO_TRADE_VOL_REF_BPS", "12"))
    at_vol_cap = float(os.getenv("SCALPING_AUTO_TRADE_VOL_CAP_BPS", "45"))
    at_vol_min_m = float(os.getenv("SCALPING_AUTO_TRADE_VOL_MIN_MULT", "0.25"))

    sig_cal_rest = float(os.getenv("SCALPING_SIGMA_CALIB_REST", "1"))
    sig_cal_ws = float(os.getenv("SCALPING_SIGMA_CALIB_WS", "1"))
    spike_abs = float(os.getenv("SCALPING_SIGMA_SPIKE_ABS_BPS", "0"))
    spike_ratio = float(os.getenv("SCALPING_SIGMA_SPIKE_RATIO", "0"))
    spike_cd = float(os.getenv("SCALPING_SIGMA_SPIKE_COOLDOWN_SECONDS", "0"))
    atr_spread_m = float(os.getenv("AUTO_TRADE_ATR_MAX_SPREAD_MULT", "0"))
    atr_not_ref = float(os.getenv("AUTO_TRADE_ATR_NOTIONAL_REF_BPS", "0"))
    atr_not_floor = float(os.getenv("AUTO_TRADE_ATR_NOTIONAL_FLOOR_MULT", "0"))

    rest_hb = float(os.getenv("SCALPING_REST_HEARTBEAT_SECONDS", "30"))
    ws_hb = float(os.getenv("SCALPING_WS_HEARTBEAT_SECONDS", "30"))
    sig_log_fp_stale = float(os.getenv("SCALPING_SIGNAL_LOG_FP_STALE_SECONDS", "30"))
    # 0 = не фильтровать; иначе не ставить лимитку, если |импульс| сигнала < порога (см. 2× комиссию)
    min_impulse_at = float(os.getenv("SCALPING_AUTO_TRADE_MIN_IMPULSE_BPS", "0"))
    min_mid_range_bps = float(os.getenv("SCALPING_MIN_MID_RANGE_BPS", "0"))
    min_mid_range_win = float(os.getenv("SCALPING_MIN_MID_RANGE_WINDOW_SECONDS", "30"))

    ta_timeframe = os.getenv("TA_TIMEFRAME", "5m").strip()
    ta_ohlcv_limit = int(os.getenv("TA_OHLCV_LIMIT", "120"))
    ta_ohlcv_refresh = float(os.getenv("TA_OHLCV_REFRESH_SECONDS", "45"))
    ta_sma_period = int(os.getenv("TA_SMA_PERIOD", "20"))
    ta_rsi_period = int(os.getenv("TA_RSI_PERIOD", "14"))
    ta_rsi_os = float(os.getenv("TA_RSI_OVERSOLD", "35"))
    ta_rsi_ob = float(os.getenv("TA_RSI_OVERBOUGHT", "65"))
    ta_bb_period = int(os.getenv("TA_BB_PERIOD", "20"))
    ta_bb_std = float(os.getenv("TA_BB_STD", "2.0"))
    ta_adx_period = int(os.getenv("TA_ADX_PERIOD", "14"))
    ta_atr_period = int(os.getenv("TA_ATR_PERIOD", "14"))
    ta_trend_adx_trg = float(os.getenv("TA_TREND_ADX_TRIGGER", "30"))
    ta_vwap_bars = int(os.getenv("TA_VWAP_BARS", "48"))
    ta_vp_bins = int(os.getenv("TA_VP_BINS", "24"))
    ta_aggr_eps = float(os.getenv("TA_AGGRESSIVE_EPS_BPS", "3"))
    ta_regime_adx = float(os.getenv("TA_REGIME_ADX_TREND", "22"))
    ta_regime_bb_sq = float(os.getenv("TA_REGIME_BB_SQUEEZE_BPS", "80"))
    ta_band_near = float(os.getenv("TA_BAND_NEAR_BPS", "40"))
    ta_aggr_poc = os.getenv("TA_AGGR_REQUIRE_POC", "false").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    ta_one_per_bar = os.getenv("TA_SIGNAL_ONE_PER_BAR", "true").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    ta_min_di = float(os.getenv("TA_TREND_MIN_DI_DIFF", "0"))
    ta_regime_mode = os.getenv("TA_REGIME_MODE", "hierarchy").strip().lower()

    def _first_nonempty(*names: str) -> str | None:
        for name in names:
            v = os.getenv(name, "").strip()
            if v:
                return v
        return None

    # Приоритет: EXCHANGE_* → API_* → BYBIT_* (часто в .env именно BYBIT_API_KEY)
    api_key = _first_nonempty("EXCHANGE_API_KEY", "API_KEY", "BYBIT_API_KEY")
    api_secret = _first_nonempty("EXCHANGE_API_SECRET", "API_SECRET", "SECRET_KEY", "BYBIT_API_SECRET")
    api_passphrase = _first_nonempty(
        "EXCHANGE_API_PASSPHRASE",
        "API_PASSPHRASE",
        "PASSPHRASE",
    )
    trading_exchange = _first_nonempty("TRADING_EXCHANGE")

    sym_list = _split_csv(raw_sym)
    if not sym_list:
        raise ValueError("ARBITRAGE_SYMBOLS пусто")

    if strategy == "scalping":
        sex = os.getenv("SCALPING_EXCHANGE", "").strip()
        raw_multi_ex = os.getenv("SCALPING_EXCHANGES", "").strip()
        if raw_multi_ex:
            ex_list = _split_exchanges(raw_multi_ex)
        elif sex:
            ex_list = [sex.strip().lower()]
        else:
            ex_list = _split_exchanges(raw_ex)
            if not ex_list:
                raise ValueError(
                    "Для scalping укажите SCALPING_EXCHANGES, SCALPING_EXCHANGE или ARBITRAGE_EXCHANGES"
                )
            ex_list = [ex_list[0]]
    else:
        ex_list = _split_exchanges(raw_ex)
        if not ex_list:
            raise ValueError("ARBITRAGE_EXCHANGES пусто")
        if len(ex_list) < 2:
            raise ValueError("Нужно минимум две биржи для межбиржевого арбитража")

    for eid in ex_list:
        if eid == "pionex":
            raise ValueError(
                "Pionex в CCXT пока не реализован (см. github.com/ccxt/ccxt/issues/18847). "
                "Уберите pionex из списка или используйте поддерживаемую биржу (okx, bybit, …)."
            )
        if not hasattr(ccxt, eid):
            raise ValueError(f"Биржа '{eid}' не найдена в установленном CCXT")

    if risk_max_orders is None:
        if strategy == "scalping" and auto_trade:
            risk_max_orders = max(5, len(ex_list) * len(sym_list))
        else:
            risk_max_orders = 5
    if risk_total is None:
        if strategy == "scalping" and auto_trade:
            risk_total = max(500.0, float(risk_max_orders) * auto_trade_notional)
        else:
            risk_total = 500.0
    # Скальпинг + авто: классический .env с RISK_MAX_OPEN_ORDERS=5 даёт open=5/5 при 2×5 потоках —
    # поднимаем пол до бирж×пар и суммарного номинала (если не RISK_STRICT_RISK_LIMITS=true).
    if strategy == "scalping" and auto_trade and not risk_strict_limits:
        streams = len(ex_list) * len(sym_list)
        risk_max_orders = max(risk_max_orders, streams)
        risk_total = max(risk_total, float(risk_max_orders) * auto_trade_notional)
    if strategy == "scalping" and auto_trade and auto_trade_max_open is not None:
        if auto_trade_max_open < 1:
            raise ValueError("AUTO_TRADE_MAX_OPEN_ORDERS должен быть >= 1")
        risk_max_orders = min(risk_max_orders, auto_trade_max_open)
        # Важно: total-notional — отдельный лимит бюджета. Не "сжимаем" его до slots×AUTO_TRADE_NOTIONAL,
        # иначе выходы (exit) могут регулярно получать отказ при небольшом дрейфе notional (qty×price) и комиссиях.
        # При маленьком бюджете задайте RISK_MAX_TOTAL_NOTIONAL явно.
    if risk_max_orders < 1:
        raise ValueError("RISK_MAX_OPEN_ORDERS должен быть >= 1")
    if risk_total <= 0:
        raise ValueError("RISK_MAX_TOTAL_NOTIONAL должен быть > 0")

    if strategy not in ("arbitrage", "scalping"):
        raise ValueError("BOT_STRATEGY должен быть arbitrage или scalping")
    _scalping_names = (
        "momentum",
        "mean_reversion",
        "filtered_momentum",
        "rotate",
        "adaptive",
        "ta_regime",
        "ta_conservative",
        "ta_aggressive",
        "ta_trend",
        "ta_rotate",
        "orderflow",
    )
    if strategy == "scalping" and variant not in _scalping_names:
        raise ValueError(
            "SCALPING_VARIANT: momentum, mean_reversion, filtered_momentum, rotate, adaptive, "
            "ta_regime, ta_conservative, ta_aggressive, ta_trend, ta_rotate или orderflow"
        )
    if strategy == "scalping" and variant == "rotate":
        if len(rotate_variants) < 2:
            raise ValueError("SCALPING_ROTATE_VARIANTS: укажите минимум два имени через запятую")
        allowed_sub = ("momentum", "mean_reversion", "filtered_momentum", "orderflow")
        for rv in rotate_variants:
            if rv not in allowed_sub:
                raise ValueError(f"SCALPING_ROTATE_VARIANTS: неизвестное имя '{rv}'")
        if rotate_interval <= 0:
            raise ValueError("SCALPING_ROTATE_INTERVAL_SECONDS должен быть > 0")
    if strategy == "scalping" and variant == "ta_rotate":
        if len(ta_rotate_variants) < 2:
            raise ValueError("SCALPING_TA_ROTATE_VARIANTS: укажите минимум два под-варианта через запятую")
        allowed_ta_rot = ("ta_conservative", "ta_aggressive", "ta_trend", "orderflow")
        for rv in ta_rotate_variants:
            if rv not in allowed_ta_rot:
                raise ValueError(
                    f"SCALPING_TA_ROTATE_VARIANTS: неизвестное имя '{rv}' (допустимо: {allowed_ta_rot})"
                )
        if rotate_interval <= 0:
            raise ValueError("SCALPING_ROTATE_INTERVAL_SECONDS должен быть > 0 для ta_rotate")
    if strategy == "scalping" and (
        variant == "orderflow" or "orderflow" in rotate_variants or "orderflow" in ta_rotate_variants
    ):
        if of_tape_win <= 0 or of_tape_win > 120:
            raise ValueError("ORDERFLOW_TAPE_WINDOW_SECONDS обычно 0.2…120")
        if of_tape_max < 8:
            raise ValueError("ORDERFLOW_TAPE_MAX_EVENTS должен быть >= 8")
        if of_tape_min_q < 0:
            raise ValueError("ORDERFLOW_TAPE_MIN_TOTAL_QUOTE должен быть >= 0")
        if not (of_tape_short < 0.5 < of_tape_long < 1.0):
            raise ValueError("ORDERFLOW_TAPE_SHORT_SHARE < 0.5 < ORDERFLOW_TAPE_LONG_SHARE < 1")
        if of_book_lv < 1 or of_book_lv > 80:
            raise ValueError("ORDERFLOW_BOOK_LEVELS обычно 1…80")
        if of_br_long <= 1.0:
            raise ValueError("ORDERFLOW_BOOK_RATIO_LONG должен быть > 1")
        if of_br_short <= 0 or of_br_short >= 1.0:
            raise ValueError("ORDERFLOW_BOOK_RATIO_SHORT должен быть в (0, 1)")
        if of_book_min_side < 0:
            raise ValueError("ORDERFLOW_BOOK_MIN_SIDE_QUOTE должен быть >= 0")
        if of_cd < 0:
            raise ValueError("ORDERFLOW_COOLDOWN_SECONDS должен быть >= 0")
    if strategy == "scalping" and variant == "adaptive":
        if ad_depth_thin <= 0 or ad_depth_thick <= 0 or ad_depth_thin >= ad_depth_thick:
            raise ValueError("SCALPING_ADAPTIVE_DEPTH_THIN/THICK: 0 < thin < thick")
        if ad_vol_low <= 0 or ad_vol_high <= 0 or ad_vol_low >= ad_vol_high:
            raise ValueError("SCALPING_ADAPTIVE_VOL_LOW/HIGH_BPS: 0 < low < high")
        if ad_depth_lv < 1 or ad_depth_lv > 50:
            raise ValueError("SCALPING_ADAPTIVE_DEPTH_LEVELS обычно 1…50")
    if vol_window < 3 or vol_window > 300:
        raise ValueError("SCALPING_VOL_WINDOW обычно 3…300")
    if min_micro_vol > 0 and max_micro_vol > 0 and min_micro_vol > max_micro_vol:
        raise ValueError("SCALPING_MIN_MICRO_VOL_BPS не больше SCALPING_MAX_MICRO_VOL_BPS")
    if data_mode not in ("rest", "ws"):
        raise ValueError("DATA_MODE: rest или ws")
    if orderbook_limit < 5 or orderbook_limit > 500:
        raise ValueError("ORDERBOOK_LIMIT обычно 5…500")
    if paper_fee_opt is not None and (paper_fee_opt < 0 or paper_fee_opt > 200):
        raise ValueError("PAPER_FEE_BPS_PER_SIDE: 0…200 (пусто = как ARBITRAGE_FEE_BPS_PER_SIDE)")
    if not paper and auto_trade and (not api_key or not api_secret):
        raise ValueError(
            "При AUTO_TRADE без бумаги задайте ключи: "
            "EXCHANGE_API_KEY или API_KEY, и EXCHANGE_API_SECRET или API_SECRET"
        )
    if auto_trade_notional <= 0:
        raise ValueError("AUTO_TRADE_NOTIONAL должен быть > 0")
    if at_tp < 0:
        raise ValueError("AUTO_TRADE_TP_BPS должен быть >= 0 (0 — выкл.)")
    if at_sl < 0:
        raise ValueError("AUTO_TRADE_SL_BPS должен быть >= 0 (0 — выкл.)")
    if at_sl_atr_mult < 0:
        raise ValueError("AUTO_TRADE_SL_ATR_MULT должен быть >= 0 (0 — выкл.)")
    if at_hold < 0:
        raise ValueError("AUTO_TRADE_MAX_HOLD_SECONDS должен быть >= 0 (0 — выкл.)")
    if at_hold_min_pnl < 0:
        raise ValueError("AUTO_TRADE_MAX_HOLD_MIN_PNL_BPS должен быть >= 0 (0 — выкл.)")
    if at_hold_min_pnl_long < 0 or at_hold_min_pnl_short < 0:
        raise ValueError(
            "AUTO_TRADE_MAX_HOLD_MIN_PNL_BPS_LONG/SHORT должны быть >= 0 (0 — выкл. порог для стороны)"
        )
    if at_hold_hard < 0:
        raise ValueError("AUTO_TRADE_MAX_HOLD_HARD_SECONDS должен быть >= 0 (0 — выкл.)")
    if pos_dust_quote < 0:
        raise ValueError("AUTO_TRADE_POSITION_DUST_QUOTE должен быть >= 0 (0 — выкл.)")
    if auto_trade_arbitrage and not auto_trade:
        raise ValueError("AUTO_TRADE_ARBITRAGE требует AUTO_TRADE=true")
    if scalping_max_slip < 0:
        raise ValueError("SCALPING_MAX_SLIPPAGE_BPS должен быть >= 0")
    if scalping_ttl < 0:
        raise ValueError("SCALPING_ORDER_TTL_SECONDS должен быть >= 0")
    if scalping_reprice_bps < 0:
        raise ValueError("SCALPING_REPRICE_BPS должен быть >= 0")
    if scalping_reprice_cd < 0:
        raise ValueError("SCALPING_REPRICE_COOLDOWN_SECONDS должен быть >= 0")
    if rest_hb < 0:
        raise ValueError("SCALPING_REST_HEARTBEAT_SECONDS должен быть >= 0 (0 — без пульса в лог)")
    if ws_hb < 0:
        raise ValueError("SCALPING_WS_HEARTBEAT_SECONDS должен быть >= 0 (0 — без пульса в лог)")
    if sig_log_fp_stale < 0:
        raise ValueError("SCALPING_SIGNAL_LOG_FP_STALE_SECONDS должен быть >= 0")
    if min_impulse_at < 0:
        raise ValueError("SCALPING_AUTO_TRADE_MIN_IMPULSE_BPS должен быть >= 0")
    if min_mid_range_bps < 0:
        raise ValueError("SCALPING_MIN_MID_RANGE_BPS должен быть >= 0 (0 — выкл.)")
    if min_mid_range_win <= 0:
        raise ValueError("SCALPING_MIN_MID_RANGE_WINDOW_SECONDS должен быть > 0")

    cap_max_loss = float(os.getenv("CAPITAL_MAX_SESSION_LOSS_QUOTE", "0"))
    cap_cd = float(os.getenv("CAPITAL_COOLDOWN_AFTER_LOSS_SECONDS", "0"))
    cap_streak = int(os.getenv("CAPITAL_MAX_CONSECUTIVE_LOSSES", "0"))
    if cap_max_loss < 0:
        raise ValueError("CAPITAL_MAX_SESSION_LOSS_QUOTE должен быть >= 0 (0 — выкл.)")
    if cap_cd < 0:
        raise ValueError("CAPITAL_COOLDOWN_AFTER_LOSS_SECONDS должен быть >= 0")
    if cap_streak < 0:
        raise ValueError("CAPITAL_MAX_CONSECUTIVE_LOSSES должен быть >= 0 (0 — выкл.)")

    if strategy == "scalping" and variant.startswith("ta_"):
        ta_regime_adx = max(ta_regime_adx, ta_trend_adx_trg)
        if ta_ohlcv_limit < 50:
            raise ValueError("TA_OHLCV_LIMIT для TA-стратегий обычно ≥50")
        if ta_ohlcv_refresh < 3:
            raise ValueError("TA_OHLCV_REFRESH_SECONDS должен быть ≥3")
        if ta_vp_bins < 4 or ta_vp_bins > 200:
            raise ValueError("TA_VP_BINS обычно 4…200")
        if ta_regime_bb_sq < 5:
            raise ValueError("TA_REGIME_BB_SQUEEZE_BPS слишком мало")
        if ta_band_near < 0 or ta_band_near > 300:
            raise ValueError("TA_BAND_NEAR_BPS обычно 0…300")
        if ta_min_di < 0:
            raise ValueError("TA_TREND_MIN_DI_DIFF должен быть >= 0")
        if variant == "ta_regime" and ta_regime_mode not in ("hierarchy", "best_signal"):
            raise ValueError("TA_REGIME_MODE: hierarchy или best_signal")

    if vol_scale_on:
        if at_vol_ref <= 0 or at_vol_cap <= 0 or at_vol_ref >= at_vol_cap:
            raise ValueError(
                "SCALPING_AUTO_TRADE_VOL_REF_BPS и CAP_BPS: 0 < ref < cap (масштаб номинала по σ)"
            )
        if not (0.0 < at_vol_min_m <= 1.0):
            raise ValueError("SCALPING_AUTO_TRADE_VOL_MIN_MULT должен быть в (0, 1]")
    if sig_cal_rest <= 0 or sig_cal_ws <= 0:
        raise ValueError("SCALPING_SIGMA_CALIB_REST/WS должны быть > 0")
    if spike_ratio < 0 or spike_abs < 0 or spike_cd < 0:
        raise ValueError("SCALPING_SIGMA_SPIKE_*: ratio/abs/cooldown должны быть >= 0")
    if spike_cd > 0 and spike_ratio <= 0 and spike_abs <= 0:
        raise ValueError(
            "SCALPING_SIGMA_SPIKE_COOLDOWN_SECONDS>0: задайте SPIKE_ABS_BPS>0 и/или SPIKE_RATIO>0"
        )
    if atr_spread_m < 0:
        raise ValueError("AUTO_TRADE_ATR_MAX_SPREAD_MULT должен быть >= 0")
    if atr_not_ref < 0:
        raise ValueError("AUTO_TRADE_ATR_NOTIONAL_REF_BPS должен быть >= 0")
    if atr_not_floor < 0 or atr_not_floor > 1.0:
        raise ValueError("AUTO_TRADE_ATR_NOTIONAL_FLOOR_MULT должен быть в [0, 1]")

    return Settings(
        strategy=strategy,
        exchanges=tuple(ex_list),
        symbols=tuple(sym_list),
        fee_bps_per_side=fee,
        paper_fee_bps_per_side=paper_fee_opt,
        poll_seconds=poll,
        paper=paper,
        scalping_move_bps=move_bps,
        scalping_max_spread_bps=max_spread,
        scalping_variant=variant,
        scalping_ema_alpha=ema_alpha,
        scalping_reversion_deviation_bps=rev_dev,
        scalping_reversion_reentry_bps=rev_reentry,
        scalping_vol_window=vol_window,
        scalping_min_micro_vol_bps=min_micro_vol,
        scalping_max_micro_vol_bps=max_micro_vol,
        data_mode=data_mode,
        orderbook_limit=orderbook_limit,
        risk_max_notional_per_order=risk_per_order,
        risk_max_open_orders=risk_max_orders,
        risk_max_total_notional=risk_total,
        risk_strict_risk_limits=risk_strict_limits,
        capital_max_session_loss_quote=cap_max_loss,
        capital_cooldown_after_loss_seconds=cap_cd,
        capital_max_consecutive_losses=cap_streak,
        auto_trade=auto_trade,
        auto_trade_notional=auto_trade_notional,
        auto_trade_cooldown_seconds=auto_trade_cooldown,
        auto_trade_cooldown_scope=auto_trade_cd_scope,
        auto_trade_max_open_orders=auto_trade_max_open,
        auto_trade_reduce_only=auto_trade_reduce_only,
        auto_trade_reduce_only_scope=auto_trade_ro_scope,
        auto_trade_position_dust_quote=pos_dust_quote,
        auto_trade_tp_bps=at_tp,
        auto_trade_sl_bps=at_sl,
        auto_trade_sl_atr_mult=at_sl_atr_mult,
        auto_trade_max_hold_seconds=at_hold,
        auto_trade_max_hold_min_pnl_bps=at_hold_min_pnl,
        auto_trade_max_hold_min_pnl_bps_long=at_hold_min_pnl_long,
        auto_trade_max_hold_min_pnl_bps_short=at_hold_min_pnl_short,
        auto_trade_max_hold_hard_seconds=at_hold_hard,
        auto_trade_tp_allow_with_pending=at_tp_allow_pending,
        auto_trade_arbitrage=auto_trade_arbitrage,
        auto_trade_min_edge_bps=auto_trade_min_edge,
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
        trading_exchange=trading_exchange,
        scalping_max_slippage_bps=scalping_max_slip,
        scalping_cancel_previous_orders=scalping_cancel_prev,
        scalping_order_ttl_seconds=scalping_ttl,
        scalping_reprice_bps=scalping_reprice_bps,
        scalping_reprice_cooldown_seconds=scalping_reprice_cd,
        scalping_rotate_variants=rotate_variants,
        scalping_ta_rotate_variants=ta_rotate_variants,
        scalping_rotate_interval_seconds=rotate_interval,
        scalping_adaptive_depth_levels=ad_depth_lv,
        scalping_adaptive_depth_thin=ad_depth_thin,
        scalping_adaptive_depth_thick=ad_depth_thick,
        scalping_adaptive_vol_low_bps=ad_vol_low,
        scalping_adaptive_vol_high_bps=ad_vol_high,
        auto_trade_vol_scale_enabled=vol_scale_on,
        scalping_auto_trade_vol_ref_bps=at_vol_ref,
        scalping_auto_trade_vol_cap_bps=at_vol_cap,
        scalping_auto_trade_vol_min_mult=at_vol_min_m,
        scalping_sigma_calib_rest=sig_cal_rest,
        scalping_sigma_calib_ws=sig_cal_ws,
        scalping_sigma_spike_abs_bps=spike_abs,
        scalping_sigma_spike_ratio=spike_ratio,
        scalping_sigma_spike_cooldown_seconds=spike_cd,
        auto_trade_atr_max_spread_mult=atr_spread_m,
        auto_trade_atr_notional_ref_bps=atr_not_ref,
        auto_trade_atr_notional_floor_mult=atr_not_floor,
        scalping_rest_heartbeat_seconds=rest_hb,
        scalping_ws_heartbeat_seconds=ws_hb,
        scalping_signal_log_fp_stale_seconds=sig_log_fp_stale,
        scalping_auto_trade_min_impulse_bps=min_impulse_at,
        scalping_min_mid_range_bps=min_mid_range_bps,
        scalping_min_mid_range_window_seconds=min_mid_range_win,
        ta_timeframe=ta_timeframe,
        ta_ohlcv_limit=ta_ohlcv_limit,
        ta_ohlcv_refresh_seconds=ta_ohlcv_refresh,
        ta_sma_period=ta_sma_period,
        ta_rsi_period=ta_rsi_period,
        ta_rsi_oversold=ta_rsi_os,
        ta_rsi_overbought=ta_rsi_ob,
        ta_bb_period=ta_bb_period,
        ta_bb_std=ta_bb_std,
        ta_adx_period=ta_adx_period,
        ta_atr_period=ta_atr_period,
        ta_trend_adx_trigger=ta_trend_adx_trg,
        ta_vwap_bars=ta_vwap_bars,
        ta_vp_bins=ta_vp_bins,
        ta_aggr_eps_bps=ta_aggr_eps,
        ta_regime_adx_trend=ta_regime_adx,
        ta_regime_mode=ta_regime_mode,
        ta_regime_bb_squeeze_bps=ta_regime_bb_sq,
        ta_band_near_bps=ta_band_near,
        ta_aggr_require_poc=ta_aggr_poc,
        ta_signal_one_per_bar=ta_one_per_bar,
        ta_trend_min_di_diff=ta_min_di,
        orderflow_tape_window_seconds=of_tape_win,
        orderflow_tape_max_events=of_tape_max,
        orderflow_tape_min_total_quote=of_tape_min_q,
        orderflow_tape_long_share=of_tape_long,
        orderflow_tape_short_share=of_tape_short,
        orderflow_book_levels=of_book_lv,
        orderflow_book_ratio_long=of_br_long,
        orderflow_book_ratio_short=of_br_short,
        orderflow_book_min_side_quote=of_book_min_side,
        orderflow_signal_mode=orderflow_signal_mode,
        orderflow_cooldown_seconds=of_cd,
        orderflow_ws_collect=orderflow_ws_collect,
    )
