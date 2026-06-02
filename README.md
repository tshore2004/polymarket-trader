# Polymarket Trader

A multi-factor signal engine for [Polymarket](https://polymarket.com) that surfaces high-conviction trading opportunities by combining smart-money leaderboard analysis, fair value edges, volume momentum, and news sentiment.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## What It Does

The bot ranks open prediction markets on a 0–100 score built from four factors:

| Factor | Weight | Source |
|--------|--------|--------|
| Leaderboard conviction | 0–30 | Top-trader consensus with hedging filter |
| Fair value edge | 0–30 | External sportsbook odds vs Polymarket price |
| Volume / line movement | 0–20 | Volume spikes + price momentum |
| News momentum | 0–10 | Trending headlines matched to market questions |
| Urgency | 0–10 | Sooner resolution = higher score |

It also runs a FastAPI web dashboard (`server.py`) for browser-based monitoring.

## Run Modes

```
python main.py
```

Choose at startup:

1. **Daily Report** — Ranked picks, smart-money positions split by horizon (tonight vs long-dated)
2. **Interactive Browse** — Event library grouped by category; drill into picks per event
3. **Continuous** — Background scan loop with configurable interval

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in credentials
python main.py
```

### Required Environment Variables

```ini
POLY_PRIVATE_KEY=        # Polygon wallet private key
POLY_API_KEY=            # Polymarket CLOB API key
POLY_API_SECRET=
POLY_API_PASSPHRASE=
```

### Optional

```ini
ODDS_API_KEY=            # The Odds API — sportsbook fair-value comparisons (free: 500 req/month)
NEWS_API_KEY=            # NewsAPI.org — richer news coverage
NEWS_ENABLED=true        # Set false to skip news sentiment (saves ~8s per scan)
MAX_BET_SIZE=25          # USD cap per trade
MIN_BET_SIZE=5
LEADERBOARD_TOP_N=20     # How many top traders to track
COPY_MIN_POSITION_USD=100  # Minimum position size to count as conviction
DAILY_REPORT_TOP_N=12    # Picks shown in daily report
SHORT_TERM_HOURS=48      # Hours window for "tonight/tomorrow" bucket
```

## Architecture

```
main.py
  ├─ Daily Report     → SignalEngine.scan() → show_daily_report()
  ├─ Interactive Browse → event library → drill into picks
  └─ Continuous       → scan loop → prompt → execute
```

### Key Files

| File | Role |
|------|------|
| `config.py` | `Config` dataclass; loads from `.env` |
| `utils/models.py` | All data models (`Market`, `Signal`, `MarketConsensus`, …) |
| `utils/display.py` | Rich-based terminal UI |
| `core/api_client.py` | Read-only HTTP client for Polymarket public APIs |
| `core/leaderboard.py` | Hedging-aware leaderboard consensus |
| `core/fair_value.py` | External odds comparison + order book depth |
| `core/news_sentiment.py` | RSS/NewsAPI headline matching |
| `core/volume_tracker.py` | Volume spike and price movement detection |
| `core/signal.py` | `SignalEngine` — orchestrates all analyzers |
| `core/executor.py` | Bet sizing and order placement via `py-clob-client` |
| `server.py` | FastAPI web dashboard |

## Hedging Detection

Traders who spread positions across more than two outcomes in the same event (e.g. five World Cup teams) are flagged as diversifying, not showing conviction, and excluded from the signal. For remaining traders, position weight is scaled by `position_size / max_position_in_event`, so a $100 conviction bet alongside a $10 hedge contributes 10× more signal.

## Bet Sizing

`TradeExecutor` scales bet size linearly: `MIN_BET_SIZE` at score 0, `MAX_BET_SIZE` at score 100.

## Performance Notes

- RSS news is fetched in parallel with an 8s wall-clock cap. Set `NEWS_ENABLED=false` if scans are slow.
- Order book depth results are cached 60s per token pair.
- Odds API fetches all sport keys in parallel (5 workers).
- Leaderboard occasionally hits Cloudflare rate limits — re-run after ~1 minute if picks come back empty.

## Web Dashboard

```bash
python server.py
```

Opens a FastAPI server (default: `http://localhost:8000`) with a live signal feed.

## Disclaimer

This is experimental software. Prediction market trading carries significant financial risk. Always review picks before executing trades. Never bet more than you can afford to lose.
