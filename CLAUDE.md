# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Bot

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in credentials
python main.py
```

The bot offers three run modes at startup:
1. **Daily Report** — one-shot ranked picks, prioritizing events resolving today
2. **Interactive Browse** — event library grouped by category, drill into picks per event
3. **Continuous** — legacy scan loop with configurable interval

## Architecture

Multi-factor scoring system with hedging-aware leaderboard analysis:

1. **Leaderboard** (`core/leaderboard.py`): Tracks top traders' open positions with conviction filtering — traders who bet on >2 outcomes in the same event are flagged as hedging and excluded/downweighted. Scores 0–30.
2. **Fair Value** (`core/fair_value.py`): Compares Polymarket odds to external sources (The Odds API for sports, order book depth analysis). Scores 0–30.
3. **Volume/Line Movement** (`core/volume_tracker.py`): Detects unusual volume spikes and price momentum. Scores 0–20.
4. **News Sentiment** (`core/news_sentiment.py`): Matches trending headlines to market questions. Scores 0–10.
5. **Urgency** (built-in): Markets resolving sooner get higher urgency scores. 0–10.

Combined score: 0–100. `SignalEngine` (`core/signal.py`) orchestrates all analyzers and produces ranked signals.

**Data flow:**
```
main.py
  ├─ Daily Report mode
  │    └─ SignalEngine.scan(mode="today") → Signal[] → show_daily_report
  ├─ Interactive Browse mode
  │    └─ SignalEngine.scan() → get_todays_events()/get_all_events() → user selects → drill into picks
  └─ Continuous mode
       └─ SignalEngine.scan() loop → present → confirm → execute
```

## Key Files

| File | Role |
|------|------|
| `config.py` | `Config` dataclass; loads from `.env` via `python-dotenv` |
| `utils/models.py` | All data models: `Market`, `Signal`, `ScoreBreakdown`, `MarketConsensus`, `TradeResult`, etc. |
| `utils/display.py` | `rich`-based terminal UI: signal panels, daily report, event library browser |
| `utils/categories.py` | Market category detection from tags and question text |
| `core/api_client.py` | Read-only HTTP client for Polymarket public APIs |
| `core/leaderboard.py` | Hedging-aware leaderboard consensus with conviction weighting |
| `core/fair_value.py` | External odds comparison (The Odds API) + order book depth analysis |
| `core/news_sentiment.py` | RSS/NewsAPI headline matching for news momentum |
| `core/volume_tracker.py` | Volume spike and price movement detection |
| `core/signal.py` | `SignalEngine` — orchestrates all analyzers, multi-factor scoring |
| `core/executor.py` | Prompts user, sizes bets, places orders via `py-clob-client` |
| `core/starred_traders.py` | JSON-backed starred trader persistence |
| `core/scanner.py` | Background daemon thread for web server mode |
| `server.py` | FastAPI web dashboard |

## Environment Variables

Required in `.env` (see `.env.example`):
- `POLY_PRIVATE_KEY` — Polygon wallet private key
- `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE` — Polymarket CLOB API credentials

Optional:
- `ODDS_API_KEY` — The Odds API key for sportsbook odds comparison (free tier: 500 req/month)
- `NEWS_API_KEY` — NewsAPI.org key for richer news coverage
- `NEWS_ENABLED` — set `false` to skip news sentiment entirely (useful if scan is slow; news is 0–10 and rarely the dominant signal)
- `MAX_BET_SIZE`, `MIN_BET_SIZE`, `SCAN_INTERVAL`, `SCAN_MODE`
- `LEADERBOARD_TOP_N`, `LEADERBOARD_MIN_PROFIT`, `LEADERBOARD_MIN_VOLUME`, `LEADERBOARD_WINDOW`
- `COPY_MIN_POSITION_USD` — minimum position size ($) to count as conviction (default 100)
- `DAILY_REPORT_TOP_N` — number of picks shown in daily report (default 12)
- `SHORT_TERM_HOURS` — hours window for "tonight/tomorrow" bucket (default 48)

## Trader Quality Scoring

Traders are scored for **consistency**, not just raw profit. The formula:

| Factor | Weight | What it rewards |
|--------|--------|-----------------|
| Profit (dampened) | 25% | Profit × consistency — big wins only count if backed by trade count |
| Volume | 15% | Active participation |
| Win rate | 30% | `pct_positive` — strongest consistency signal |
| Consistency | 30% | Log-scaled trade count — need ~50 trades to be "proven" |

Each trader gets a letter grade (A/B/C/D) shown in pick cards. A one-hit wonder with 3 trades and $80k profit gets grade C; a grinder with 200 trades and 63% win rate gets grade A.

## Starred Traders

Star traders you trust to track them across sessions:
- `star <pick#>` — star all traders backing a pick
- `unstar <address>` — remove a starred trader
- `stars` — view all starred traders with positions

Starred traders are loaded even if they fall off the leaderboard. Data persists in `starred_traders.json`.

## Resolved Event Filtering

Markets are filtered at multiple layers:
- Position-level: `curPrice` of 0 or 1 → already settled, skip entirely
- Market-level: `end_date` in the past, `closed=true`, or `active=false` → excluded
- Synthesized markets: if no `end_date` but `curPrice` is pinned → marked as past

## Scoring System (0–100)

| Factor | Weight | Source |
|--------|--------|--------|
| Leaderboard conviction | 0–30 | Top trader consensus with hedging filter |
| Fair value edge | 0–30 | External odds vs Polymarket price |
| Line movement | 0–20 | Volume spikes + price momentum |
| News momentum | 0–10 | Trending headlines matching market |
| Urgency | 0–10 | Time to resolution boost |

## Hedging Detection

The leaderboard analyzer filters out noise from traders who bet on multiple outcomes in the same event:
- **Concentration filter**: If a trader holds positions in >2 markets within the same event (e.g., 5 World Cup teams), they're excluded entirely
- **Relative size weighting**: For non-excluded traders, position weight = (this position size / max position in event), so a $100 conviction bet alongside a $10 hedge gets weighted 1.0 vs 0.1

## Bet Sizing

`TradeExecutor.execute()` scales bet size linearly: `min_bet_size` ($5) at score 0, `max_bet_size` ($25) at score 100.

## No Test Suite

There are no maintained automated tests. Changes to execution logic should be tested carefully against Polymarket's sandbox/test environment before use with real credentials.

## Performance Notes

- **RSS news fetching** is bounded to ≤8 seconds total: feeds are fetched in parallel via `curl_cffi` with a 5s per-feed timeout and an 8s wall-clock cap. If it's still slow, set `NEWS_ENABLED=false`.
- **Order book depth** results are cached for 60 seconds per token pair to avoid redundant fetches within a scan cycle.
- **Odds API** (if `ODDS_API_KEY` set) fetches all sport keys in parallel with 5 workers.
- **Leaderboard** hits Cloudflare rate limits occasionally — if picks come back empty, re-run after ~1 minute.

## Pick Display

Each signal card shows a **"Bet" row** as the first item:
- Sports/named-outcome markets: `Select: Cleveland Guardians (NO @ 0.500)` — reads the actual outcome label from the Gamma API `outcomes` field
- Binary YES/NO markets: `BET YES @ 0.650 — Will X happen?`

Past/resolved markets are filtered out at both the leaderboard consensus stage and the signal scoring stage.
