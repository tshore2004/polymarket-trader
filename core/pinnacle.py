"""Pinnacle sportsbook odds client.

Pinnacle is a "sharp" book — low-margin, high-limit — whose closing lines are
among the best predictors of true event probability in the industry.  Using
their lines as a fair-value benchmark surfaces genuine mispricing on Polymarket.

Credentials: set PINNACLE_USERNAME and PINNACLE_PASSWORD in .env.
API access must be enabled by Pinnacle — contact api@pinnacle.com with your
use case.  A funded Pinnacle account is required.

API docs: https://pinnacleapi.github.io/linesapi
"""
from __future__ import annotations
import logging
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

logger = logging.getLogger(__name__)

# Polymarket sport category → Pinnacle sportId
POLY_TO_PINNACLE_SPORT: dict[str, int] = {
    "nfl":       15,
    "nba":        4,
    "mlb":        3,
    "nhl":        6,
    "soccer":    29,
    "tennis":    33,
    "golf":      10,
    "ufc":        7,
    "formula-1": 62,
}


class PinnacleClient:
    """Fetches and caches sharp moneyline odds from the Pinnacle API.

    Call refresh() before each scan cycle.  get_odds(poly_sport) returns a
    flat dict of {team_name_lower: devigged_implied_probability} for matching
    against Polymarket market questions.
    """

    BASE_URL = "https://api.pinnacle.com/v1"
    CACHE_TTL = 300.0  # 5 min — Pinnacle enforces ~2 min per-endpoint rate limit

    def __init__(self, username: str, password: str) -> None:
        self._auth = (username, password)
        # poly_sport → {team_name_lower: implied_prob}
        self._odds: dict[str, dict[str, float]] = {}
        # sport_id → last "since" token for delta polling
        self._since: dict[int, int] = {}
        # sport_id → (fixtures_dict, timestamp)
        self._fixture_cache: dict[int, tuple[dict[int, dict], float]] = {}
        self._last_refresh: float = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self._auth[0] and self._auth[1])

    def refresh(self) -> None:
        """Fetch fresh odds for all configured sports. No-op if cache is warm."""
        if not self.enabled:
            return
        now = _time.monotonic()
        if now - self._last_refresh < self.CACHE_TTL:
            return
        self._last_refresh = now

        self._odds = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = {
                pool.submit(self._fetch_sport, poly_key, sport_id): poly_key
                for poly_key, sport_id in POLY_TO_PINNACLE_SPORT.items()
            }
            for fut in as_completed(futs):
                poly_key = futs[fut]
                try:
                    result = fut.result()
                    if result:
                        self._odds[poly_key] = result
                except Exception as exc:
                    logger.debug("Pinnacle: failed fetching %s: %s", poly_key, exc)

        total = sum(len(v) for v in self._odds.values())
        logger.info(
            "Pinnacle: refreshed %d sport(s), %d outcomes.",
            len(self._odds), total,
        )

    def get_odds(self, poly_sport: str) -> dict[str, float]:
        """Return {team_name_lower: devigged_probability} for a single sport."""
        return self._odds.get(poly_sport, {})

    def get_all_odds(self) -> dict[str, dict[str, float]]:
        """Return all cached odds keyed by Polymarket sport name."""
        return self._odds

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        """HTTP GET with Basic Auth. Returns parsed JSON or None on error."""
        try:
            from curl_cffi import requests as cf
            resp = cf.get(
                f"{self.BASE_URL}{path}",
                params=params or {},
                auth=self._auth,
                headers={"Accept": "application/json"},
                impersonate="chrome120",
                timeout=15,
            )
            if resp.status_code == 401:
                logger.warning(
                    "Pinnacle: 401 Unauthorized — check PINNACLE_USERNAME / PINNACLE_PASSWORD."
                )
                return None
            if resp.status_code == 403:
                logger.warning(
                    "Pinnacle: 403 Forbidden — API access not yet enabled. "
                    "Contact api@pinnacle.com to request access."
                )
                return None
            if resp.status_code == 429:
                logger.warning("Pinnacle: 429 rate limited — will retry next cycle.")
                return None
            if resp.status_code != 200:
                logger.debug("Pinnacle %s → HTTP %d", path, resp.status_code)
                return None
            return resp.json()
        except Exception as exc:
            logger.debug("Pinnacle request failed (%s): %s", path, exc)
            return None

    def _fetch_fixtures(self, sport_id: int) -> dict[int, dict]:
        """Fetch event fixtures (team names) for a sport.

        Returns {event_id: {"home": str, "away": str, "starts": str}}.
        Results are cached for CACHE_TTL seconds.
        """
        now = _time.monotonic()
        cached = self._fixture_cache.get(sport_id)
        if cached is not None:
            fixtures, ts = cached
            if now - ts < self.CACHE_TTL:
                return fixtures

        data = self._get("/fixtures", {"sportId": sport_id, "isLive": 0})
        if not data:
            return {}

        fixtures: dict[int, dict] = {}
        for league in data.get("leagues", []):
            for event in league.get("events", []):
                eid = event.get("id")
                # status "O" = open (accepting bets)
                if eid and event.get("status") == "O":
                    fixtures[eid] = {
                        "home": event.get("home", ""),
                        "away": event.get("away", ""),
                        "starts": event.get("starts", ""),
                    }

        self._fixture_cache[sport_id] = (fixtures, now)
        logger.debug("Pinnacle: %d fixtures for sportId %d.", len(fixtures), sport_id)
        return fixtures

    def _fetch_sport(self, poly_key: str, sport_id: int) -> dict[str, float]:
        """Fetch and join fixtures + odds for one sport.

        Returns {team_name_lower: devigged_implied_probability}.
        """
        fixtures = self._fetch_fixtures(sport_id)
        if not fixtures:
            return {}

        params: dict = {
            "sportId": sport_id,
            "oddsFormat": "American",
            "isLive": 0,
        }
        # Delta polling: only request changes since the last successful fetch
        since = self._since.get(sport_id)
        if since:
            params["since"] = since

        data = self._get("/odds", params)
        if not data:
            return {}

        # Store the opaque delta token for next poll
        if "last" in data:
            self._since[sport_id] = data["last"]

        result: dict[str, float] = {}
        for league in data.get("leagues", []):
            for event in league.get("events", []):
                eid = event.get("id")
                fixture = fixtures.get(eid)
                if not fixture:
                    continue

                for period in event.get("periods", []):
                    # period 0 = full-game result (what Polymarket markets resolve on)
                    if period.get("number") != 0:
                        continue
                    # status 1 = line is open
                    if period.get("status") != 1:
                        continue

                    ml = period.get("moneyline")
                    if not ml:
                        continue

                    home_ml = ml.get("home")
                    away_ml = ml.get("away")
                    draw_ml = ml.get("draw")

                    if home_ml is None or away_ml is None:
                        continue

                    raw: list[tuple[str, float]] = [
                        (fixture["home"], _american_to_prob(home_ml)),
                        (fixture["away"], _american_to_prob(away_ml)),
                    ]
                    if draw_ml is not None:
                        raw.append(("draw", _american_to_prob(draw_ml)))

                    probs = _devig([p for _, p in raw])
                    for (name, _), prob in zip(raw, probs):
                        if name:
                            result[name.lower().strip()] = round(prob, 4)

        logger.debug(
            "Pinnacle: %s (sportId %d) → %d outcomes.", poly_key, sport_id, len(result)
        )
        return result


# ── Standalone helpers ────────────────────────────────────────────────────────

def _american_to_prob(american: float) -> float:
    """Convert American moneyline odds to raw implied probability (includes vig)."""
    if american > 0:
        return 100.0 / (american + 100.0)
    return abs(american) / (abs(american) + 100.0)


def _devig(probs: list[float]) -> list[float]:
    """Remove the bookmaker's margin: normalize probabilities to sum to 1.0."""
    total = sum(probs)
    if total <= 0:
        return probs
    return [p / total for p in probs]
