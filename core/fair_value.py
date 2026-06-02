"""FairValueAnalyzer — compares Polymarket odds to external sources to find mispriced markets.

Supports:
  - The Odds API (sports): set ODDS_API_KEY in .env for sportsbook consensus
  - Order book depth analysis: detects imbalanced books as a proxy for smart money
  - Fallback: internal momentum-based fair value when no external data is available
"""
from __future__ import annotations
import logging
import re
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from collections import defaultdict

from core.api_client import PolymarketPublicClient
from utils.models import Market

logger = logging.getLogger(__name__)

# Sports we can match to The Odds API sport keys
_POLYMARKET_TO_ODDS_API: dict[str, list[str]] = {
    "nfl": ["americanfootball_nfl"],
    "nba": ["basketball_nba"],
    "mlb": ["baseball_mlb"],
    "nhl": ["icehockey_nhl"],
    "soccer": [
        "soccer_fifa_world_cup", "soccer_epl", "soccer_spain_la_liga",
        "soccer_germany_bundesliga", "soccer_italy_serie_a", "soccer_france_ligue_one",
        "soccer_uefa_champs_league", "soccer_usa_mls",
    ],
    "tennis": ["tennis_atp_french_open", "tennis_atp_wimbledon", "tennis_atp_us_open"],
    "golf": ["golf_pga_championship", "golf_masters_tournament"],
    "ufc": ["mma_mixed_martial_arts"],
    "formula-1": ["motorsport_formula_one"],
}


