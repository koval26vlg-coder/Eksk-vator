"""Лимитные заявки: бумажный режим или реальные API-ключи."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import ccxt.async_support as ccxt

from capital import CapitalGuard
from config import Settings
from orderbook import immediate_limit_buy_fill, immediate_limit_sell_fill
from paper_pnl import PaperSessionPnl
from risk import RiskManager

log = logging.getLogger(__name__)

# Не спамить WARNING на каждом тике WS, когда лимит уже достигнут.
_RISK_REJECT_LOG_THROTTLE_SEC = 15.0
_CAPITAL_REJECT_LOG_THROTTLE_SEC = 15.0


class OrderExecutor:
    """Выставление и снятие лимитных ордеров с проверкой RiskManager."""

    def __init__(
        self,
        settings: Settings,
        risk: RiskManager,
        capital: CapitalGuard | None = None,
    ) -> None:
        self._s = settings
        self._risk = risk
        self._capital = capital if capital is not None else CapitalGuard.from_settings(settings)
        self._clients: dict[str, ccxt.Exchange] = {}
        self._paper_orders: dict[str, dict[str, Any]] = {}
        self._paper_pnl = PaperSessionPnl(settings.fee_bps_per_side)
        self._last_risk_reject_log_mono: float = 0.0
        self._last_capital_reject_log_mono: float = 0.0

    def update_open_notional_for_order(self, order_id: str, remaining: float, price: float) -> None:
        """Синхронизировать риск с биржей: остаток лимитки в базе × цена (GTC)."""
        if remaining <= 1e-18:
            self._risk.release(order_id)
        else:
            self._risk.set_notional(order_id, remaining * price)

    @staticmethod
    def _normalize_fetched_order(o: dict[str, Any]) -> dict[str, Any]:
        filled = float(o.get("filled") or 0.0)
        rem_raw = o.get("remaining")
        if rem_raw is None:
            amt = float(o.get("amount") or 0.0)
            remaining = max(0.0, amt - filled)
        else:
            remaining = float(rem_raw)
        price = float(o.get("price") or 0.0)
        if price <= 0:
            price = float(o.get("average") or 0.0)
        raw_side = str(o.get("side") or "")
        side = raw_side.lower() if raw_side else ""
        return {
            "id": str(o.get("id", "")),
            "status": str(o.get("status") or "open"),
            "filled": filled,
            "remaining": remaining,
            "price": price,
            "side": side,
            "average": float(o.get("average") or 0.0),
        }

    @staticmethod
    def _terminal_fetched_order(order_id: str) -> dict[str, Any]:
        return {
            "id": order_id,
            "status": "canceled",
            "filled": 0.0,
            "remaining": 0.0,
            "price": 0.0,
            "side": "",
            "average": 0.0,
        }

    async def fetch_order(self, exchange_id: str, symbol: str, order_id: str) -> dict[str, Any] | None:
        """Актуальный статус с биржи (или paper-копия). None — временная ошибка, повторить позже."""
        if order_id.startswith("paper-"):
            po = self._paper_orders.get(order_id)
            return self._normalize_fetched_order(po) if po else None

        ex = await self._client(exchange_id)
        if not ex.markets:
            await ex.load_markets()
        try:
            raw = await ex.fetch_order(order_id, symbol)
            return self._normalize_fetched_order(raw)
        except ccxt.OrderNotFound:
            log.info("fetch_order: ордер не найден id=%s (снят/исполнен)", order_id)
            return self._terminal_fetched_order(order_id)
        except Exception as e:
            log.warning("fetch_order %s: %s", order_id, e)
            return None

    async def fetch_open_orders(
        self, exchange_id: str, symbol: str | None = None
    ) -> list[dict[str, Any]]:
        """Открытые ордера: symbol=None — все инструменты аккаунта (если биржа поддерживает)."""
        if self._s.paper:
            return []
        ex = await self._client(exchange_id)
        if not ex.markets:
            await ex.load_markets()
        try:
            raw_list = await ex.fetch_open_orders(symbol)
        except Exception as e:
            log.warning("fetch_open_orders %s %s: %s", exchange_id, symbol or "*", e)
            return []
        out: list[dict[str, Any]] = []
        for raw in raw_list:
            n = self._normalize_fetched_order(raw)
            n["symbol"] = str(raw.get("symbol") or "")
            n["type"] = str(raw.get("type") or "").lower()
            ts = raw.get("timestamp")
            n["timestamp"] = int(ts) if ts is not None else None
            out.append(n)
        return out

    async def fetch_order_book(self, exchange_id: str, symbol: str, limit: int) -> dict[str, Any] | None:
        """REST-стакан (live), тот же клиент что и для ордеров; для paper — None."""
        if self._s.paper:
            return None
        ex = await self._client(exchange_id)
        if not ex.markets:
            await ex.load_markets()
        if symbol not in ex.symbols:
            log.debug("fetch_order_book: нет пары %s на %s", symbol, exchange_id)
            return None
        try:
            return await ex.fetch_order_book(symbol, limit)
        except Exception as e:
            log.warning("fetch_order_book %s %s: %s", exchange_id, symbol, e)
            return None

    def get_paper_order(self, order_id: str) -> dict[str, Any] | None:
        return self._paper_orders.get(order_id)

    def paper_session_pnl_summary(self) -> str | None:
        """Строка для лога в конце сессии (paper); None если не было закрытых ордеров."""
        if not self._s.paper:
            return None
        if self._paper_pnl.closed_buys == 0 and self._paper_pnl.closed_sells == 0:
            return None
        return self._paper_pnl.summary_line()

    def paper_pnl_running_line(self) -> str | None:
        """Краткая строка для пульса paper: реализованный PnL и счётчики (даже если сделок ещё не было)."""
        if not self._s.paper:
            return None
        p = self._paper_pnl
        return (
            f"paper: реализ.≈{p.realized_pnl_quote:.4f} USDT, комисс.≈{p.fees_paid_quote:.4f}, "
            f"ордеров buy/sell закрыто {p.closed_buys}/{p.closed_sells}"
        )

    def paper_net_position_base(self, exchange_id: str, symbol: str) -> float | None:
        """Paper: net-инвентарь по базе (base). Live: None (нужны балансы/позиции)."""
        if not self._s.paper:
            return None
        return self._paper_pnl.net_position_base(exchange_id, symbol)

    def paper_net_position_base_symbol(self, symbol: str) -> float | None:
        """Paper: net-инвентарь по базе суммарно по всем биржам для symbol. Live: None."""
        if not self._s.paper:
            return None
        return self._paper_pnl.net_position_base_symbol(symbol)

    def paper_position_entry_vwap(self, exchange_id: str, symbol: str) -> tuple[float, float | None] | None:
        """Paper: (pos_base, entry_vwap). Live: None."""
        if not self._s.paper:
            return None
        return self._paper_pnl.position_entry_vwap(exchange_id, symbol)

    def paper_position_entry_vwap_symbol(self, symbol: str) -> tuple[float, float | None] | None:
        """Paper: (pos_base, entry_vwap) суммарно по всем биржам для symbol. Live: None."""
        if not self._s.paper:
            return None
        return self._paper_pnl.position_entry_vwap_symbol(symbol)

    def _paper_store(self, order: dict[str, Any]) -> None:
        oid = str(order.get("id", ""))
        if oid:
            self._paper_orders[oid] = order

    def _paper_remove(self, order_id: str) -> None:
        self._paper_orders.pop(order_id, None)

    def paper_apply_order_book(self, exchange_id: str, symbol: str, order_book: dict) -> list[dict[str, Any]]:
        """Применить новый стакан к открытым paper-ордерам (доливка остатка)."""
        if not self._s.paper:
            return []
        updated: list[dict[str, Any]] = []
        for oid, o in list(self._paper_orders.items()):
            if o.get("exchange_id") != exchange_id or o.get("symbol") != symbol:
                continue
            if o.get("status") not in ("open", "partial"):
                continue

            side = str(o.get("side", ""))
            price = float(o.get("price", 0.0))
            remaining = float(o.get("remaining", 0.0))
            filled = float(o.get("filled", 0.0))
            cum_quote = float(o.get("cum_quote", 0.0))

            if remaining <= 1e-18 or price <= 0:
                continue

            if side == "buy":
                d_filled, d_quote, _avg = immediate_limit_buy_fill(
                    price, remaining, order_book.get("asks") or []
                )
            else:
                d_filled, d_quote, _avg = immediate_limit_sell_fill(
                    price, remaining, order_book.get("bids") or []
                )

            if d_filled <= 1e-18:
                continue

            new_filled = filled + d_filled
            new_remaining = max(0.0, remaining - d_filled)
            new_cum_quote = cum_quote + d_quote
            avg = new_cum_quote / new_filled if new_filled > 1e-18 else float(o.get("average", price))
            status = "closed" if new_remaining <= 1e-18 else "partial"

            o["filled"] = new_filled
            o["remaining"] = new_remaining
            o["cum_quote"] = new_cum_quote
            o["average"] = avg
            o["status"] = status

            # В риск кладём только остаток по лимит-цене
            self._risk.set_notional(oid, new_remaining * price)

            log.info(
                "PAPER fill+ %s %s %s d_filled=%s remaining=%s avg=%s id=%s status=%s",
                side,
                symbol,
                exchange_id,
                d_filled,
                new_remaining,
                avg,
                oid,
                status,
            )
            if status == "closed":
                dpnl = self._paper_pnl.try_record_closed_order(o)
                if dpnl is not None:
                    self._capital.on_sell_realized_delta(dpnl, now_mono=time.monotonic())
                    log.info(
                        "paper PnL: по продаже ≈ %.4f USDT (сессия ≈ %.4f USDT)",
                        dpnl,
                        self._paper_pnl.realized_pnl_quote,
                    )
            updated.append(o)
        return updated

    def _trading_exchange_id(self) -> str:
        if self._s.trading_exchange:
            return self._s.trading_exchange
        return self._s.exchanges[0]

    async def _client(self, exchange_id: str) -> ccxt.Exchange:
        if exchange_id in self._clients:
            return self._clients[exchange_id]
        if not hasattr(ccxt, exchange_id):
            raise ValueError(f"Неизвестная биржа: {exchange_id}")
        if not self._s.api_key or not self._s.api_secret:
            raise RuntimeError(
                "Для live-ордеров задайте ключи: EXCHANGE_API_KEY/API_KEY/BYBIT_API_KEY и секрет"
            )
        cls = getattr(ccxt, exchange_id)
        opts: dict[str, Any] = {
            "apiKey": self._s.api_key,
            "secret": self._s.api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
        if self._s.api_passphrase:
            opts["password"] = self._s.api_passphrase
        ex: ccxt.Exchange = cls(opts)
        self._clients[exchange_id] = ex
        return ex

    async def place_limit(
        self,
        exchange_id: str,
        symbol: str,
        side: str,
        amount: float,
        price: float,
        order_book: dict | None = None,
        *,
        risk_priority: str = "normal",
    ) -> dict[str, Any] | None:
        """side: 'buy' | 'sell'. Возвращает dict ордера CCXT или paper-заглушку.

        В paper при переданном order_book симулируется немедленное частичное исполнение
        по глубине (уровни, доступные по цене лимитки); в риск попадает только остаток.
        """
        notional = amount * price
        now0 = time.monotonic()
        # CapitalGuard ограничивает "новые входы" (paper). Выходы (TP/SL/MAX_HOLD) должны проходить
        # даже при стопе по сессии/кулдауне, иначе позицию невозможно закрыть.
        if str(risk_priority) != "exit":
            ok_c, cap_reason = self._capital.can_open(
                realized_pnl_quote=self._paper_pnl.realized_pnl_quote,
                now_mono=now0,
                paper=self._s.paper,
            )
            if not ok_c:
                now = time.monotonic()
                if now - self._last_capital_reject_log_mono >= _CAPITAL_REJECT_LOG_THROTTLE_SEC:
                    self._last_capital_reject_log_mono = now
                    log.warning(
                        "Капитал: отказ в заявке (%s) (частые повторы сжаты: не чаще 1×/%ds)",
                        cap_reason,
                        int(_CAPITAL_REJECT_LOG_THROTTLE_SEC),
                    )
                else:
                    log.debug("Капитал: отказ (%s)", cap_reason)
                return None

        can_open = self._risk.can_open_exit(notional) if str(risk_priority) == "exit" else self._risk.can_open(notional)
        if not can_open:
            now = time.monotonic()
            extra = ""
            if self._s.strategy == "scalping" and self._s.auto_trade:
                extra = (
                    " Скальпинг+AUTO_TRADE: по умолчанию лимиты в config поднимаются до бирж×пар "
                    "(см. RISK_STRICT_RISK_LIMITS в .env.example); иначе увеличьте RISK_*."
                )
            prio = "exit" if str(risk_priority) == "exit" else "normal"
            msg = (
                "Риск: отказ в заявке[%s] notional=%.4f (лимиты: per_order=%s open=%s/%s total=%.4f/%.4f).%s"
                % (
                    prio,
                    notional,
                    self._risk.limits.max_notional_per_order,
                    self._risk.open_count,
                    self._risk.limits.max_open_orders,
                    self._risk.total_open_notional,
                    self._risk.limits.max_total_notional_open,
                    extra,
                )
            )
            if now - self._last_risk_reject_log_mono >= _RISK_REJECT_LOG_THROTTLE_SEC:
                self._last_risk_reject_log_mono = now
                log.warning("%s (частые повторы сжаты: не чаще 1×/%ds)", msg, int(_RISK_REJECT_LOG_THROTTLE_SEC))
            else:
                log.debug(msg)
            return None

        if self._s.paper:
            oid = f"paper-{uuid.uuid4().hex[:12]}"
            now = time.monotonic()

            if not order_book:
                self._risk.register(oid, notional)
                log.info(
                    "PAPER limit %s %s %s amount=%s price=%s id=%s",
                    side,
                    symbol,
                    exchange_id,
                    amount,
                    price,
                    oid,
                )
                order = {
                    "id": oid,
                    "exchange_id": exchange_id,
                    "symbol": symbol,
                    "side": side,
                    "amount": amount,
                    "price": price,
                    "filled": 0.0,
                    "remaining": amount,
                    "average": price,
                    "cum_quote": 0.0,
                    "created_mono": now,
                    "last_reprice_mono": now,
                    "status": "open",
                }
                self._paper_store(order)
                return order

            if side == "buy":
                filled_base, quote_flow, avg_px = immediate_limit_buy_fill(
                    price, amount, order_book.get("asks") or []
                )
            else:
                filled_base, quote_flow, avg_px = immediate_limit_sell_fill(
                    price, amount, order_book.get("bids") or []
                )

            remaining_base = max(0.0, amount - filled_base)
            open_notional = remaining_base * price

            if remaining_base <= amount * 1e-12 and filled_base > 1e-18:
                status = "closed"
            elif filled_base > 1e-18 and remaining_base > amount * 1e-12:
                status = "partial"
            else:
                status = "open"

            if open_notional > 1e-12:
                self._risk.register(oid, open_notional)

            log.info(
                "PAPER limit %s %s %s req=%s price=%s filled=%s remaining=%s avg=%s id=%s status=%s",
                side,
                symbol,
                exchange_id,
                amount,
                price,
                filled_base,
                remaining_base,
                avg_px if filled_base > 0 else price,
                oid,
                status,
            )
            order = {
                "id": oid,
                "exchange_id": exchange_id,
                "symbol": symbol,
                "side": side,
                "amount": amount,
                "price": price,
                "filled": filled_base,
                "remaining": remaining_base,
                "average": avg_px if filled_base > 0 else price,
                "status": status,
                "quote_flow": quote_flow,
                "cum_quote": float(quote_flow) if filled_base > 0 else 0.0,
                "created_mono": now,
                "last_reprice_mono": now,
            }
            self._paper_store(order)
            if str(order.get("status", "")).lower() == "closed":
                dpnl = self._paper_pnl.try_record_closed_order(order)
                if dpnl is not None:
                    self._capital.on_sell_realized_delta(dpnl, now_mono=time.monotonic())
                    log.info(
                        "paper PnL: по продаже ≈ %.4f USDT (сессия ≈ %.4f USDT)",
                        dpnl,
                        self._paper_pnl.realized_pnl_quote,
                    )
            return order

        ex = await self._client(exchange_id)
        if not ex.markets:
            await ex.load_markets()
        amount_p = float(ex.amount_to_precision(symbol, amount))
        price_p = float(ex.price_to_precision(symbol, price))
        params: dict[str, Any] = {"timeInForce": "GTC"}
        order = await ex.create_order(symbol, "limit", side, amount_p, price_p, params)
        oid = str(order.get("id", ""))
        if oid:
            self._risk.register(oid, amount_p * price_p)
        log.info("LIVE limit id=%s %s %s @ %s", oid, side, symbol, price_p)
        return order

    async def cancel_order(self, exchange_id: str, symbol: str, order_id: str) -> bool:
        if order_id.startswith("paper-"):
            self._paper_remove(order_id)
            self._risk.release(order_id)
            log.info("PAPER cancel %s %s", symbol, order_id)
            return True
        ex = await self._client(exchange_id)
        try:
            await ex.cancel_order(order_id, symbol)
        except Exception as e:
            log.warning("cancel %s: %s", order_id, e)
            return False
        self._risk.release(order_id)
        return True

    async def close(self) -> None:
        for ex in self._clients.values():
            await ex.close()
        self._clients.clear()
        self._paper_orders.clear()
        self._paper_pnl.reset()
        self._capital.reset()
