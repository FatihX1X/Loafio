from __future__ import annotations

import sqlite3
import time
from dataclasses import replace

import loaf
import pytest
from conftest import FakeClient

from loaf_bot.domain import OrderState, Quote
from loaf_bot.engine import MakerBot, RestartRequired, TradingPaused
from loaf_bot.storage import Store


def make_bot(config, client=None):
    fake = client or FakeClient()
    store = Store(config.db_path)
    return MakerBot(config, client=fake, store=store), fake


def test_preflight_seeds_session_and_market(config):
    bot, _client = make_bot(config)
    try:
        bot.preflight()
        assert bot.session is not None
        assert bot.session.start_equity == 100_000
        assert bot.session.loss_floor == 0
        assert bot.state.best_bid == 99.99
        assert bot.state.best_ask == 100.0
        assert bot.state.rank3_samples[-1][1] == 8_000
    finally:
        bot.close()


def test_preflight_cancels_unknown_active_order_and_requests_restart(config):
    client = FakeClient(
        active_orders=[
            {"orderId": 77, "tokenName": "terafab", "side": "BUY", "price": 99.99}
        ]
    )
    bot, _client = make_bot(config, client)
    try:
        with pytest.raises(RestartRequired, match="unknown active orders were cleaned"):
            bot.preflight()
        assert client.orders.cancelled == [77]
        client.active_orders = []
        bot.preflight()
        assert bot.session is not None
    finally:
        bot.close()


def test_preflight_halt_requests_temporary_watchdog_retry(config):
    bot, _client = make_bot(config, FakeClient(halted=True))
    try:
        with pytest.raises(TradingPaused, match="trading is halted"):
            bot.preflight()
    finally:
        bot.close()


def test_self_trade_guard_blocks_crossing_order(config):
    bot, client = make_bot(config)
    try:
        bot.preflight()
        bot.state.bot_orders["SELL"] = OrderState(50, "SELL", 99.99, 10, 10)
        bot.state.ws_connected = True
        bot.state.book_updated_mono = time.monotonic()
        bot._place(Quote("BUY", 99.99, 10))
        assert client.orders.create_count == 0
    finally:
        bot.close()


def test_place_records_unique_nonce_and_order(config):
    bot, client = make_bot(config)
    try:
        bot.preflight()
        bot.state.ws_connected = True
        bot.state.book_updated_mono = time.monotonic()
        bot._place(Quote("BUY", 99.99, 10))
        assert client.orders.nonce_count == 1
        assert bot.state.bot_orders["BUY"].order_id == 1001
        with sqlite3.connect(config.db_path) as db:
            assert db.execute("SELECT COUNT(*) FROM nonces").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    finally:
        bot.close()


def test_low_equity_does_not_lock_or_flatten(config):
    client = FakeClient(equity=100_000, quantity=0)
    bot, _client = make_bot(config, client)
    try:
        bot.preflight()
        bot.state.cash = 74_999
        bot.state.frozen = 0
        bot.state.total_quantity = 0
        client.component.update(
            {"cash": 74_999, "portfolioValue": 74_999, "positions": []}
        )
        bot._risk_check()
        assert client.orders.cancel_all_count == 0
        assert bot.store.latest_session().status == "ACTIVE"
    finally:
        bot.close()


def test_cancel_replace_waits_for_terminal_cancel_event(config):
    bot, client = make_bot(config)
    try:
        bot.preflight()
        old = OrderState(50, "BUY", 99.98, 100, 100)
        bot.state.bot_orders["BUY"] = old
        bot.store.save_order(config.session_id, 50, "BUY", 99.98, 100, 100, "OPEN")
        bot._ensure_quote("BUY", Quote("BUY", 99.99, 100))
        assert client.orders.cancelled == [50]
        assert client.orders.create_count == 0
    finally:
        bot.close()


def test_terminal_cancel_error_clears_stale_filled_order(config):
    bot, client = make_bot(config)
    try:
        bot.preflight()
        order = OrderState(50, "SELL", 100.0, 100, 100)
        bot.state.bot_orders["SELL"] = order
        bot.store.save_order(config.session_id, 50, "SELL", 100.0, 100, 100, "OPEN")
        client.orders.cancel_error = loaf.LoafValidationError(
            "Cannot cancel order with status: FILLED", status_code=400
        )
        bot._cancel_side("SELL")
        assert "SELL" not in bot.state.bot_orders
        assert bot.store.active_orders(config.session_id) == []
        assert client.orders.cancelled == [50]
    finally:
        bot.close()


