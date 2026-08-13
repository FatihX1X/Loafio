from __future__ import annotations

import contextlib
import logging
import logging.handlers
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import loaf
from loaf import LoafClient

from .config import BotConfig
from .domain import LiveState, OrderState, Quote, value
from .storage import SessionRecord, Store
from .strategy import MakerStrategy

logger = logging.getLogger("loaf_bot")


class PreflightError(RuntimeError):
    pass


class RiskLockTriggered(RuntimeError):
    pass


def configure_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)
        rotating = logging.handlers.RotatingFileHandler(
            log_dir / "loaf-maker.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        rotating.setFormatter(formatter)
        root.addHandler(rotating)


class MakerBot:
    def __init__(
        self,
        config: BotConfig,
        *,
        client: LoafClient | None = None,
        store: Store | None = None,
    ) -> None:
        self.config = config
        self.client = client or LoafClient(
            api_key=config.api_key,
            base_url=config.base_url,
            max_retries=3,
        )
        self.store = store or Store(config.db_path)
        self.strategy = MakerStrategy(config)
        self.state = LiveState()
        self.session: SessionRecord | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._ws: Any = None
        self._last_equity_persist = 0.0
        self._last_summary = 0.0
        self._last_metrics_persist = 0.0
        self._last_leaderboard_persist = 0.0
        self._placing_blocked_until = 0.0
        self._risk_breach_since: float | None = None
        self._seen_trade_ids: set[str] = set()
        self._seen_trade_order: deque[str] = deque(maxlen=5_000)
        self._ws_auth_ok = threading.Event()
        self._portfolio_subscription_ok = threading.Event()

    # ----------------------------- preflight ----------------------------- #

    def preflight(self) -> None:
        component = self.client.portfolio.component()
        self._seed_portfolio(component)
        equity = float(value(component, "portfolioValue", self.state.estimated_equity()) or 0.0)
        if equity <= 0:
            raise PreflightError("Portfolio equity is missing or zero.")
        self.session = self.store.open_session(
            self.config.session_id,
            equity,
            self.state.lifetime_volume,
            self.config.loss_fraction,
        )
        if self.session.status == "RISK_LOCKED":
            raise RiskLockTriggered("This watchdog session is risk-locked.")
        self.store.event(self.config.session_id, "process_started", {"equity": equity})

        competition = self.client.competition.info()
        featured = value(competition, "featuredRound")
        if featured is None or str(value(featured, "status", "")) != "ACTIVE":
            raise PreflightError("The featured competition round is not ACTIVE.")
        asset = value(featured, "newAssetProperty")
        if str(value(asset, "tokenName", "")).lower() != self.config.target_token:
            raise PreflightError("The active competition asset is not terafab.")
        maker_fee = int(value(competition, "makerFeeBps", -1))
        taker_fee = int(value(competition, "takerFeeBps", -1))
        if maker_fee != 0 or taker_fee != 10:
            raise PreflightError(
                f"Fee schedule changed (maker={maker_fee}, taker={taker_fee} bps); "
                "refusing live mode."
            )
        with self._lock:
            self.state.round_ends_at = float(value(featured, "endsAt", 0) or 0)

        queue = self.client.competition.queue_position()
        if value(queue, "position") is not None:
            raise PreflightError(f"Account is still queued at position {value(queue, 'position')}.")

        listing = self.client.market.properties()
        properties = value(listing, "properties", []) or []
        target = next(
            (
                item
                for item in properties
                if str(value(item, "tokenName", "")) == self.config.target_token
            ),
            None,
        )
        if target is None or str(value(target, "status", "")) != "LIVE":
            raise PreflightError("Terafab is not a LIVE tradeable property.")
        with self._lock:
            self.state.market_price = float(value(target, "marketPrice", 0) or 0)
            self.state.record_price(self.state.market_price)

        detail = self.client.market.property(self.config.target_token)
        if bool(value(value(detail, "property", {}), "isHalted", False)):
            raise PreflightError("Terafab trading is halted.")
        self._seed_book(value(detail, "orderBook"))
        board = self.client.leaderboard.get()
        self._on_leaderboard(board)
        self._reconcile_active_orders()
        logger.info(
            "Preflight passed: equity=%.2f floor=%.2f inventory=%.2f",
            equity,
            self.session.loss_floor,
            self.state.inventory_notional(),
        )

    def _seed_portfolio(self, component: Any) -> None:
        positions = value(component, "positions", []) or []
        target = next(
            (p for p in positions if str(value(p, "tokenName", "")) == self.config.target_token),
            None,
        )
        with self._lock:
            self.state.cash = float(value(component, "cash", 0) or 0)
            self.state.frozen = float(value(component, "frozen", 0) or 0)
            self.state.portfolio_value = float(value(component, "portfolioValue", 0) or 0)
            self.state.lifetime_volume = float(value(component, "lifetimeVolume", 0) or 0)
            if target is not None:
                self.state.available_quantity = float(value(target, "quantity", 0) or 0)
                self.state.total_quantity = float(
                    value(target, "totalQuantity", self.state.available_quantity) or 0
                )
                market = float(value(target, "marketPrice", 0) or 0)
                if market > 0:
                    self.state.market_price = market

    def _seed_book(self, book: Any) -> None:
        if not book:
            return
        bids = value(book, "bids", []) or []
        asks = value(book, "asks", []) or []
        with self._lock:
            self.state.best_bid = float(value(bids[0], "price")) if bids else None
            self.state.best_ask = float(value(asks[0], "price")) if asks else None
            self.state.book_updated_mono = time.monotonic()
            mid = self.state.mid()
            if mid:
                self.state.record_price(mid)

    def _reconcile_active_orders(self) -> None:
        response = self.client.history.active_orders()
        active = value(response, "activeOrders", []) or []
        known = {row["order_id"]: row for row in self.store.active_orders(self.config.session_id)}
        seen: set[int] = set()
        for item in active:
            order_id = int(value(item, "orderId", value(item, "id", 0)) or 0)
            if not order_id:
                continue
            seen.add(order_id)
            row = known.get(order_id)
            if row is None:
                raise PreflightError(
                    f"Unknown active order #{order_id}; run 'python -m loaf_bot flatten' first."
                )
            side = str(value(item, "side", row["side"]))
            remaining = float(value(item, "quantityLeft", row["remaining"]) or 0)
            order = OrderState(
                order_id,
                side,
                float(value(item, "price", row["price"]) or row["price"]),
                float(value(item, "quantity", row["quantity"]) or row["quantity"]),
                remaining,
                str(value(item, "status", "OPEN")),
            )
            self.state.bot_orders[side] = order
            self.store.save_order(
                self.config.session_id,
                order_id,
                side,
                order.price,
                order.quantity,
                remaining,
                order.status,
            )
        for order_id in known:
            if order_id not in seen:
                self.store.update_order(order_id, 0.0, "CLOSED_ON_RECONCILE")

    # ------------------------------ websocket ---------------------------- #

    def _wire_websocket(self) -> None:
        ws = self.client.websocket(auto_reconnect=True, reconnect_delay=2.0)
        ws.on_orderbook(self._on_orderbook)
        ws.on_mark_price(self._on_mark_price)
        ws.on_trades(self._on_trades)
        ws.on_leaderboard(self._on_leaderboard)
        ws.on_balances(self._on_balances)
        ws.on_position(self._on_position)
        ws.on_order_status(self._on_order_event)
        ws.on_order_update(self._on_order_event)
        ws.on_trade(self._on_fill)
        ws.on_lifetime_volume(self._on_lifetime_volume)
        ws.on_connect(self._on_connect)
        ws.on_transport_error(self._on_transport_error)
        ws.on_error(self._on_ws_error)
        ws.on("auth_result", self._on_auth_result)
        ws.on("subscription_confirmed", self._on_subscription_confirmed)
        ws.subscribe_orderbook(self.config.target_token)
        ws.subscribe_mark_price(self.config.target_token)
        ws.subscribe_trades(self.config.target_token)
        ws.subscribe_leaderboard()
        ws.subscribe_portfolio(self.config.user_id)
        self._ws = ws

    def _on_connect(self, _message: Any) -> None:
        with self._lock:
            self.state.ws_connected = True
        self.store.event(self.config.session_id, "ws_connected", {})

    def _on_transport_error(self, error: Any) -> None:
        with self._lock:
            self.state.ws_connected = False
        self.store.event(self.config.session_id, "ws_transport_error", {"error": str(error)})

    def _on_ws_error(self, message: Any) -> None:
        text = str(value(message, "message", value(message, "error", message)))
        with self._lock:
            self.state.fatal_ws_error = f"WebSocket server error: {text}"
        self.store.event(self.config.session_id, "ws_server_error", {"error": text})

    def _on_auth_result(self, message: Any) -> None:
        payload = value(message, "data", message)
        raw_ok = value(payload, "success", value(payload, "authenticated", False))
        status = str(value(payload, "status", "")).lower()
        ok = bool(raw_ok) or status in {"ok", "success", "authenticated"}
        if not ok:
            self._on_ws_error({"message": "private WebSocket authentication rejected"})
            return
        self._ws_auth_ok.set()
        self.store.event(self.config.session_id, "ws_authenticated", {})

    def _on_subscription_confirmed(self, message: Any) -> None:
        payload = value(message, "data", message)
        channels = value(payload, "channels", value(payload, "channel", []))
        if isinstance(channels, str):
            channels = [channels]
        expected = f"portfolio:{self.config.user_id}"
        if expected in (channels or []):
            self._portfolio_subscription_ok.set()
            self.store.event(
                self.config.session_id,
                "portfolio_subscription_confirmed",
                {"channel": expected},
            )

    def _on_orderbook(self, message: Any) -> None:
        self._seed_book(message)

    def _on_mark_price(self, message: Any) -> None:
        price = float(value(message, "price", 0) or 0)
        if price <= 0:
            return
        with self._lock:
            self.state.market_price = price
            self.state.record_price(price)

    @staticmethod
    def _aggressor_side(raw: Any) -> str:
        if raw in (0, "0"):
            return "BUY"
        if raw in (1, "1"):
            return "SELL"
        return str(raw).upper()

    def _on_trades(self, message: Any) -> None:
        now = time.monotonic()
        trades = value(message, "trades", []) or []
        with self._lock:
            for trade in trades:
                trade_id = str(value(trade, "tradeId", ""))
                if trade_id and trade_id in self._seen_trade_ids:
                    continue
                if trade_id:
                    if len(self._seen_trade_order) == self._seen_trade_order.maxlen:
                        expired = self._seen_trade_order.popleft()
                        self._seen_trade_ids.discard(expired)
                    self._seen_trade_order.append(trade_id)
                    self._seen_trade_ids.add(trade_id)
                price = float(value(trade, "price", 0) or 0)
                quantity = float(value(trade, "quantity", 0) or 0)
                side = self._aggressor_side(value(trade, "aggressorSide", ""))
                if price > 0 and quantity > 0 and side in {"BUY", "SELL"}:
                    self.state.trades.append((now, side, price * quantity))

    def _on_leaderboard(self, message: Any) -> None:
        board = value(message, "leaderboard", message)
        entries = value(board, "entries", []) or []
        third = next((item for item in entries if int(value(item, "rank", 0) or 0) == 3), None)
        if third is not None:
            rank3_volume = float(value(third, "volume", 0) or 0)
            with self._lock:
                self.state.rank3_samples.append((time.time(), rank3_volume))
            now = time.monotonic()
            if now - self._last_leaderboard_persist >= 5.0:
                self.store.event(
                    self.config.session_id,
                    "leaderboard_rank3",
                    {"volume": rank3_volume},
                )
                self._last_leaderboard_persist = now

    def _on_balances(self, message: Any) -> None:
        payload = value(message, "balances", message)
        with self._lock:
            if value(payload, "cash") is not None:
                self.state.cash = float(value(payload, "cash") or 0)
            if value(payload, "frozen") is not None:
                self.state.frozen = float(value(payload, "frozen") or 0)
            if value(payload, "portfolioValue") is not None:
                self.state.portfolio_value = float(value(payload, "portfolioValue") or 0)

    def _on_position(self, message: Any) -> None:
        position = value(message, "position", message)
        token = str(value(position, "tokenName", self.config.target_token))
        if token != self.config.target_token:
            return
        with self._lock:
            if value(position, "quantity") is not None:
                self.state.available_quantity = float(value(position, "quantity") or 0)
            if value(position, "totalQuantity") is not None:
                self.state.total_quantity = float(value(position, "totalQuantity") or 0)
            elif value(position, "quantity") is not None:
                self.state.total_quantity = max(
                    self.state.total_quantity, self.state.available_quantity
                )
            if value(position, "marketPrice") is not None:
                self.state.market_price = float(value(position, "marketPrice") or 0)

    def _on_order_event(self, message: Any) -> None:
        order_id = int(value(message, "orderId", value(message, "id", 0)) or 0)
        status = str(value(message, "status", "OPEN"))
        remaining = float(
            value(message, "quantityLeft", value(message, "remainingQuantity", 0)) or 0
        )
        with self._lock:
            for side, order in list(self.state.bot_orders.items()):
                if order.order_id != order_id:
                    continue
                order.status = status
                if value(message, "quantityLeft") is not None or value(
                    message, "remainingQuantity"
                ) is not None:
                    order.remaining = remaining
                self.store.update_order(order_id, order.remaining, status)
                if status in {"FILLED", "CANCELLED", "REJECTED"}:
                    self.state.bot_orders.pop(side, None)
                break

    def _on_fill(self, message: Any) -> None:
        trade = value(message, "trade", message)
        payload = {
            "tradeId": value(trade, "tradeId"),
            "side": value(trade, "side"),
            "price": value(trade, "price"),
            "quantity": value(trade, "quantity"),
            "fee": value(trade, "fee", 0),
        }
        self.store.event(self.config.session_id, "fill", payload)
        if float(payload["fee"] or 0) > 0:
            logger.warning("Taker/non-zero fee fill observed: %s", payload)

    def _on_lifetime_volume(self, message: Any) -> None:
        volume = value(message, "lifetimeVolume", value(message, "volume"))
        if volume is not None:
            with self._lock:
                self.state.lifetime_volume = float(volume)

    # ------------------------------ orders ------------------------------- #

    def _cancel_side(self, side: str) -> None:
        with self._lock:
            order = self.state.bot_orders.get(side)
        if order is None or not order.active:
            return
        if order.status == "CANCEL_REQUESTED":
            return
        try:
            self.client.orders.cancel(order.order_id)
            status = "CANCEL_REQUESTED"
        except (loaf.LoafConflictError, loaf.LoafNotFoundError):
            status = "CLOSED_DURING_CANCEL"
        except loaf.LoafError as exc:
            logger.warning("Cancel failed for #%s: %s", order.order_id, exc)
            self.store.event(
                self.config.session_id,
                "cancel_failed",
                {"orderId": order.order_id, "error": str(exc)},
            )
            return
        with self._lock:
            order.status = status
            if status == "CLOSED_DURING_CANCEL":
                self.state.bot_orders.pop(side, None)
        self.store.update_order(order.order_id, order.remaining, status)

    def _cancel_owned(self) -> None:
        self._cancel_side("BUY")
        self._cancel_side("SELL")

    def _place(self, quote: Quote) -> None:
        now = time.monotonic()
        if now < self._placing_blocked_until:
            return
        with self._lock:
            if now - self.state.book_updated_mono > self.config.stale_book_seconds:
                return
            if quote.side == "BUY" and (
                self.state.best_ask is None or quote.price >= self.state.best_ask
            ):
                return
            if quote.side == "SELL" and (
                self.state.best_bid is None or quote.price <= self.state.best_bid
            ):
                return
            opposite = self.state.bot_orders.get("SELL" if quote.side == "BUY" else "BUY")
            if opposite and opposite.active:
                crosses_self = (
                    quote.side == "BUY" and quote.price >= opposite.price
                ) or (quote.side == "SELL" and quote.price <= opposite.price)
                if crosses_self:
                    logger.error("Self-trade guard blocked %s quote", quote.side)
                    return
        nonce_value: str | None = None
        try:
            nonce = self.client.orders.nonce()
            nonce_value = str(value(nonce, "nonce"))
            self.store.mint_nonce(self.config.session_id, nonce_value)
            response = self.client.orders.create(
                self.config.target_token,
                side=quote.side,
                quantity=quote.quantity,
                type="LIMIT",
                price=quote.price,
                time_in_force="GTC",
                deadline=0,
                nonce=nonce_value,
            )
            if not bool(value(response, "success", False)):
                raise RuntimeError(str(value(response, "errorMessage", "Order rejected")))
            order_id = int(value(response, "orderId"))
            self.store.update_nonce(nonce_value, "SUBMITTED", order_id)
            order = OrderState(order_id, quote.side, quote.price, quote.quantity, quote.quantity)
            with self._lock:
                self.state.bot_orders[quote.side] = order
            self.store.save_order(
                self.config.session_id,
                order_id,
                quote.side,
                quote.price,
                quote.quantity,
                quote.quantity,
                "OPEN",
            )
            logger.info(
                "Placed %s #%s %.1f @ %.2f (%.2f USDL)",
                quote.side,
                order_id,
                quote.quantity,
                quote.price,
                quote.notional,
            )
        except loaf.LoafRateLimitError as exc:
            if nonce_value:
                self.store.update_nonce(nonce_value, "RATE_LIMITED")
            delay = float(exc.retry_after or 5.0)
            self._placing_blocked_until = time.monotonic() + min(60.0, delay)
            logger.warning("Order rate-limited; pausing %.1fs", delay)
        except (loaf.LoafServiceUnavailableError, loaf.LoafConnectionError) as exc:
            if nonce_value:
                self.store.update_nonce(nonce_value, "AMBIGUOUS")
            logger.error("Ambiguous order submission: %s", exc)
            self._placing_blocked_until = time.monotonic() + 10.0
            self._reconcile_ambiguous(quote)
        except loaf.LoafError as exc:
            if nonce_value:
                self.store.update_nonce(nonce_value, "REJECTED")
            self.store.event(
                self.config.session_id,
                "order_rejected",
                {
                    "side": quote.side,
                    "price": quote.price,
                    "quantity": quote.quantity,
                    "error": str(exc),
                },
            )
            logger.warning("Order rejected: %s", exc)

    def _reconcile_ambiguous(self, quote: Quote) -> None:
        try:
            active = value(self.client.history.active_orders(), "activeOrders", []) or []
        except loaf.LoafError as exc:
            logger.error("Ambiguous-order reconciliation failed: %s", exc)
            return
        candidates = []
        known = self.store.known_active_order_ids(self.config.session_id)
        for item in active:
            order_id = int(value(item, "orderId", value(item, "id", 0)) or 0)
            if order_id in known:
                continue
            if str(value(item, "tokenName", self.config.target_token)) != self.config.target_token:
                continue
            if str(value(item, "side", quote.side)) != quote.side:
                continue
            price = float(value(item, "price", quote.price) or quote.price)
            if abs(price - quote.price) > 0.001:
                continue
            candidates.append((order_id, item))
        if len(candidates) != 1:
            self.store.event(
                self.config.session_id,
                "ambiguous_unresolved",
                {"side": quote.side, "price": quote.price, "candidateCount": len(candidates)},
            )
            return
        order_id, item = candidates[0]
        remaining = float(value(item, "quantityLeft", quote.quantity) or quote.quantity)
        order = OrderState(order_id, quote.side, quote.price, quote.quantity, remaining)
        with self._lock:
            self.state.bot_orders[quote.side] = order
        self.store.save_order(
            self.config.session_id,
            order_id,
            quote.side,
            quote.price,
            quote.quantity,
            remaining,
            "OPEN",
        )
        logger.warning("Adopted ambiguous submission as order #%s", order_id)

    def _ensure_quote(self, side: str, desired: Quote | None) -> None:
        with self._lock:
            current = self.state.bot_orders.get(side)
        if desired is None:
            self._cancel_side(side)
            return
        if current and current.active:
            same_price = abs(current.price - desired.price) < 0.001
            enough_size = current.remaining >= desired.quantity * 0.50
            not_oversized = current.remaining <= desired.quantity * 1.10
            if same_price and enough_size and not_oversized:
                return
            self._cancel_side(side)
            with self._lock:
                still_active = self.state.bot_orders.get(side)
            if still_active and still_active.active:
                return
        self._place(desired)

    # ------------------------------ lifecycle ---------------------------- #

    def _risk_check(self) -> None:
        assert self.session is not None
        with self._lock:
            equity = self.state.estimated_equity()
        if equity <= 0:
            return
        now = time.monotonic()
        if now - self._last_equity_persist >= 2.0:
            self.store.update_equity(self.config.session_id, equity)
            self._last_equity_persist = now
        if equity > self.session.loss_floor:
            self._risk_breach_since = None
            return

        # Portfolio deltas may arrive out of order (cash before position or vice versa).
        # Confirm a suspected breach with a one-shot REST snapshot before liquidating.
        try:
            component = self.client.portfolio.component()
            self._seed_portfolio(component)
            confirmed = float(
                value(component, "portfolioValue", self.state.estimated_equity()) or 0
            )
            if confirmed > self.session.loss_floor:
                self._risk_breach_since = None
                return
            equity = confirmed
        except loaf.LoafError as exc:
            logger.error("Risk-breach confirmation failed: %s", exc)
            now = time.monotonic()
            if self._risk_breach_since is None:
                self._risk_breach_since = now
                return
            if now - self._risk_breach_since < 2.0:
                return

        if equity <= self.session.loss_floor:
            reason = f"equity {equity:.2f} <= session floor {self.session.loss_floor:.2f}"
            self.store.lock_session(self.config.session_id, reason, equity)
            self.emergency_flatten()
            raise RiskLockTriggered(reason)

    def _tick(self) -> None:
        assert self.session is not None
        self._risk_check()
        with self._lock:
            decision = self.strategy.decide(
                self.state,
                session_start_volume=self.session.start_volume,
            )
            inventory = self.state.inventory_notional()
            equity = self.state.estimated_equity()
            own_volume = max(0.0, self.state.lifetime_volume - self.session.start_volume)
            podium_target, _rank3_rate = self.strategy.podium_target(self.state)
        now = time.monotonic()
        if now - self._last_metrics_persist >= 2.0:
            self.store.update_metrics(
                self.config.session_id,
                equity=equity,
                inventory=inventory,
                session_volume=own_volume,
                podium_target=podium_target,
                catchup=decision.catchup,
            )
            self._last_metrics_persist = now
        if decision.pause_reason:
            self._cancel_owned()
            if now - self._last_summary >= 5:
                logger.warning("Paused: %s", decision.pause_reason)
                self._last_summary = now
            return
        self._ensure_quote("BUY", decision.bid)
        self._ensure_quote("SELL", decision.ask)
        if now - self._last_summary >= 10:
            logger.info(
                "equity=%.2f inventory=%.2f sessionVolume=%.2f catchup=%s",
                equity,
                inventory,
                own_volume,
                decision.catchup,
            )
            self._last_summary = now

    def run(self) -> None:
        self.preflight()
        self._wire_websocket()
        self._ws.start()
        if not self._ws.wait_until_connected(timeout=10):
            raise PreflightError("WebSocket did not connect within 10 seconds.")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with self._lock:
                fresh = time.monotonic() - self.state.book_updated_mono <= 2
                fatal = self.state.fatal_ws_error
            if fatal:
                raise PreflightError(fatal)
            private_ready = self._ws_auth_ok.is_set() and self._portfolio_subscription_ok.is_set()
            if fresh and private_ready:
                break
            time.sleep(0.1)
        else:
            missing = []
            if not fresh:
                missing.append("fresh order book")
            if not self._ws_auth_ok.is_set():
                missing.append("private auth_result")
            if not self._portfolio_subscription_ok.is_set():
                missing.append("portfolio subscription confirmation")
            raise PreflightError("WebSocket readiness failed: " + ", ".join(missing))
        logger.info("LIVE autonomous maker started; no confirmation gate is active.")
        while not self._stop.is_set():
            self._tick()
            self._stop.wait(self.config.loop_interval_seconds)

    def request_stop(self) -> None:
        self._stop.set()

    def emergency_flatten(self) -> None:
        logger.critical("Emergency flatten started")
        try:
            self.client.orders.cancel_all()
        except loaf.LoafError as exc:
            logger.error("cancel-all failed during emergency: %s", exc)
        for attempt in range(3):
            try:
                component = self.client.portfolio.component()
                self._seed_portfolio(component)
                quantity = self.state.available_quantity
                if quantity <= 0:
                    return
                self.client.orders.market_sell(
                    self.config.target_token, quantity=round(quantity, 1)
                )
                self.store.event(
                    self.config.session_id,
                    "emergency_market_sell",
                    {"quantity": quantity, "attempt": attempt + 1},
                )
                time.sleep(2)
            except (loaf.LoafServiceUnavailableError, loaf.LoafConnectionError) as exc:
                logger.error("Emergency sell ambiguous; reconciling before retry: %s", exc)
                time.sleep(2)
            except loaf.LoafError as exc:
                logger.error("Emergency sell failed: %s", exc)
                time.sleep(2)

    def graceful_flatten(self) -> None:
        logger.info("Graceful flatten: cancelling maker orders")
        self._cancel_owned()
        deadline = time.monotonic() + self.config.graceful_flatten_seconds
        passive_order_id: int | None = None
        try:
            component = self.client.portfolio.component()
            self._seed_portfolio(component)
            quantity = self.state.available_quantity
            with self._lock:
                ask = self.state.best_ask
            if quantity > 0 and ask:
                nonce = self.client.orders.nonce()
                nonce_value = str(value(nonce, "nonce"))
                self.store.mint_nonce(self.config.session_id, nonce_value)
                response = self.client.orders.create(
                    self.config.target_token,
                    side="SELL",
                    quantity=round(quantity, 1),
                    type="LIMIT",
                    price=ask,
                    time_in_force="GTC",
                    deadline=0,
                    nonce=nonce_value,
                )
                passive_order_id = int(value(response, "orderId", 0) or 0)
                self.store.update_nonce(nonce_value, "GRACEFUL_EXIT", passive_order_id or None)
            while passive_order_id and time.monotonic() < deadline:
                time.sleep(2)
                component = self.client.portfolio.component()
                self._seed_portfolio(component)
                if self.state.available_quantity <= 0:
                    return
        except loaf.LoafError as exc:
            logger.warning("Passive graceful exit failed: %s", exc)
        if passive_order_id:
            with contextlib.suppress(loaf.LoafError):
                self.client.orders.cancel(passive_order_id)
        try:
            component = self.client.portfolio.component()
            self._seed_portfolio(component)
            quantity = self.state.available_quantity
            if quantity > 0:
                self.client.orders.market_sell(
                    self.config.target_token, quantity=round(quantity, 1)
                )
        except loaf.LoafError as exc:
            logger.error("Final graceful market sell failed: %s", exc)

    def close(self, *, graceful: bool = False) -> None:
        if graceful and self.session and self.session.status == "ACTIVE":
            self.graceful_flatten()
            self.store.finish_session(self.config.session_id)
        if self._ws is not None:
            self._ws.stop()
        self.client.close()
        self.store.close()


def flatten_account(config: BotConfig) -> None:
    configure_logging(config.log_dir)
    client = LoafClient(api_key=config.api_key, base_url=config.base_url, max_retries=3)
    store = Store(config.db_path)
    bot = MakerBot(config, client=client, store=store)
    try:
        client.orders.cancel_all()
        component = client.portfolio.component()
        bot._seed_portfolio(component)
        quantity = bot.state.available_quantity
        if quantity > 0:
            client.orders.market_sell(config.target_token, quantity=round(quantity, 1))
            logger.warning("Flatten submitted market SELL for %.1f terafab", quantity)
        else:
            logger.info("No available terafab position to flatten")
    finally:
        client.close()
        store.close()
