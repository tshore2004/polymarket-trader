"""Backtest infrastructure — signal logging + automated resolution tracking.

Provides:
  - SignalLogger: persists every generated signal to SQLite
  - ResolutionPoller: background thread that checks unresolved tracked markets
    against the Polymarket API and records outcomes + P&L
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.api_client import PolymarketPublicClient
from utils.categories import detect_market_category
from utils.models import Market, Signal, ScoreBreakdown, Side

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent.parent / "backtest.db"

# How often the resolution poller checks unresolved picks (seconds)
_POLL_INTERVAL = 1800  # 30 minutes


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class SignalLogger:
    """Logs signals to SQLite and tracks their resolution."""

    def __init__(self, db_path: Path = _DB_PATH) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS picks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    condition_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    recommended_side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    combined_score REAL NOT NULL,
                    score_leaderboard REAL DEFAULT 0,
                    score_fair_value REAL DEFAULT 0,
                    score_line_movement REAL DEFAULT 0,
                    score_news REAL DEFAULT 0,
                    score_urgency REAL DEFAULT 0,
                    fair_value_estimate REAL,
                    edge_pct REAL DEFAULT 0,
                    explanation TEXT DEFAULT '',
                    event_slug TEXT DEFAULT '',
                    end_date TEXT,
                    category TEXT DEFAULT '',
                    logged_at TEXT NOT NULL,
                    -- Resolution fields (filled later by poller)
                    resolved_at TEXT,
                    outcome TEXT,
                    exit_price REAL,
                    pnl REAL,
                    won INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_picks_unresolved
                    ON picks(resolved_at) WHERE resolved_at IS NULL;

                CREATE INDEX IF NOT EXISTS idx_picks_condition
                    ON picks(condition_id);

                CREATE TABLE IF NOT EXISTS stats_cache (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            # Migration: add category column to existing DBs
            cols = {row[1] for row in conn.execute("PRAGMA table_info(picks)")}
            if "category" not in cols:
                conn.execute("ALTER TABLE picks ADD COLUMN category TEXT DEFAULT ''")
                logger.info("Migrated backtest.db: added category column.")

    def log_signal(self, signal: Signal, min_score: float = 0.0) -> Optional[int]:
        """Log a signal if it meets the minimum score threshold.

        Returns the pick ID if logged, None if skipped.
        """
        if signal.combined_score < min_score:
            return None

        market = signal.market
        scores = signal.scores

        entry_price = signal.recommended_price
        side = signal.recommended_side.value

        # Second-line defense: never log a near-resolved price that slipped through signal.py
        if entry_price <= 0.03 or entry_price >= 0.97:
            logger.warning(
                "Skipping log: near-resolved price %.3f for %s", entry_price, market.question[:50]
            )
            return None

        end_date_str = market.end_date.isoformat() if market.end_date else None
        category, _ = detect_market_category(market)
        now = _now_utc().isoformat()

        with self._lock:
            with self._conn() as conn:
                # Avoid duplicate: same market + side within last 2 hours
                existing = conn.execute("""
                    SELECT id FROM picks
                    WHERE condition_id = ? AND recommended_side = ?
                      AND logged_at > datetime(?, '-2 hours')
                      AND resolved_at IS NULL
                """, (market.condition_id, side, now)).fetchone()

                if existing:
                    return None

                cur = conn.execute("""
                    INSERT INTO picks (
                        condition_id, question, recommended_side, entry_price,
                        combined_score, score_leaderboard, score_fair_value,
                        score_line_movement, score_news, score_urgency,
                        fair_value_estimate, edge_pct, explanation,
                        event_slug, end_date, category, logged_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    market.condition_id, market.question, side, entry_price,
                    signal.combined_score, scores.leaderboard, scores.fair_value_edge,
                    scores.line_movement, scores.news_momentum, scores.urgency,
                    signal.fair_value, signal.edge_pct, signal.explanation,
                    market.event_slug, end_date_str, category, now,
                ))
                pick_id = cur.lastrowid

        logger.info("Logged pick #%d: %s %s @ %.3f (score %.1f)",
                    pick_id, side, market.question[:50], entry_price, signal.combined_score)
        return pick_id

    def log_signals(self, signals: list[Signal], min_score: float = 50.0) -> int:
        """Log multiple signals. Returns count of newly logged picks."""
        count = 0
        for sig in signals:
            if self.log_signal(sig, min_score=min_score) is not None:
                count += 1
        return count

    def get_unresolved(self) -> list[dict]:
        """Get all picks that haven't been resolved yet."""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM picks WHERE resolved_at IS NULL
                ORDER BY logged_at DESC
            """).fetchall()
        return [dict(r) for r in rows]

    def resolve_pick(self, pick_id: int, outcome: str, exit_price: float) -> None:
        """Mark a pick as resolved and compute P&L.

        outcome: 'YES' or 'NO' — what the market resolved to
        exit_price: 1.0 if won, 0.0 if lost
        """
        now = _now_utc().isoformat()

        with self._lock:
            with self._conn() as conn:
                row = conn.execute("SELECT * FROM picks WHERE id = ?", (pick_id,)).fetchone()
                if not row:
                    return

                side = row["recommended_side"]
                entry = row["entry_price"]

                # Did our recommended side win?
                won = 1 if outcome == side else 0

                # P&L per $1 bet: if won, profit = (1 - entry_price); if lost, loss = -entry_price
                if won:
                    pnl = 1.0 - entry
                else:
                    pnl = -entry

                conn.execute("""
                    UPDATE picks SET
                        resolved_at = ?, outcome = ?, exit_price = ?, pnl = ?, won = ?
                    WHERE id = ?
                """, (now, outcome, exit_price, pnl, won, pick_id))

        logger.info("Resolved pick #%d: %s (side=%s, won=%s, pnl=%.3f)",
                    pick_id, row["question"][:40], side, bool(won), pnl)

    def get_performance(self) -> dict:
        """Compute aggregate performance stats."""
        with self._conn() as conn:
            resolved = conn.execute("""
                SELECT * FROM picks WHERE resolved_at IS NOT NULL
            """).fetchall()

            total_picks = conn.execute("SELECT COUNT(*) FROM picks").fetchone()[0]
            unresolved = conn.execute(
                "SELECT COUNT(*) FROM picks WHERE resolved_at IS NULL"
            ).fetchone()[0]

        if not resolved:
            return {
                "total_picks": total_picks,
                "resolved": 0,
                "unresolved": unresolved,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "avg_pnl_per_pick": 0.0,
                "roi_pct": 0.0,
                "by_score_tier": {},
                "by_factor": {},
            }

        wins = sum(1 for r in resolved if r["won"])
        losses = len(resolved) - wins
        total_pnl = sum(r["pnl"] for r in resolved)
        total_risked = sum(r["entry_price"] for r in resolved)

        # Performance by score tier
        tiers = {"90-100": [], "70-89": [], "50-69": [], "0-49": []}
        for r in resolved:
            score = r["combined_score"]
            if score >= 90:
                tiers["90-100"].append(r)
            elif score >= 70:
                tiers["70-89"].append(r)
            elif score >= 50:
                tiers["50-69"].append(r)
            else:
                tiers["0-49"].append(r)

        by_score_tier = {}
        for tier_name, picks in tiers.items():
            if not picks:
                by_score_tier[tier_name] = {"count": 0, "win_rate": 0, "avg_pnl": 0}
                continue
            tier_wins = sum(1 for p in picks if p["won"])
            tier_pnl = sum(p["pnl"] for p in picks)
            by_score_tier[tier_name] = {
                "count": len(picks),
                "win_rate": round(tier_wins / len(picks), 3),
                "avg_pnl": round(tier_pnl / len(picks), 4),
            }

        # Factor contribution analysis: avg factor score for wins vs losses
        factor_cols = [
            "score_leaderboard", "score_fair_value",
            "score_line_movement", "score_news", "score_urgency",
        ]
        by_factor = {}
        for col in factor_cols:
            win_scores = [r[col] for r in resolved if r["won"]]
            loss_scores = [r[col] for r in resolved if not r["won"]]
            by_factor[col.replace("score_", "")] = {
                "avg_in_wins": round(sum(win_scores) / len(win_scores), 2) if win_scores else 0,
                "avg_in_losses": round(sum(loss_scores) / len(loss_scores), 2) if loss_scores else 0,
            }

        return {
            "total_picks": total_picks,
            "resolved": len(resolved),
            "unresolved": unresolved,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / len(resolved), 3),
            "total_pnl": round(total_pnl, 4),
            "avg_pnl_per_pick": round(total_pnl / len(resolved), 4),
            "roi_pct": round((total_pnl / total_risked) * 100, 2) if total_risked > 0 else 0,
            "by_score_tier": by_score_tier,
            "by_factor": by_factor,
        }

    def get_recent_picks(self, limit: int = 50) -> list[dict]:
        """Get most recent picks with resolution status."""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM picks ORDER BY logged_at DESC LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]


class ResolutionPoller:
    """Background thread that checks unresolved picks for market resolution."""

    def __init__(
        self,
        signal_logger: SignalLogger,
        client: PolymarketPublicClient,
        poll_interval: float = _POLL_INTERVAL,
    ) -> None:
        self._logger = signal_logger
        self._client = client
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="resolution-poller", daemon=True
        )
        self._thread.start()
        logger.info("ResolutionPoller started (interval=%ds).", self._poll_interval)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        logger.info("ResolutionPoller stopped.")

    def poll_once(self) -> int:
        """Check all unresolved picks. Returns number resolved this cycle.

        Fetches each market from the CLOB API (which returns real token prices)
        rather than the Gamma API (which leaves Token.price at 0.0). Without
        real prices, _check_resolution can never detect pinned 0/1 outcomes.
        One CLOB call per unique condition_id, parallelised with 5 workers.
        """
        unresolved = self._logger.get_unresolved()
        if not unresolved:
            return 0

        # Deduplicate condition IDs, then fetch from CLOB in parallel
        unique_cids = list({p["condition_id"] for p in unresolved})

        market_map: dict[str, Market] = {}
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(self._client.get_market_from_clob, cid): cid
                       for cid in unique_cids}
            for fut in as_completed(futures):
                m = fut.result()
                if m is not None:
                    market_map[m.condition_id] = m

        resolved_count = 0
        for pick in unresolved:
            cid = pick["condition_id"]
            market = market_map.get(cid)
            if not market:
                continue

            outcome = self._check_resolution(market)
            if outcome is None:
                # Warn if price has drifted far from entry — informational only, pick kept.
                entry = pick["entry_price"]
                pick_side = pick["recommended_side"]
                drift_token = market.yes_token if pick_side == "YES" else market.no_token
                if drift_token and drift_token.price > 0:
                    drift = abs(drift_token.price - entry)
                    if drift > 0.40:
                        logger.warning(
                            "Large price drift on pick #%d: %s %s @ %.3f → now %.3f (Δ%.2f)",
                            pick["id"], pick_side, pick["question"][:40],
                            entry, drift_token.price, drift,
                        )
                continue

            # Market has resolved — determine exit price
            exit_price = 1.0 if outcome == pick["recommended_side"] else 0.0
            self._logger.resolve_pick(pick["id"], outcome, exit_price)
            resolved_count += 1

        if resolved_count:
            logger.info("Resolved %d/%d picks this cycle.", resolved_count, len(unresolved))
        return resolved_count

    @staticmethod
    def _check_resolution(market: Market) -> Optional[str]:
        """Determine if a market has resolved. Returns 'YES'/'NO' or None if still open.

        Primary path: market.closed == True + pinned token prices.
        Fallback: end_date well past + pinned prices (handles API lag where closed
        flag hasn't propagated yet but settlement prices are already final).
        """
        yes_token = market.yes_token
        no_token = market.no_token
        if not yes_token or not no_token:
            return None

        def _read_prices() -> Optional[str]:
            yes_p = yes_token.price
            no_p = no_token.price
            if yes_p >= 0.95:
                return "YES"
            if no_p >= 0.95:
                return "NO"
            if yes_p <= 0.05 and no_p >= 0.90:
                return "NO"
            if no_p <= 0.05 and yes_p >= 0.90:
                return "YES"
            return None

        # Primary: closed flag set by the API
        if market.closed:
            return _read_prices()

        # Fallback: if end_date is more than 1 hour in the past, trust the prices
        # even if the API hasn't flipped closed=True yet
        if market.end_date:
            now = _now_utc()
            ed = market.end_date
            if ed.tzinfo is None:
                ed = ed.replace(tzinfo=timezone.utc)
            if (now - ed).total_seconds() > 3600:
                return _read_prices()

        return None

    def _loop(self) -> None:
        # Poll immediately on startup — resolve any picks logged before the last restart
        try:
            n = self.poll_once()
            if n:
                logger.info("Startup poll resolved %d pick(s).", n)
        except Exception as exc:
            logger.error("ResolutionPoller startup poll error: %s", exc)

        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._poll_interval)
            if self._stop_event.is_set():
                break
            try:
                self.poll_once()
            except Exception as exc:
                logger.error("ResolutionPoller error: %s", exc)
