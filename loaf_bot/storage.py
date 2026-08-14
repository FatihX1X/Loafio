from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    start_equity: float
    start_volume: float
    loss_floor: float
    status: str
    last_equity: float


@dataclass(frozen=True, slots=True)
class SessionMetrics:
    equity: float
    inventory: float
    session_volume: float
    podium_target: float
    catchup: bool
    updated_at: float


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock, self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    start_equity REAL NOT NULL,
                    start_volume REAL NOT NULL,
                    loss_floor REAL NOT NULL,
                    status TEXT NOT NULL,
                    last_equity REAL NOT NULL,
                    ended_at REAL,
                    lock_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS nonces (
                    nonce TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    state TEXT NOT NULL,
                    order_id INTEGER,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );
                CREATE TABLE IF NOT EXISTS orders (
                    order_id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    remaining REAL NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    created_at REAL NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metrics (
                    session_id TEXT PRIMARY KEY,
                    updated_at REAL NOT NULL,
                    equity REAL NOT NULL,
                    inventory REAL NOT NULL,
                    session_volume REAL NOT NULL,
                    podium_target REAL NOT NULL,
                    catchup INTEGER NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );
                CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, created_at);
                """
            )

    def open_session(
        self,
        session_id: str,
        equity: float,
        lifetime_volume: float,
        _legacy_loss_fraction: float | None = None,
    ) -> SessionRecord:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                floor = 0.0
                self._db.execute(
                    """INSERT INTO sessions
                    (session_id, created_at, start_equity, start_volume,
                     loss_floor, status, last_equity)
                    VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?)""",
                    (session_id, time.time(), equity, lifetime_volume, floor, equity),
                )
                self.event(session_id, "session_started", {"equity": equity, "floor": floor})
                return SessionRecord(session_id, equity, lifetime_volume, floor, "ACTIVE", equity)
            # Loss locking was removed. Re-enable sessions created by an older
            # version and retain the column only for database compatibility.
            self._db.execute(
                """UPDATE sessions
                SET loss_floor=0, status='ACTIVE', ended_at=NULL, lock_reason=NULL
                WHERE session_id=? AND status IN ('ACTIVE', 'RISK_LOCKED')""",
                (session_id,),
            )
            refreshed = self._db.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            return self._record(refreshed)

    @staticmethod
    def _record(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            row["session_id"],
            row["start_equity"],
            row["start_volume"],
            row["loss_floor"],
            row["status"],
            row["last_equity"],
        )

    def update_equity(self, session_id: str, equity: float) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE sessions SET last_equity = ? WHERE session_id = ?", (equity, session_id)
            )

    def update_metrics(
        self,
        session_id: str,
        *,
        equity: float,
        inventory: float,
        session_volume: float,
        podium_target: float,
        catchup: bool,
    ) -> None:
        now = time.time()
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO metrics
                (session_id, updated_at, equity, inventory,
                 session_volume, podium_target, catchup)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                  updated_at=excluded.updated_at,
                  equity=excluded.equity,
                  inventory=excluded.inventory,
                  session_volume=excluded.session_volume,
                  podium_target=excluded.podium_target,
                  catchup=excluded.catchup""",
                (
                    session_id,
                    now,
                    equity,
                    inventory,
                    session_volume,
                    podium_target,
                    int(catchup),
                ),
            )
            self._db.execute(
                "UPDATE sessions SET last_equity=? WHERE session_id=?",
                (equity, session_id),
            )

    def metrics(self, session_id: str) -> SessionMetrics | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM metrics WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            return SessionMetrics(
                equity=float(row["equity"]),
                inventory=float(row["inventory"]),
                session_volume=float(row["session_volume"]),
                podium_target=float(row["podium_target"]),
                catchup=bool(row["catchup"]),
                updated_at=float(row["updated_at"]),
            )

    def lock_session(self, session_id: str, reason: str, equity: float) -> None:
        with self._lock, self._db:
            self._db.execute(
                """UPDATE sessions
                SET status='RISK_LOCKED', ended_at=?, lock_reason=?, last_equity=?
                WHERE session_id=?""",
                (time.time(), reason, equity, session_id),
            )
            self.event(session_id, "risk_locked", {"reason": reason, "equity": equity})

    def finish_session(self, session_id: str, status: str = "STOPPED") -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE sessions SET status=?, ended_at=? WHERE session_id=? AND status='ACTIVE'",
                (status, time.time(), session_id),
            )

    def latest_session(self) -> SessionRecord | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM sessions ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            return None if row is None else self._record(row)

    def archive_locked(self) -> bool:
        with self._lock, self._db:
            row = self._db.execute(
                """SELECT session_id FROM sessions
                WHERE status='RISK_LOCKED' ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()
            if row is None:
                return False
            self._db.execute(
                "UPDATE sessions SET status='ARCHIVED' WHERE session_id=?", (row["session_id"],)
            )
            return True

    def mint_nonce(self, session_id: str, nonce: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO nonces (nonce, session_id, created_at, state)
                VALUES (?, ?, ?, 'MINTED')""",
                (nonce, session_id, time.time()),
            )

    def update_nonce(self, nonce: str, state: str, order_id: int | None = None) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE nonces SET state=?, order_id=? WHERE nonce=?", (state, order_id, nonce)
            )

    def save_order(
        self,
        session_id: str,
        order_id: int,
        side: str,
        price: float,
        quantity: float,
        remaining: float,
        status: str,
    ) -> None:
        now = time.time()
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO orders
                (order_id, session_id, side, price, quantity,
                 remaining, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                  remaining=excluded.remaining,
                  status=excluded.status,
                  updated_at=excluded.updated_at""",
                (order_id, session_id, side, price, quantity, remaining, status, now, now),
            )

    def update_order(self, order_id: int, remaining: float, status: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE orders SET remaining=?, status=?, updated_at=? WHERE order_id=?",
                (remaining, status, time.time(), order_id),
            )

    def known_active_order_ids(self, session_id: str) -> set[int]:
        with self._lock:
            rows = self._db.execute(
                """SELECT order_id FROM orders WHERE session_id=?
                AND status IN ('OPEN','PARTIALLY_FILLED','SUBMITTING','CANCEL_REQUESTED')""",
                (session_id,),
            ).fetchall()
            return {int(row[0]) for row in rows}

    def active_orders(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """SELECT order_id, side, price, quantity, remaining, status
                FROM orders WHERE session_id=?
                AND status IN ('OPEN','PARTIALLY_FILLED','SUBMITTING','CANCEL_REQUESTED')""",
                (session_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def event(self, session_id: str | None, kind: str, payload: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO events (session_id, created_at, kind, payload) VALUES (?, ?, ?, ?)",
                (
                    session_id,
                    time.time(),
                    kind,
                    json.dumps(payload, default=str, separators=(",", ":")),
                ),
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()