def test_repeated_self_trade_guard_requests_watchdog_restart(config):
    config = replace(config, loop_restart_threshold=3, loop_restart_window_seconds=10)
    bot, client = make_bot(config)
    try:
        bot.preflight()
        bot.state.bot_orders["SELL"] = OrderState(50, "SELL", 99.99, 10, 10)
        bot.state.ws_connected = True
        bot.state.book_updated_mono = time.monotonic()
        quote = Quote("BUY", 99.99, 10)
        bot._place(quote)
        bot._place(quote)
        with pytest.raises(RestartRequired, match="repeated order-state loop"):
            bot._place(quote)
        assert client.orders.create_count == 0
    finally:
        bot.close()


def test_repeated_nonterminal_cancel_failure_requests_watchdog_restart(config):
    config = replace(config, loop_restart_threshold=3, loop_restart_window_seconds=10)
    bot, client = make_bot(config)
    try:
        bot.preflight()
        order = OrderState(51, "BUY", 99.99, 100, 100)
        bot.state.bot_orders["BUY"] = order
        bot.store.save_order(config.session_id, 51, "BUY", 99.99, 100, 100, "OPEN")
        client.orders.cancel_error = loaf.LoafValidationError(
            "temporary cancel failure", status_code=400
        )
        bot._cancel_side("BUY")
        bot._cancel_side("BUY")
        with pytest.raises(RestartRequired, match="cancel_failed:51"):
            bot._cancel_side("BUY")
        assert client.orders.cancelled == [51, 51, 51]
    finally:
        bot.close()


def test_trading_halt_pauses_cancel_without_fault_restart(config):
    bot, client = make_bot(config)
    try:
        bot.preflight()
        order = OrderState(52, "BUY", 99.99, 100, 100)
        bot.state.bot_orders["BUY"] = order
        bot.store.save_order(config.session_id, 52, "BUY", 99.99, 100, 100, "OPEN")
        client.orders.cancel_error = loaf.TradingHaltedError(
            "Trading is currently halted", status_code=403
        )
        before = time.monotonic()
        bot._cancel_side("BUY")
        assert bot._halt_until_mono >= before + config.halt_retry_seconds - 0.1
        assert bot._loop_faults == {}
        bot._cancel_side("BUY")
        assert client.orders.cancelled == [52]
    finally:
        bot.close()


def test_graceful_exit_order_is_persisted_for_restart_reconciliation(config):
    config = replace(config, graceful_flatten_seconds=0)
    bot, client = make_bot(config)
    try:
        bot.preflight()
        bot.graceful_flatten()
        rows = bot.store.active_orders(config.session_id)
        assert len(rows) == 1
        assert rows[0]["order_id"] == 1001
        assert rows[0]["side"] == "SELL"
        assert rows[0]["status"] == "CANCEL_REQUESTED"
        assert client.orders.market_sells == [300.0]
    finally:
        bot.close()


def test_oversized_resting_order_is_cancelled_before_cap_can_be_exceeded(config):
    bot, client = make_bot(config)
    try:
        bot.preflight()
        old = OrderState(51, "BUY", 99.99, 150, 150)
        bot.state.bot_orders["BUY"] = old
        bot.store.save_order(config.session_id, 51, "BUY", 99.99, 150, 150, "OPEN")
        bot._ensure_quote("BUY", Quote("BUY", 99.99, 50))
        assert client.orders.cancelled == [51]
        assert client.orders.create_count == 0
        assert bot.state.bot_orders["BUY"].status == "CANCEL_REQUESTED"
    finally:
        bot.close()


def test_repeated_full_picture_trade_batches_are_deduplicated(config):
    bot, _client = make_bot(config)
    try:
        batch = {
            "trades": [
                {
                    "tradeId": 1,
                    "aggressorSide": "BUY",
                    "price": 100,
                    "quantity": 10,
                }
            ]
        }
        bot._on_trades(batch)
        bot._on_trades(batch)
        assert len(bot.state.trades) == 1
    finally:
        bot.close()


def test_partial_fill_keeps_order_and_updates_remaining(config):
    bot, _client = make_bot(config)
    try:
        order = OrderState(50, "BUY", 99.99, 100, 100)
        bot.state.bot_orders["BUY"] = order
        bot.store.open_session(config.session_id, 100_000, 0, 0.25)
        bot.store.save_order(config.session_id, 50, "BUY", 99.99, 100, 100, "OPEN")
        bot._on_order_event(
            {"orderId": 50, "status": "PARTIALLY_FILLED", "quantityLeft": 40}
        )
        assert bot.state.bot_orders["BUY"].remaining == 40
        assert bot.state.bot_orders["BUY"].active
    finally:
        bot.close()


