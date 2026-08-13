from __future__ import annotations

import argparse
import sys
import traceback

from .config import BotConfig, ConfigError, LocalConfig
from .engine import (
    MakerBot,
    PreflightError,
    RiskLockTriggered,
    configure_logging,
    flatten_account,
)
from .storage import Store

EXIT_RISK_LOCKED = 25
EXIT_CONFIG = 64
EXIT_PREFLIGHT = 65
EXIT_RESTART = 70


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loaf-maker")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("live", help="start autonomous live trading immediately")
    sub.add_parser("status", help="show the latest local session")
    sub.add_parser("flatten", help="cancel all orders and market-sell available terafab")
    sub.add_parser("unlock", help="archive the latest risk-locked session")
    return parser


def _status() -> int:
    config = LocalConfig.load()
    if not config.db_path.exists():
        print("No local session database exists yet.")
        return 0
    store = Store(config.db_path)
    try:
        session = store.latest_session()
        if session is None:
            print("No sessions recorded.")
            return 0
        drawdown = session.start_equity - session.last_equity
        print(f"session_id: {session.session_id}")
        print(f"status: {session.status}")
        print(f"start_equity: {session.start_equity:.2f}")
        print(f"last_equity: {session.last_equity:.2f}")
        print(f"drawdown: {drawdown:.2f}")
        print(f"loss_floor: {session.loss_floor:.2f}")
        metrics = store.metrics(session.session_id)
        if metrics is not None:
            print(f"inventory_usdl: {metrics.inventory:.2f}")
            print(f"session_volume: {metrics.session_volume:.2f}")
            print(f"podium_target: {metrics.podium_target:.2f}")
            print(f"catchup: {metrics.catchup}")
            print(f"metrics_updated_at: {metrics.updated_at:.0f}")
        return 0
    finally:
        store.close()


def _unlock() -> int:
    config = LocalConfig.load()
    store = Store(config.db_path)
    try:
        if store.archive_locked():
            print("Latest risk lock archived. A new manual run.ps1 starts a new loss budget.")
        else:
            print("No risk-locked session found.")
        return 0
    finally:
        store.close()


def _live() -> int:
    try:
        config = BotConfig.load(require_session=True)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    configure_logging(config.log_dir)
    bot = MakerBot(config)
    graceful = False
    try:
        bot.run()
        return 0
    except KeyboardInterrupt:
        graceful = True
        bot.request_stop()
        return 0
    except RiskLockTriggered as exc:
        print(f"Risk lock: {exc}", file=sys.stderr)
        return EXIT_RISK_LOCKED
    except PreflightError as exc:
        print(f"Preflight failed: {exc}", file=sys.stderr)
        return EXIT_PREFLIGHT
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return EXIT_RESTART
    finally:
        bot.close(graceful=graceful)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "status":
        return _status()
    if args.command == "unlock":
        return _unlock()
    if args.command == "flatten":
        try:
            flatten_account(BotConfig.load(require_session=False))
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"Flatten failed: {exc}", file=sys.stderr)
            return EXIT_CONFIG
    return _live()


if __name__ == "__main__":
    raise SystemExit(main())
