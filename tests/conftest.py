from __future__ import annotations

from types import SimpleNamespace

import pytest

from loaf_bot.config import BotConfig


@pytest.fixture
def config(tmp_path):
    return BotConfig(
        api_key="a" * 64,
        user_id=123,
        base_url="https://api.loafmarkets.com/api",
        target_token="terafab",
        db_path=tmp_path / "state.sqlite3",
        log_dir=tmp_path / "logs",
        session_id="session-one",
    )


class FakeOrders:
    def __init__(self) -> None:
        self.nonce_count = 0
        self.create_count = 0
        self.cancelled: list[int] = []
        self.cancel_all_count = 0
        self.market_sells: list[float] = []
        self.create_error = None
        self.cancel_error = None

    def nonce(self):
        self.nonce_count += 1
        return {"nonce": f"{self.nonce_count:032x}", "deadline": 9999999999}

    def create(self, _token, **kwargs):
        self.create_count += 1
        if self.create_error:
            raise self.create_error
        return {"success": True, "orderId": 1000 + self.create_count, **kwargs}

    def cancel(self, order_id):
        self.cancelled.append(order_id)
        if self.cancel_error:
            raise self.cancel_error
        return {"success": True, "orderId": order_id}

    def cancel_all(self):
        self.cancel_all_count += 1
        return {"requestedCount": 0, "cancelledOrderIds": [], "failedOrders": []}

    def market_sell(self, _token, quantity, **_kwargs):
        self.market_sells.append(quantity)
        return {"success": True, "orderId": 2000 + len(self.market_sells)}


class FakeClient:
    def __init__(
        self, *, equity=100_000.0, quantity=300.0, active_orders=None, halted=False
    ) -> None:
        self.orders = FakeOrders()
        self.closed = False
        position = {
            "tokenName": "terafab",
            "quantity": quantity,
            "totalQuantity": quantity,
            "marketPrice": 100.0,
        }
        self.component = {
            "cash": equity - quantity * 100,
            "frozen": 0.0,
            "portfolioValue": equity,
            "portfolioPnl": 0.0,
            "lifetimeVolume": 1_000.0,
            "positions": [position] if quantity else [],
        }
        self.portfolio = SimpleNamespace(component=lambda: self.component)
        self.competition = SimpleNamespace(
            info=lambda: {
                "makerFeeBps": 0,
                "takerFeeBps": 10,
                "featuredRound": {
                    "status": "ACTIVE",
                    "endsAt": 2_000_000_000,
                    "newAssetProperty": {"tokenName": "terafab"},
                },
            },
            queue_position=lambda: {"position": None, "finalPlacement": None},
        )
        self.market = SimpleNamespace(
            properties=lambda: {
                "properties": [
                    {
                        "tokenName": "terafab",
                        "status": "LIVE",
                        "marketPrice": 100.0,
                    }
                ]
            },
            property=lambda _token: {
                "property": {"tokenName": "terafab", "isHalted": halted},
                "orderBook": {
                    "bids": [{"price": 99.99, "quantity": 1000}],
                    "asks": [{"price": 100.00, "quantity": 1000}],
                },
            },
        )
        self.leaderboard = SimpleNamespace(
            get=lambda: {
                "entries": [
                    {"rank": 1, "volume": 10_000},
                    {"rank": 2, "volume": 9_000},
                    {"rank": 3, "volume": 8_000},
                    {"rank": 10, "userId": 123, "volume": 1_234},
                ]
            }
        )
        self.active_orders = list(active_orders or [])
        self.history = SimpleNamespace(active_orders=lambda: {"activeOrders": self.active_orders})

    def close(self):
        self.closed = True
