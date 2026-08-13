from __future__ import annotations

from loaf_bot.cli import main
from loaf_bot.config import BotConfig, ConfigError
from loaf_bot.storage import Store


def test_live_config_loads_only_explicit_safe_round_one_values(monkeypatch, tmp_path):
    monkeypatch.setenv("LOAF_API_KEY", "b" * 64)
    monkeypatch.setenv("LOAF_USER_ID", "42")
    monkeypatch.setenv("LOAF_SESSION_ID", "manual-session")
    monkeypatch.setenv("LOAF_TARGET_TOKEN", "terafab")
    monkeypatch.setenv("LOAF_API_BASE_URL", "https://api.loafmarkets.com/api")
    monkeypatch.setenv("LOAF_DB_PATH", str(tmp_path / "state.sqlite3"))
    config = BotConfig.load()
    assert config.session_id == "manual-session"
    assert config.loss_fraction == 0.25
    assert config.max_inventory_usdl == 60_000


def test_live_config_rejects_non_production_host(monkeypatch):
    monkeypatch.setenv("LOAF_API_KEY", "b" * 64)
    monkeypatch.setenv("LOAF_USER_ID", "42")
    monkeypatch.setenv("LOAF_SESSION_ID", "manual-session")
    monkeypatch.setenv("LOAF_TARGET_TOKEN", "terafab")
    monkeypatch.setenv("LOAF_API_BASE_URL", "http://localhost:8005/api")
    try:
        BotConfig.load()
    except ConfigError as exc:
        assert "only accepts" in str(exc)
    else:
        raise AssertionError("unsafe live host was accepted")


def test_status_and_unlock_commands_read_local_database(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "state.sqlite3"
    monkeypatch.setenv("LOAF_DB_PATH", str(db_path))
    store = Store(db_path)
    store.open_session("locked", 100_000, 0, 0.25)
    store.update_metrics(
        "locked",
        equity=75_000,
        inventory=30_000,
        session_volume=5_000_000,
        podium_target=100_000_000,
        catchup=True,
    )
    store.lock_session("locked", "test", 75_000)
    store.close()

    assert main(["status"]) == 0
    status = capsys.readouterr().out
    assert "inventory_usdl: 30000.00" in status
    assert "catchup: True" in status
    assert main(["unlock"]) == 0
    assert "archived" in capsys.readouterr().out
