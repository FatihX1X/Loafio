from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


def value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def round_price(price: float) -> float:
    return round(float(price) + 1e-12, 2)


def quantity_for_notional(notional: float, price: float) -> float:
    if notional < 10 or price <= 0:
        return 0.0
    quantity = math.floor((notional / price) * 10 + 1e-9) / 10
    return quantity if quantity * price >= 10 else 0.0


@dataclass(slots=True)
class OrderState:
    order_id: int
    side: str
    price: float
    quantity: float
    remaining: float
    status: str = "OPEN"

    @property
    def active(self) -> bool:
        return self.status in {"OPEN", "PARTIALLY_FILLED", "SUBMITTING", "CANCEL_REQUESTED"}


@dataclass(slots=True)
class Quote:
    side: str
    price: float
    quantity: float

    @property
    def notional(self) -> float:
        return self.price * self.quantity


@dataclass(slots=True)
class LiveState:
    cash: float = 0.0
    frozen: float = 0.0
    portfolio_value: float = 0.0
    lifetime_volume: float = 0.0
    round_volume: float | None = None
    available_quantity: float = 0.0
    total_quantity: float = 0.0
    market_price: float = 0.0
    best_bid: float | None = None
    best_ask: float | None = None
    book_updated_mono: float = 0.0
    ws_connected: bool = False
    fatal_ws_error: str | None = None
    price_samples: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=512))
    trades: deque[tuple[float, str, float]] = field(default_factory=lambda: deque(maxlen=2048))
    rank3_samples: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=4096))
    round_ends_at: float = 0.0
    bot_orders: dict[str, OrderState] = field(default_factory=dict)
    pause_until_mono: float = 0.0

    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    def inventory_notional(self) -> float:
        reference = self.market_price or self.mid() or 0.0
        return self.total_quantity * reference

    def estimated_equity(self) -> float:
        reference = self.market_price or self.mid() or 0.0
        if reference > 0:
            return self.cash + self.frozen + self.total_quantity * reference
        return self.portfolio_value

    def record_price(self, price: float, now_mono: float | None = None) -> None:
        now = time.monotonic() if now_mono is None else now_mono
        if price > 0:
            self.price_samples.append((now, price))

    def recent_volatility_bps(self, window_seconds: float, now_mono: float | None = None) -> float:
        now = time.monotonic() if now_mono is None else now_mono
        prices = [p for ts, p in self.price_samples if now - ts <= window_seconds]
        if len(prices) < 2:
            return 0.0
        center = (max(prices) + min(prices)) / 2
        return 0.0 if center <= 0 else (max(prices) - min(prices)) / center * 10_000

    def toxic_side(
        self,
        ratio_limit: float,
        min_notional: float,
        window_seconds: float = 10.0,
        now_mono: float | None = None,
    ) -> str | None:
        now = time.monotonic() if now_mono is None else now_mono
        recent = [
            (side, notional)
            for ts, side, notional in self.trades
            if now - ts <= window_seconds
        ]
        total = sum(n for _, n in recent)
        if total < min_notional:
            return None
        buy = sum(n for side, n in recent if side == "BUY")
        if buy / total >= ratio_limit:
            return "SELL"
        if (total - buy) / total >= ratio_limit:
            return "BUY"
        return None