def test_rate_limit_pauses_new_orders_without_retry(config):
    client = FakeClient()
    client.orders.create_error = loaf.LoafRateLimitError(
        "slow down", status_code=429, retry_after=7
    )
    bot, _client = make_bot(config, client)
    try:
        bot.preflight()
        bot.state.ws_connected = True
        bot.state.book_updated_mono = time.monotonic()
        before = time.monotonic()
        bot._place(Quote("BUY", 99.99, 10))
        assert client.orders.create_count == 1
        assert bot._placing_blocked_until >= before + 6.9
        assert "BUY" not in bot.state.bot_orders
    finally:
        bot.close()


def test_ambiguous_503_adopts_one_matching_active_order(config):
    client = FakeClient()
    client.orders.create_error = loaf.LoafServiceUnavailableError(
        "engine timeout", status_code=503
    )
    client.active_orders = [
        {
            "orderId": 9001,
            "tokenName": "terafab",
            "side": "BUY",
            "price": 99.99,
            "quantityLeft": 10,
        }
    ]
    bot, _client = make_bot(config, client)
    try:
        # Avoid preflight's intentional unknown-order rejection; the order appears
        # only after the simulated ambiguous POST.
        client.active_orders = []
        bot.preflight()
        client.active_orders = [
            {
                "orderId": 9001,
                "tokenName": "terafab",
                "side": "BUY",
                "price": 99.99,
                "quantityLeft": 10,
            }
        ]
        bot.state.ws_connected = True
        bot.state.book_updated_mono = time.monotonic()
        bot._place(Quote("BUY", 99.99, 10))
        assert bot.state.bot_orders["BUY"].order_id == 9001
        assert client.orders.create_count == 1
    finally:
        bot.close()


def test_restart_adopts_known_active_order(config):
    client = FakeClient(
        active_orders=[
            {
                "orderId": 77,
                "side": "SELL",
                "price": 100.0,
                "quantity": 25,
                "quantityLeft": 20,
                "status": "PARTIALLY_FILLED",
            }
        ]
    )
    store = Store(config.db_path)
    store.open_session(config.session_id, 100_000, 1_000, 0.25)
    store.save_order(config.session_id, 77, "SELL", 100.0, 25, 25, "OPEN")
    bot = MakerBot(config, client=client, store=store)
    try:
        bot.preflight()
        assert bot.state.bot_orders["SELL"].order_id == 77
        assert bot.state.bot_orders["SELL"].remaining == 20
    finally:
        bot.close()


def test_out_of_order_cash_delta_is_rest_confirmed_before_risk_lock(config):
    bot, _client = make_bot(config)
    try:
        bot.preflight()
        bot.state.cash = 10_000
        bot.state.frozen = 0
        bot.state.total_quantity = 0
        bot._risk_check()
        assert bot.store.latest_session().status == "ACTIVE"
        assert bot.state.portfolio_value == 100_000
    finally:
        bot.close()


def test_private_websocket_requires_auth_and_exact_portfolio_channel(config):
    bot, _client = make_bot(config)
    try:
        bot.store.open_session(config.session_id, 100_000, 0, 0.25)
        bot._on_auth_result({"type": "auth_result", "success": True})
        bot._on_subscription_confirmed(
            {"type": "subscription_confirmed", "channels": ["orderbook:terafab"]}
        )
        assert bot._ws_auth_ok.is_set()
        assert not bot._portfolio_subscription_ok.is_set()
        bot._on_subscription_confirmed(
            {
                "type": "subscription_confirmed",
                "channels": [f"portfolio:{config.user_id}"],
            }
        )
        assert bot._portfolio_subscription_ok.is_set()
    finally:
        bot.close()


def test_rejected_private_websocket_auth_fails_closed(config):
    bot, _client = make_bot(config)
    try:
        bot.store.open_session(config.session_id, 100_000, 0, 0.25)
        bot._on_auth_result({"type": "auth_result", "success": False})
        assert not bot._ws_auth_ok.is_set()
        assert "authentication rejected" in bot.state.fatal_ws_error
    finally:
        bot.close()


def test_leaderboard_tracks_own_round_volume_by_user_id(config):
    bot, _client = make_bot(config)
    try:
        bot.store.open_session(config.session_id, 100_000, 0, 0.25)
        bot._on_leaderboard(
            {
                "entries": [
                    {"rank": 3, "userId": 999, "volume": 1_300_000_000},
                    {"rank": 200, "userId": config.user_id, "volume": 16_000_000},
                ]
            }
        )
        assert bot.state.round_volume == 16_000_000
        assert bot.state.rank3_samples[-1][1] == 1_300_000_000
    finally:
        bot.close()
