from __future__ import annotations

from loaf_bot.storage import Store


def test_watchdog_restart_preserves_original_loss_floor(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    try:
        first = store.open_session("same", 100_000, 1_000, 0.25)
        restarted = store.open_session("same", 80_000, 2_000, 0.25)
        assert first.loss_floor == 75_000
        assert restarted.start_equity == 100_000
        assert restarted.loss_floor == 75_000

        manual_new = store.open_session("new", 80_000, 2_000, 0.25)
        assert manual_new.loss_floor == 60_000
    finally:
        store.close()


def test_risk_lock_and_unlock_are_persistent(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    try:
        store.open_session("same", 100_000, 0, 0.25)
        store.lock_session("same", "test", 74_999)
        restarted = store.open_session("same", 90_000, 0, 0.25)
        assert restarted.status == "RISK_LOCKED"
        assert store.archive_locked() is True
        assert store.latest_session().status == "ARCHIVED"
    finally:
        store.close()


def test_nonce_primary_key_rejects_reuse(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    try:
        store.open_session("same", 100_000, 0, 0.25)
        store.mint_nonce("same", "a" * 32)
        try:
            store.mint_nonce("same", "a" * 32)
        except Exception as exc:  # sqlite intentionally surfaced as a hard failure
            assert "UNIQUE" in str(exc)
        else:
            raise AssertionError("duplicate nonce was accepted")
    finally:
        store.close()


def test_metrics_round_trip_for_status(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    try:
        store.open_session("same", 100_000, 1_000, 0.25)
        store.update_metrics(
            "same",
            equity=99_500,
            inventory=30_000,
            session_volume=5_000_000,
            podium_target=200_000_000,
            catchup=True,
        )
        metrics = store.metrics("same")
        assert metrics is not None
        assert metrics.inventory == 30_000
        assert metrics.session_volume == 5_000_000
        assert metrics.catchup is True
        assert store.latest_session().last_equity == 99_500
    finally:
        store.close()
