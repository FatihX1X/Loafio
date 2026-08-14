from __future__ import annotations

import time

import pytest

from loaf_bot.domain import LiveState, OrderState, quantity_for_notional, round_price
from loaf_bot.strategy import MakerStrategy


def ready_state(now: float) -> LiveState:
    state = LiveState(
        cash=70_000,
        portfolio_value=100_000,
        available_quantity=300,
        total_quantity=300,
        market_price=100.0,
        best_bid=99.99,
        best_ask=100.0,
        book_updated_mono=now,
        ws_connected=True,
        round_ends_at=time.time() + 86_400,
    )
    state.record_price(100.0, now)
    return state


def test_money_rounding_is_passive_and_one_decimal():
    assert round_price(100.004) == 100.0
    assert round_price(100.006) == 100.01
    assert quantity_for_notional(10_000, 149.0) == 67.1
    assert quantity_for_notional(9, 100) == 0


def test_neutral_inventory_quotes_both_sides(config):
    now = time.monotonic()
    state = ready_state(now)
    decision = MakerStrategy(config).decide(
        state,
        session_start_volume=state.lifetime_volume,
        now_mono=now,
        now_wall=time.time(),
    )
    assert decision.pause_reason is None
    assert decision.bid is not None and decision.bid.price == 99.99
    assert decision.ask is not None and decision.ask.price == 100.0
    assert decision.bid.notional == pytest.approx(14_998.5)
    assert decision.ask.notional == pytest.approx(15_000.0)
    assert decision.bid.price < decision.ask.price


def test_sprint_mode_uses_forty_thousand_usdl_quotes(config):
    now_mono = time.monotonic()
    now_wall = time.time()
    state = ready_state(now_mono)
    state.lifetime_volume = 0
    state.round_ends_at = now_wall + 3_600
    state.rank3_samples.extend([(now_wall - 60, 1_000), (now_wall, 2_000)])
    decision = MakerStrategy(config).decide(
        state,
        session_start_volume=0,
        now_mono=now_mono,
        now_wall=now_wall,
    )
    assert decision.catchup is True
    assert decision.pace_mode == "sprint"
    assert decision.bid is not None and decision.bid.notional == pytest.approx(39_996.0)
    # The sell remains bounded by actual inventory even in sprint mode.
    assert decision.ask is not None and decision.ask.notional == pytest.approx(30_000.0)


def test_pace_uses_restart_stable_lifetime_volume(config):
    now = time.time()
    state = LiveState(lifetime_volume=900_000, round_ends_at=now + 3_600)
    state.rank3_samples.append((now, 1_000_000))
    assert MakerStrategy(config).pace_mode(state, now) == "normal"


def test_catchup_mode_is_between_sprint_and_normal(config):
    now = time.time()
    state = LiveState(lifetime_volume=500_000, round_ends_at=now + 3_600)
    state.rank3_samples.append((now, 1_000_000))
    assert MakerStrategy(config).pace_mode(state, now) == "catchup"


def test_zero_round_volume_does_not_fall_back_to_lifetime(config):
    now = time.time()
    state = LiveState(lifetime_volume=9_000_000, round_volume=0, round_ends_at=now + 3_600)
    state.rank3_samples.append((now, 1_000_000))
    assert MakerStrategy(config).pace_mode(state, now) == "sprint"


def test_inventory_cap_disables_buy(config):
    now = time.monotonic()
    state = ready_state(now)
    state.total_quantity = 800
    state.available_quantity = 800
    state.cash = 40_000
    decision = MakerStrategy(config).decide(
        state, session_start_volume=0, now_mono=now, now_wall=time.time()
    )
    assert decision.bid is None
    assert decision.ask is not None


def test_no_inventory_builds_passively_without_sell(config):
    now = time.monotonic()
    state = ready_state(now)
    state.total_quantity = 0
    state.available_quantity = 0
    state.cash = 100_000
    decision = MakerStrategy(config).decide(
        state, session_start_volume=0, now_mono=now, now_wall=time.time()
    )
    assert decision.bid is not None
    assert decision.ask is None


def test_stale_book_fails_closed(config):
    now = time.monotonic()
    state = ready_state(now - 3)
    decision = MakerStrategy(config).decide(
        state, session_start_volume=0, now_mono=now, now_wall=time.time()
    )
    assert decision.bid is None and decision.ask is None
    assert decision.pause_reason == "stale order book"


def test_toxic_sell_flow_disables_buy(config):
    now = time.monotonic()
    state = ready_state(now)
    state.trades.extend([(now, "SELL", 3_000), (now, "SELL", 3_000), (now, "BUY", 100)])
    decision = MakerStrategy(config).decide(
        state, session_start_volume=0, now_mono=now, now_wall=time.time()
    )
    assert decision.bid is None
    assert decision.ask is not None


def test_volatility_filter_cancels_both_sides(config):
    now = time.monotonic()
    state = ready_state(now)
    state.record_price(100.0, now - 2)
    state.record_price(101.0, now)
    decision = MakerStrategy(config).decide(
        state, session_start_volume=0, now_mono=now, now_wall=time.time()
    )
    assert decision.bid is None and decision.ask is None
    assert decision.pause_reason.startswith("volatility")


def test_podium_projection_uses_recent_rank_three_rate(config):
    strategy = MakerStrategy(config)
    state = LiveState(round_ends_at=2_000.0)
    state.rank3_samples.extend([(900.0, 1_000.0), (1_000.0, 2_000.0)])
    target, rate = strategy.podium_target(state, now_wall=1_000.0)
    assert rate == 10.0
    assert target == pytest.approx(13_200.0)


def test_existing_sell_reservation_is_counted_as_sellable(config):
    now = time.monotonic()
    state = ready_state(now)
    state.available_quantity = 200
    state.bot_orders["SELL"] = OrderState(5, "SELL", 100.0, 100, 100)
    decision = MakerStrategy(config).decide(
        state, session_start_volume=0, now_mono=now, now_wall=time.time()
    )
    assert decision.ask is not None
    assert decision.ask.quantity == 150


def test_existing_buy_reservation_is_counted_as_available_cash(config):
    now = time.monotonic()
    state = ready_state(now)
    state.cash = 0
    state.bot_orders["BUY"] = OrderState(6, "BUY", 99.99, 100, 100)
    decision = MakerStrategy(config).decide(
        state, session_start_volume=0, now_mono=now, now_wall=time.time()
    )
    assert decision.bid is not None
