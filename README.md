# Loafio

[Türkçe](README_TR.md) | English

Loafio is an autonomous, inventory-bounded market-making bot for the League of
Loaf competition. It runs locally on Windows and quotes the active `terafab`
order book with maker-first execution, inventory skew, podium-pace sizing, and
session-level risk controls.

> [!CAUTION]
> `run.cmd` starts live competition trading without asking for confirmation.
> There is no guarantee of profit, podium placement, execution quality, or a
> maximum realised loss. Gaps, latency, disconnections, and insufficient
> liquidity can cause losses beyond the configured threshold.

## Strategy overview

- Keeps normal orders passive to avoid taker fees where possible.
- Targets 30,000 USDL of Terafab inventory with a hard 60,000 USDL cap.
- Skews buy and sell size according to current inventory.
- Preserves queue priority and replaces orders only when price, fill, or risk
  conditions require it.
- Tracks short- and medium-term leaderboard pace without deliberately crossing
  the spread.
- Blocks self-trades and refuses to run with unrecognised active orders.
- Uses taker orders only for emergency or manual flattening.
- Stores sessions, orders, fills, nonces, fees, equity snapshots, leaderboard
  snapshots, and watchdog restarts in SQLite.

This design is inventory-neutral rather than truly delta-neutral: Loaf does not
provide a separate hedge instrument for the Terafab exposure.

## Risk model

Every manual `run.cmd` invocation creates a new session and records its starting
equity. The session loss floor is 75% of that value, corresponding to a 25%
drawdown limit.

If the Python process crashes, the PowerShell watchdog restarts it with the same
session ID and the same loss floor. If the floor is reached, the bot stops
quoting, cancels its orders, attempts to sell the remaining Terafab position,
and locks the session against watchdog restarts.

The 25% value is a trigger, not a guaranteed stop price. Market gaps, stale
prices, unavailable liquidity, API failures, or connection loss can produce a
larger realised loss.

## Requirements

- Windows 10 or Windows 11
- PowerShell 5.1+
- Python 3.11+
- A Loaf account admitted to the active competition round
- A Loaf API key and numeric Loaf user ID

The official Loaf SDK dependency is pinned to commit
`e1157bcc2cba41fcde6e0f929cc58ad61bc4d442` for reproducible installs.

## Installation

Clone the repository and run:

```powershell
git clone https://github.com/FatihX1X/Loafio.git
cd Loafio
.\setup.cmd
```

`setup.cmd` creates `.venv`, installs the pinned runtime and development
dependencies, and creates a local `.env` from `.env.example` when needed.

## Configuration

Create or edit `.env` in the repository root:

```dotenv
LOAF_API_KEY=
LOAF_USER_ID=
LOAF_API_BASE_URL=https://api.loafmarkets.com/api
LOAF_TARGET_TOKEN=terafab
LOAF_DB_PATH=.state/loaf_bot.sqlite3
LOAF_LOG_DIR=logs
```

Create and rotate API keys in the
[Loaf API settings](https://beta.loafmarkets.com/api). The key must never be
committed, pasted into a command, or included in logs. `.env`, the database,
and logs are excluded by `.gitignore`.

The official documentation describes `LOAF_USER_ID` as the numeric user ID used
by the private `portfolio:{userId}` WebSocket channel. If the web interface does
not display it, first put the API key in `.env`, then run this read-only command:

```powershell
.\.venv\Scripts\python.exe -c "from dotenv import load_dotenv; load_dotenv(); from loaf import LoafClient; c=LoafClient(); print(c.get('/auth/profile')['userId']); c.close()"
```

Copy only the printed number to `LOAF_USER_ID`.

## Start live trading

Before starting, cancel any manually created open orders. The bot deliberately
refuses to take ownership of unknown orders.

```powershell
.\run.cmd
```

The bot performs account, competition, asset, fee, order, and private WebSocket
preflight checks. If all checks pass, live quoting begins immediately. Keep the
computer awake and connected to the internet.

Use `Ctrl+C` for a normal shutdown. The bot cancels open orders, attempts a
passive sell for up to 30 seconds, and then market-sells any remaining Terafab.

## Operations

```powershell
# Show the latest local session and risk state
.\.venv\Scripts\python.exe -m loaf_bot status

# Cancel all orders and market-sell the Terafab position
.\.venv\Scripts\python.exe -m loaf_bot flatten

# Archive the latest risk lock before a new manual session
.\.venv\Scripts\python.exe -m loaf_bot unlock
```

`flatten` is destructive: it liquidates the current Terafab position using a
market order after cancelling open orders.

Runtime data is written to:

- `.state/loaf_bot.sqlite3`
- `logs/loaf-maker.log`
- `logs/watchdog.log`

## Validation

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
```

The offline suite covers rounding, inventory skew, podium pace, partial fills,
nonce uniqueness, HTTP ambiguity, self-trade prevention, emergency flattening,
session persistence, risk locks, stale data, and WebSocket reconciliation.

## Security

- Never commit `.env`, `.state`, or `logs`.
- Revoke a key immediately if it is exposed in chat, a terminal capture, a log,
  or Git history.
- Use a dedicated competition account and the minimum permissions available.
- Review the competition rules and local legal requirements before running.

References:

- [Building a trading bot](https://docs.loafmarkets.com/en/guides/building-a-trading-bot/)
- [WebSocket API](https://docs.loafmarkets.com/en/api-reference/websocket/)
- [Orders API](https://docs.loafmarkets.com/en/api-reference/orders/)
- [Trading competition](https://docs.loafmarkets.com/en/trading-competition/)

## Disclaimer

This project is provided for educational and competition use. It is not
financial advice. You are responsible for credentials, account eligibility,
strategy behaviour, losses, and compliance with Loaf's rules.