class FairValueAnalyzer:
    """
    Estimates fair value for Polymarket markets by comparing to external odds sources.

    When external odds are available (The Odds API for sports), computes the edge
    as the difference between Polymarket's implied probability and the external consensus.
    When no external data exists, uses order book depth imbalance as a weaker proxy.
    """

    _BOOK_CACHE_TTL = 60.0  # seconds

    def __init__(self, client: PolymarketPublicClient, odds_api_key: str = "") -> None:
        self._client = client
        self._odds_api_key = odds_api_key
        self._external_odds: dict[str, dict[str, float]] = {}  # sport_key → {team/outcome: probability}
        self._last_fetch: float = 0.0
        self._book_cache: dict[str, tuple[Optional[float], str, float]] = {}  # key → (fv, source, ts)

    def refresh(self) -> None:
        """Fetch fresh external odds data."""
        if not self._odds_api_key:
            return

        now = _time.monotonic()
        if now - self._last_fetch < 300:  # cache for 5 min
            return
        self._last_fetch = now

        self._external_odds = {}
        all_sport_keys = [sk for sks in _POLYMARKET_TO_ODDS_API.values() for sk in sks]
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(self._fetch_odds_api, sk): sk for sk in all_sport_keys}
            for fut in as_completed(futures):
                sk = futures[fut]
                try:
                    odds = fut.result()
                    if odds:
                        self._external_odds[sk] = odds
                except Exception as exc:
                    logger.debug("Odds API future failed for %s: %s", sk, exc)

        total_outcomes = sum(len(v) for v in self._external_odds.values())
        logger.info("FairValue: fetched %d sport keys, %d outcomes from The Odds API.",
                     len(self._external_odds), total_outcomes)

    def estimate_fair_value(self, market: Market) -> tuple[Optional[float], str]:
        """
        Return (fair_value_probability, source_description) for a market.
        fair_value is 0.0–1.0 or None if we can't estimate.
        """
        # Try external odds first (sports markets)
        fv, source = self._try_external_odds(market)
        if fv is not None:
            return fv, source

        # Fallback: order book depth analysis
        fv, source = self._try_book_depth(market)
        if fv is not None:
            return fv, source

        return None, ""

    def edge(self, market: Market, side: str, poly_price: float) -> tuple[float, Optional[float], str]:
        """
        Compute edge for a given side/price.
        Returns (edge_pct, fair_value, source).
        edge_pct > 0 means Polymarket is underpricing (buy opportunity).
        """
        fv, source = self.estimate_fair_value(market)
        if fv is None:
            return 0.0, None, ""

        if side.upper() in ("YES", "1"):
            # If fair value for YES is higher than Polymarket price, there's positive edge
            edge_pct = fv - poly_price
        else:
            # For NO, fair value is (1 - fv)
            edge_pct = (1.0 - fv) - poly_price

        return round(edge_pct, 4), round(fv, 4), source

    def score(self, edge_pct: float) -> float:
        """Convert edge percentage to 0–30 score. 10%+ edge = max score."""
        if edge_pct <= 0:
            return 0.0
        # Linear scale: 1% edge = 3 points, 10% edge = 30 points
        return min(30.0, edge_pct * 300.0)

    # ── External Odds (The Odds API) ─────────────────────────────────────────

    def _fetch_odds_api(self, sport_key: str) -> dict[str, float]:
        """Fetch consensus odds from The Odds API. Returns {outcome_name: implied_probability}."""
        if not self._odds_api_key:
            return {}

        try:
            from curl_cffi import requests as cf_requests
            resp = cf_requests.get(
                f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds",
                params={
                    "apiKey": self._odds_api_key,
                    "regions": "us,eu",
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                },
                impersonate="chrome120",
                timeout=10,
            )
            if resp.status_code != 200:
                logger.debug("Odds API %s returned %d", sport_key, resp.status_code)
                return {}

            data = resp.json()
            return self._parse_odds_response(data)

        except Exception as exc:
            logger.debug("Odds API fetch failed for %s: %s", sport_key, exc)
            return {}

    @staticmethod
    def _parse_odds_response(events: list[dict]) -> dict[str, float]:
        """
        Average decimal odds across all bookmakers to get consensus implied probability.
        Returns {team_name_lower: avg_implied_probability}.
        """
        outcome_odds: dict[str, list[float]] = defaultdict(list)

        for event in events:
            for bookmaker in event.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    if market.get("key") != "h2h":
                        continue
                    for outcome in market.get("outcomes", []):
                        name = outcome.get("name", "").lower().strip()
                        price = outcome.get("price", 0)
                        if name and price > 1.0:
                            # Decimal odds → implied probability
                            outcome_odds[name].append(1.0 / price)

        result = {}
        for name, probs in outcome_odds.items():
            avg = sum(probs) / len(probs)
            result[name] = round(avg, 4)

        # Normalize probabilities to sum to ~1.0 (remove vig)
        total = sum(result.values())
        if total > 0:
            result = {k: round(v / total, 4) for k, v in result.items()}

        return result

    def _try_external_odds(self, market: Market) -> tuple[Optional[float], str]:
        """Try to match market question to external odds data."""
        if not self._external_odds:
            return None, ""

        question_lower = market.question.lower()

        for sport_key, outcomes in self._external_odds.items():
            for team_name, prob in outcomes.items():
                # Check if the team name appears in the question
                if team_name in question_lower or _fuzzy_match(team_name, question_lower):
                    return prob, f"Sportsbook consensus ({sport_key}): {prob*100:.1f}%"

        return None, ""

    # ── Order Book Depth Analysis ────────────────────────────────────────────

    def _try_book_depth(self, market: Market) -> tuple[Optional[float], str]:
        """Use order book depth imbalance as weak fair value signal."""
        yes_tok = market.yes_token
        no_tok = market.no_token
        if not yes_tok or not no_tok:
            return None, ""

        cache_key = f"{yes_tok.token_id}:{no_tok.token_id}"
        now = _time.monotonic()
        cached = self._book_cache.get(cache_key)
        if cached is not None:
            fv, source, ts = cached
            if now - ts < self._BOOK_CACHE_TTL:
                return fv, source

        try:
            yes_book = self._client.get_orderbook(yes_tok.token_id)
            no_book = self._client.get_orderbook(no_tok.token_id)
        except Exception:
            return None, ""

        yes_bid_liq = yes_book.bid_liquidity
        no_bid_liq = no_book.bid_liquidity

        total_liq = yes_bid_liq + no_bid_liq
        if total_liq < 100:  # not enough liquidity to read signal
            self._book_cache[cache_key] = (None, "", now)
            return None, ""

        fv = yes_bid_liq / total_liq
        source = f"Book depth: YES ${yes_bid_liq:,.0f} / NO ${no_bid_liq:,.0f} → {fv*100:.0f}% implied"
        result = round(fv, 4)
        self._book_cache[cache_key] = (result, source, now)
        return result, source


def _fuzzy_match(team: str, question: str) -> bool:
    """Basic fuzzy match — check if all significant words of the team appear in the question."""
    team_words = [w for w in team.split() if len(w) > 2]
    if not team_words:
        return False
    return all(w in question for w in team_words)
