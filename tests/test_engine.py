from __future__ import annotations

import sqlite3
import time

import loaf
import pytest
from conftest import FakeClient

from loaf_bot.domain import OrderState, Quote
from loaf_bot.engine import MakerBot, PreflightError, RiskLockTriggered
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
        assert bot.session.loss_floor == 75_000
        assert bot.state.best_bid == 99.99
        assert bot.state.best_ask == 100.0
        assert bot.state.rank3_samples[-1][1] == 8_000
    finally:
        bot.close()


def test_preflight_rejects_unknown_active_order(config):
    client = FakeClient(active_orders=[{"orderId": 77, "side": "BUY", "price": 99.99}])
    bot, _client = make_bot(config, client)
    try:
        with pytest.raises(PreflightError, match="Unknown active order"):
            bot.preflight()
    finally:
        bot.close()


def test_self_trade_guard_blocks_crossing_order(config):
    bot, client = make_bot(config)
    try:
        bot.preflight()
        bot.state.bot_orders["SELL"] = OrderState(50, "SELL", 100.0, 10, 10)
        bot.state.ws_connected = True
        bot.state.book_updated_mono = time.monotonic()
        bot._place(Quote("BUY", 100.0, 10))
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


def test_risk_floor_locks_and_flattens_without_restart_reset(config):
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
        with pytest.raises(RiskLockTriggered):
            bot._risk_check()
        assert client.orders.cancel_all_count == 1
        assert bot.store.latest_session().status == "RISK_LOCKED"
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
