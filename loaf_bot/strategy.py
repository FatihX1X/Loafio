from __future__ import annotations

import time
from dataclasses import dataclass

from .config import BotConfig
from .domain import LiveState, Quote, quantity_for_notional, round_price


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    bid: Quote | None
    ask: Quote | None
    pause_reason: str | None = None
    catchup: bool = False
    pace_mode: str = "normal"


class MakerStrategy:
    def __init__(self, config: BotConfig) -> None:
        self.config = config

    def podium_target(self, state: LiveState, now_wall: float | None = None) -> tuple[float, float]:
        now = time.time() if now_wall is None else now_wall
        samples = list(state.rank3_samples)
        if not samples:
            return 0.0, 0.0
        current = samples[-1][1]
        if len(samples) < 2 or state.round_ends_at <= now:
            return current * 1.10, 0.0
        cutoff_15m = now - 900
        cutoff_60m = now - 3600

        def rate_after(cutoff: float) -> float:
            first = next((sample for sample in samples if sample[0] >= cutoff), samples[0])
            elapsed = samples[-1][0] - first[0]
            return 0.0 if elapsed <= 0 else max(0.0, (current - first[1]) / elapsed)

        rate = max(rate_after(cutoff_15m), rate_after(cutoff_60m))
        projected = current + rate * max(0.0, state.round_ends_at - now)
        return projected * 1.10, rate

    def pace_mode(self, state: LiveState, now_wall: float) -> str:
        target, _ = self.podium_target(state, now_wall)
        if target <= 0 or state.round_ends_at <= now_wall:
            return "normal"
        # Leaderboard volume is round-scoped. Until our own entry can be matched,
        # lifetime volume is a better restart-stable fallback than session delta.
        source_volume = state.lifetime_volume if state.round_volume is None else state.round_volume
        own_volume = max(0.0, source_volume)
        ratio = own_volume / target
        if ratio < self.config.sprint_pace_ratio:
            return "sprint"
        if ratio < self.config.catchup_pace_ratio:
            return "catchup"
        return "normal"

    def decide(
        self,
        state: LiveState,
        *,
        session_start_volume: float,
        now_mono: float | None = None,
        now_wall: float | None = None,
    ) -> StrategyDecision:
        mono = time.monotonic() if now_mono is None else now_mono
        wall = time.time() if now_wall is None else now_wall
        if not state.ws_connected:
            return StrategyDecision(None, None, "websocket disconnected")
        if state.fatal_ws_error:
            return StrategyDecision(None, None, state.fatal_ws_error)
        if state.best_bid is None or state.best_ask is None:
            return StrategyDecision(None, None, "empty order book")
        if state.best_bid >= state.best_ask:
            return StrategyDecision(None, None, "crossed order book")
        if mono - state.book_updated_mono > self.config.stale_book_seconds:
            return StrategyDecision(None, None, "stale order book")
        if mono < state.pause_until_mono:
            return StrategyDecision(None, None, "toxicity cooldown")
        volatility = state.recent_volatility_bps(self.config.volatility_window_seconds, mono)
        if volatility > self.config.volatility_limit_bps:
            return StrategyDecision(None, None, f"volatility {volatility:.1f} bps")
        mid = state.mid()
        assert mid is not None
        if state.market_price > 0:
            divergence = abs(state.market_price - mid) / mid * 10_000
            if divergence > self.config.mark_divergence_limit_bps:
                return StrategyDecision(None, None, f"mark divergence {divergence:.1f} bps")

        spread_cents = round((state.best_ask - state.best_bid) * 100)
        bid_price = state.best_bid
        ask_price = state.best_ask
        if spread_cents >= 3:
            bid_price = round_price(state.best_bid + 0.01)
            ask_price = round_price(state.best_ask - 0.01)
        if bid_price >= ask_price:
            return StrategyDecision(None, None, "no passive spread")

        pace_mode = self.pace_mode(state, wall)
        catchup = pace_mode != "normal"
        if pace_mode == "sprint":
            full = self.config.sprint_quote_usdl
        elif pace_mode == "catchup":
            full = self.config.catchup_quote_usdl
        else:
            full = self.config.base_quote_usdl
        reduced = full / 2
        inventory = state.inventory_notional()
        active_buy = state.bot_orders.get("BUY")
        active_sell = state.bot_orders.get("SELL")
        reserved_buy = (
            active_buy.remaining * active_buy.price if active_buy and active_buy.active else 0.0
        )
        reserved_sell_quantity = (
            active_sell.remaining if active_sell and active_sell.active else 0.0
        )
        buy_notional = full
        sell_notional = full
        if inventory < self.config.lower_inventory_usdl:
            buy_notional, sell_notional = full, reduced
        elif inventory > self.config.upper_inventory_usdl:
            buy_notional, sell_notional = reduced, full
        if inventory >= self.config.max_inventory_usdl:
            buy_notional = 0.0
        else:
            buy_notional = min(
                buy_notional,
                max(0.0, self.config.max_inventory_usdl - inventory),
                max(0.0, state.cash + reserved_buy - 10.0),
            )
        effective_sell_quantity = state.available_quantity + reserved_sell_quantity
        sell_notional = min(sell_notional, max(0.0, effective_sell_quantity * ask_price))

        toxic = state.toxic_side(
            self.config.toxic_flow_ratio,
            self.config.toxic_flow_min_notional,
            now_mono=mono,
        )
        if toxic == "BUY":
            buy_notional = 0.0
        elif toxic == "SELL":
            sell_notional = 0.0

        bid_qty = quantity_for_notional(buy_notional, bid_price)
        ask_qty = min(
            effective_sell_quantity,
            quantity_for_notional(sell_notional, ask_price),
        )
        bid = Quote("BUY", round_price(bid_price), bid_qty) if bid_qty > 0 else None
        ask = Quote("SELL", round_price(ask_price), ask_qty) if ask_qty > 0 else None
        return StrategyDecision(bid, ask, catchup=catchup, pace_mode=pace_mode)
