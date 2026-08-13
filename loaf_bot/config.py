from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(ValueError):
    """Configuration is missing or unsafe for live trading."""


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _resolve_path(raw: str, root: Path) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else root / path


@dataclass(frozen=True, slots=True)
class BotConfig:
    api_key: str
    user_id: int
    base_url: str
    target_token: str
    db_path: Path
    log_dir: Path
    session_id: str
    target_inventory_usdl: float = 30_000.0
    lower_inventory_usdl: float = 24_000.0
    upper_inventory_usdl: float = 36_000.0
    max_inventory_usdl: float = 60_000.0
    base_quote_usdl: float = 10_000.0
    catchup_quote_usdl: float = 15_000.0
    loss_fraction: float = 0.25
    stale_book_seconds: float = 2.0
    volatility_window_seconds: float = 10.0
    volatility_limit_bps: float = 50.0
    mark_divergence_limit_bps: float = 10.0
    toxic_flow_ratio: float = 0.85
    toxic_flow_min_notional: float = 2_500.0
    pause_seconds: float = 5.0
    loop_interval_seconds: float = 0.5
    graceful_flatten_seconds: float = 30.0

    @classmethod
    def load(cls, *, require_session: bool = True) -> BotConfig:
        root = _root()
        load_dotenv(root / ".env")
        api_key = os.getenv("LOAF_API_KEY", "").strip()
        raw_user_id = os.getenv("LOAF_USER_ID", "").strip()
        session_id = os.getenv("LOAF_SESSION_ID", "").strip()
        if not api_key:
            raise ConfigError("LOAF_API_KEY is missing. Put a newly rotated key in .env.")
        if len(api_key) != 64 or any(c not in "0123456789abcdefABCDEF" for c in api_key):
            raise ConfigError("LOAF_API_KEY must be a 64-character hexadecimal key.")
        if not raw_user_id.isdigit() or int(raw_user_id) <= 0:
            raise ConfigError("LOAF_USER_ID must be the positive numeric id shown in the Loaf app.")
        if require_session and not session_id:
            raise ConfigError("LOAF_SESSION_ID is missing. Start live trading with run.ps1.")
        target = os.getenv("LOAF_TARGET_TOKEN", "terafab").strip().lower()
        if target != "terafab":
            raise ConfigError("Round 1 bot is locked to LOAF_TARGET_TOKEN=terafab.")
        base_url = os.getenv("LOAF_API_BASE_URL", "https://api.loafmarkets.com/api").rstrip("/")
        if base_url != "https://api.loafmarkets.com/api":
            raise ConfigError("Live mode only accepts https://api.loafmarkets.com/api.")
        return cls(
            api_key=api_key,
            user_id=int(raw_user_id),
            base_url=base_url,
            target_token=target,
            db_path=_resolve_path(os.getenv("LOAF_DB_PATH", ".state/loaf_bot.sqlite3"), root),
            log_dir=_resolve_path(os.getenv("LOAF_LOG_DIR", "logs"), root),
            session_id=session_id,
        )


@dataclass(frozen=True, slots=True)
class LocalConfig:
    db_path: Path

    @classmethod
    def load(cls) -> LocalConfig:
        root = _root()
        load_dotenv(root / ".env")
        return cls(_resolve_path(os.getenv("LOAF_DB_PATH", ".state/loaf_bot.sqlite3"), root))
