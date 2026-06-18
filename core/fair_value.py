"""FairValueAnalyzer — compares Polymarket odds to external sources to find mispriced markets.

Supports (in priority order):
  - Pinnacle (sharp book): set PINNACLE_USERNAME / PINNACLE_PASSWORD — highest accuracy
  - The Odds API (recreational consensus): set ODDS_API_KEY — good fallback
  - Order book depth analysis: always available, weaker signal
"""
from __future__ import annotations
import logging
import re
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from collections import defaultdict

from core.api_client import PolymarketPublicClient
from core.pinnacle import PinnacleClient
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

    def __init__(
        self,
        client: PolymarketPublicClient,
        odds_api_key: str = "",
        pinnacle: Optional["PinnacleClient"] = None,
    ) -> None:
        self._client = client
        self._odds_api_key = odds_api_key
        self._pinnacle = pinnacle
        self._external_odds: dict[str, dict[str, float]] = {}  # sport_key → {team/outcome: probability}
        self._pinnacle_teams: set[str] = set()  # team names whose odds came from Pinnacle via Odds API
        self._last_fetch: float = 0.0
        self._book_cache: dict[str, tuple[Optional[float], str, float]] = {}  # key → (fv, source, ts)

    def refresh(self) -> None:
        """Fetch fresh external odds data (Pinnacle + The Odds API)."""
        # Pinnacle is refreshed independently with its own TTL
        if self._pinnacle and self._pinnacle.enabled:
            self._pinnacle.refresh()

        if not self._odds_api_key:
            return

        now = _time.monotonic()
        if now - self._last_fetch < 300:  # cache for 5 min
            return
        self._last_fetch = now

        self._external_odds = {}
        self._pinnacle_teams = set()
        all_sport_keys = [sk for sks in _POLYMARKET_TO_ODDS_API.values() for sk in sks]
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(self._fetch_odds_api, sk): sk for sk in all_sport_keys}
            for fut in as_completed(futures):
                sk = futures[fut]
                try:
                    odds, pin_names = fut.result()
                    if odds:
                        self._external_odds[sk] = odds
                        self._pinnacle_teams.update(pin_names)
                except Exception as exc:
                    logger.debug("Odds API future failed for %s: %s", sk, exc)

        total_outcomes = sum(len(v) for v in self._external_odds.values())
        logger.info(
            "FairValue: fetched %d sport keys, %d outcomes (%d from Pinnacle) via The Odds API.",
            len(self._external_odds), total_outcomes, len(self._pinnacle_teams),
        )

    def estimate_fair_value(self, market: Market) -> tuple[Optional[float], str]:
        """
        Return (fair_value_probability, source_description) for a market.
        fair_value is 0.0–1.0 or None if we can't estimate.

        Priority: Pinnacle (sharp) → The Odds API (consensus) → order book depth.
        """
        # 1. Pinnacle sharp-book odds (most accurate)
        if self._pinnacle and self._pinnacle.enabled:
            fv, source = self._try_pinnacle_odds(market)
            if fv is not None:
                return fv, source

        # 2. The Odds API consensus
        fv, source = self._try_external_odds(market)
        if fv is not None:
            return fv, source

        # 3. Fallback: order book depth analysis
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

    # ── Pinnacle Sharp-Book Odds ─────────────────────────────────────────────

    def _try_pinnacle_odds(self, market: Market) -> tuple[Optional[float], str]:
        """Match a market question against Pinnacle's cached moneyline odds."""
        if not self._pinnacle:
            return None, ""

        question_lower = market.question.lower()
        all_odds = self._pinnacle.get_all_odds()

        for sport_key, outcomes in all_odds.items():
            for team_name, prob in outcomes.items():
                if team_name in question_lower or _fuzzy_match(team_name, question_lower):
                    return prob, f"Pinnacle ({sport_key}): {prob*100:.1f}%"

        return None, ""

    # ── External Odds (The Odds API) ─────────────────────────────────────────

    def _fetch_odds_api(self, sport_key: str) -> tuple[dict[str, float], set[str]]:
        """Fetch odds from The Odds API, preferring Pinnacle when available.

        Returns (odds_dict, pinnacle_team_names).
        """
        if not self._odds_api_key:
            return {}, set()

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
                return {}, set()

            return self._parse_odds_response(resp.json())

        except Exception as exc:
            logger.debug("Odds API fetch failed for %s: %s", sport_key, exc)
            return {}, set()

    @staticmethod
    def _parse_odds_response(events: list[dict]) -> tuple[dict[str, float], set[str]]:
        """Parse Odds API response, using Pinnacle's line per event when available.

        Pinnacle is a sharp, low-margin book — their implied probabilities are
        more accurate than a consensus average across recreational sportsbooks.
        Falls back to consensus average for events Pinnacle doesn't cover.

        Returns (odds_dict, pinnacle_team_names).
        """
        result: dict[str, float] = {}
        pinnacle_names: set[str] = set()

        for event in events:
            bookmakers = event.get("bookmakers", [])
            pinnacle_bm = next((b for b in bookmakers if b.get("key") == "pinnacle"), None)

            if pinnacle_bm:
                # Use Pinnacle's line and devig it
                for mkt in pinnacle_bm.get("markets", []):
                    if mkt.get("key") != "h2h":
                        continue
                    names, probs = [], []
                    for o in mkt.get("outcomes", []):
                        name = o.get("name", "").lower().strip()
                        price = o.get("price", 0)
                        if name and price > 1.0:
                            names.append(name)
                            probs.append(1.0 / price)
                    if probs:
                        total = sum(probs)
                        for name, prob in zip(names, probs):
                            result[name] = round(prob / total, 4)
                            pinnacle_names.add(name)
            else:
                # Consensus average across all bookmakers for this event
                outcome_odds: dict[str, list[float]] = defaultdict(list)
                for bm in bookmakers:
                    for mkt in bm.get("markets", []):
                        if mkt.get("key") != "h2h":
                            continue
                        for o in mkt.get("outcomes", []):
                            name = o.get("name", "").lower().strip()
                            price = o.get("price", 0)
                            if name and price > 1.0:
                                outcome_odds[name].append(1.0 / price)
                if outcome_odds:
                    event_probs = {n: sum(ps) / len(ps) for n, ps in outcome_odds.items()}
                    total = sum(event_probs.values())
                    if total > 0:
                        for name, prob in event_probs.items():
                            result[name] = round(prob / total, 4)

        return result, pinnacle_names

    def _try_external_odds(self, market: Market) -> tuple[Optional[float], str]:
        """Match market question against cached Odds API data."""
        if not self._external_odds:
            return None, ""

        question_lower = market.question.lower()

        for sport_key, outcomes in self._external_odds.items():
            for team_name, prob in outcomes.items():
                if team_name in question_lower or _fuzzy_match(team_name, question_lower):
                    if team_name in self._pinnacle_teams:
                        source = f"Pinnacle via The Odds API ({sport_key}): {prob*100:.1f}%"
                    else:
                        source = f"Sportsbook consensus ({sport_key}): {prob*100:.1f}%"
                    return prob, source

        return None, ""

    # ── Order Book Depth Analysis ────────────────────────────────────────────

    def _try_book_depth(self, market: Market) -> tuple[Optional[float], str]:
        """Estimate fair value from near-market order book depth imbalance.

        Compares YES vs NO buying pressure within 8% of the current price.
        An imbalance shifts fair value by up to ±10% from the market price,
        giving a realistic edge signal (typically 0–8%) without the ±30-40%
        distortion caused by summing all bids regardless of price level.
        """
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

        if total_liq < 100:
            self._book_cache[cache_key] = (None, "", now)
            return None, ""

        # Anchor on mid-price; fall back to token price if order book is thin
        yes_bid = yes_book.best_bid
        yes_ask = yes_book.best_ask
        if yes_bid is not None and yes_ask is not None:
            yes_price = (yes_bid + yes_ask) / 2
        elif yes_tok.price > 0:
            yes_price = yes_tok.price
        else:
            yes_price = 0.5

        # Near-market depth: bids placed within 8% of the current price.
        # These represent active buying intent and are a reliable pressure signal.
        window = 0.08
        no_price = 1.0 - yes_price
        yes_near = sum(l.size for l in yes_book.bids if l.price >= yes_price - window)
        no_near = sum(l.size for l in no_book.bids if l.price >= no_price - window)
        total_near = yes_near + no_near

        if total_near >= 50:
            # imbalance: +1 = all YES depth, -1 = all NO depth
            imbalance = (yes_near - no_near) / total_near
            # Shift fair value by up to ±10% based on near-market depth pressure
            fv = round(max(0.01, min(0.99, yes_price + imbalance * 0.10)), 4)
        else:
            fv = round(yes_price, 4)

        source = f"Book depth: YES ${yes_bid_liq:,.0f} / NO ${no_bid_liq:,.0f} → {fv*100:.0f}% implied"
        self._book_cache[cache_key] = (fv, source, now)
        return fv, source


def _fuzzy_match(team: str, question: str) -> bool:
    """Match a team name against a market question.

    Handles Polymarket's use of nicknames ("Dodgers") vs The Odds API's full
    names ("Los Angeles Dodgers") by also checking the last word of the team
    name, which is always the distinctive nickname in US sports naming.
    """
    words = team.split()
    sig_words = [w for w in words if len(w) > 2]
    if not sig_words:
        return False
    # All significant words present (e.g. "chicago bulls" fully in question)
    if all(w in question for w in sig_words):
        return True
    # Nickname only — last word of team name (e.g. "dodgers" from "los angeles dodgers")
    nickname = words[-1]
    return len(nickname) > 3 and nickname in question
