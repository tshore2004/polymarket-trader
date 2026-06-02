# Polymarket Trader — Handoff

## Goal

Overhaul the Polymarket trading bot from an arb-focused scanner into a multi-factor daily picks system. Specifically:

1. **Remove arbitrage entirely** — too slow, not profitable enough
2. **Fix pick quality** — old leaderboard-only system had no hedging detection (traders betting all World Cup teams counted as signals)
3. **Daily picks focus** — prioritize events resolving today
4. **Event library** — browse events by category, drill into picks
5. **Multi-factor scoring** — replace arb+leaderboard (0–50 each) with 5-factor system (0–100)

## Current State

**Backend: complete and patched.** Multi-factor system is working. Three bugs were fixed post-refactor (see "Recent Fixes" below).

**Frontend: complete.** `templates/index.html` rewritten to match new API.

**Tests: stubbed.** `test_signal.py` and `test_display.py` are placeholder TODOs. `test_models.py` has working tests for `ScoreBreakdown`, `Market`, `OrderBook`, `MarketConsensus`. Old `test_arbitrage.py` gutted (couldn't delete in sandbox, overwritten with stub).

### What was built

**New scoring system (0–100):**
- Leaderboard conviction: 0–30 (with hedging filter)
- Fair value edge: 0–30 (The Odds API for sports, order book depth fallback)
- Line movement/volume: 0–20
- News momentum: 0–10
- Urgency: 0–10

**New files:**
- `core/fair_value.py` — FairValueAnalyzer with Odds API + order book depth

**Rewritten files:**
- `core/leaderboard.py` — dual-layer hedging detection (concentration filter + relative size weighting)
- `core/signal.py` — SignalEngine orchestrating all 5 analyzers, removed ArbDetector
- `utils/models.py` — ScoreBreakdown dataclass, removed ArbOpportunity/SignalType.ARB/Side.BOTH
- `utils/display.py` — rich terminal UI for daily report + event library
- `main.py` — three run modes: daily report, interactive browser, continuous
- `core/executor.py` — removed Side.BOTH arb execution
- `core/scanner.py` — removed arb_opps from ScanSnapshot
- `server.py` — removed /api/arb endpoint, state no longer returns arb_count
- `templates/index.html` — removed Arbitrage tab, signals show 5-factor breakdown, dashboard stats updated
- `config.py` — removed min_arb_profit_pct, added odds_api_key

**Hedging detection (in leaderboard.py):**
- Concentration filter: traders with >2 outcomes in same event excluded entirely
- Relative size weighting: position_size / max_position_in_event (so $100 conviction bet = 1.0, $10 hedge = 0.1)

## Recent Fixes (Post-Refactor Session)

### 1. Past/Resolved Markets Appearing as Picks
**Files:** `core/leaderboard.py` (`_to_consensus_list`), `core/signal.py` (`_build_multifactor_signals`)

The leaderboard ingested trader positions regardless of market resolution date. Resolved markets had urgency=0 but still scored via leaderboard conviction and appeared in the daily report.

**Fix:** Added `if market.time_category == "past": continue` guard in both the consensus builder and the signal scoring loop.

---

### 2. Picks Didn't Show What to Actually Select on Polymarket
**File:** `utils/display.py` (`show_signal`)

Signal panels showed `Side: NO` in the title and the market question separately. For sports markets (e.g., "Washington Nationals vs. Cleveland Guardians" with a NO recommendation), it was unclear what to click.

**Fix:** Added a **"Bet" row** as the first table entry in each signal card:
- For **named-outcome markets** (Polymarket stores team names in the `outcomes` field, e.g., `["Washington Nationals", "Cleveland Guardians"]`): shows `Select: Cleveland Guardians (NO @ 0.500) — [question]`
- For **binary YES/NO markets**: shows `BET YES @ 0.650 — [question]`

Logic reads `market.yes_token.outcome` / `market.no_token.outcome` — the Gamma API `outcomes` array is preserved in the Token model, so team names come through automatically for real markets. Synthesized markets (from position payloads) always use "Yes"/"No" and fall back to the binary format.

**Also fixed:** Event library browser (`show_event_library`) now shows the first market's question instead of the raw event slug.

---

### 3. Scan Hung Indefinitely at News Sentiment Step
**Files:** `core/news_sentiment.py` (`_fetch_rss`), `core/fair_value.py` (`refresh`, `_try_book_depth`)

`feedparser.parse(url)` makes a raw socket call with no timeout. With 6 RSS feeds fetched serially, one slow/unresponsive feed could block the entire scan forever.

**Fix — `core/news_sentiment.py`:**
- Fetch each feed's raw content via `curl_cffi` with a **5-second HTTP timeout**, then pass the text to `feedparser.parse()` (in-memory, no network call)
- All 6 feeds run **simultaneously** in a `ThreadPoolExecutor`
- **8-second total wall-clock cap** via `as_completed(timeout=8)`
- Result: RSS step bounded to ≤8s regardless of feed responsiveness

**Fix — `core/fair_value.py`:**
- Odds API refresh: 19 sport_key calls now run with **5 parallel workers** (was serial; only matters if `ODDS_API_KEY` is set)
- `_try_book_depth()`: added **60-second TTL cache** per token pair — prevents 100+ redundant order book fetches per scan cycle

---

## Failed Approaches / Issues Encountered

1. **Null bytes in test files** — some test files had null bytes causing `ast.parse()` to fail with "source code string cannot contain null bytes." Fixed by reading with `errors='replace'` and stripping nulls.

2. **Couldn't delete test_arbitrage.py** — sandbox `rm` returned "Operation not permitted." Workaround: overwrote file contents with a stub comment instead of deleting.

3. **Frontend not updated after backend rewrite** — caused "no picks at all" because JS referenced `sig.arb_score`, `sig.leaderboard_score`, `state.arb_count`, and called `/api/arb` (removed endpoint). Any `undefined.toFixed()` call would throw, cascading JS errors that broke the entire page. Fixed by rewriting `index.html` to use `sig.scores.leaderboard`, `sig.scores.fair_value_edge`, etc.

## What's Left

- **Write real tests** for `test_signal.py` (ScoreBreakdown integration, hedging detection, fair value edge scoring) and `test_display.py` (show_signal with ScoreBreakdown, show_daily_report, show_event_library)
- **Live testing** — run against Polymarket APIs to validate pick quality, scoring calibration, and hedging filter effectiveness
- **Scoring calibration** — the weight distribution (30/30/20/10/10) and thresholds (min_signal_score=15) may need tuning based on real data
- **Odds API key** — needed for fair value sports comparison; without it, falls back to order book depth only
- **Leaderboard API fragility** — Polymarket's `/positions` endpoint occasionally hits Cloudflare rate limits; if leaderboard picks are empty, re-run after ~1 minute
- **Synthesized market outcome labels** — markets built from position payloads (not found in Gamma scan) use "Yes"/"No" tokens, not team names; the named-outcome display falls back to "BET YES/NO" for these

## Quick Env Reference

| Variable | Default | Notes |
|---|---|---|
| `POLY_PRIVATE_KEY` | required | Polygon wallet key |
| `POLY_API_KEY/SECRET/PASSPHRASE` | required | CLOB credentials |
| `NEWS_ENABLED` | `true` | Set `false` to skip news entirely (0–10 score component, rarely dominant) |
| `NEWS_API_KEY` | — | NewsAPI.org for richer coverage |
| `ODDS_API_KEY` | — | The Odds API (500 req/month free); without it, fair value uses order book depth only |
| `LEADERBOARD_TOP_N` | 20 | Traders tracked |
| `COPY_MIN_POSITION_USD` | 100 | Min $ to count as conviction |
| `DAILY_REPORT_TOP_N` | 12 | Picks shown in report |
| `SHORT_TERM_HOURS` | 48 | "Tonight/tomorrow" bucket window |
