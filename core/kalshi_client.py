"""KalshiClient — read-only REST client for Kalshi prediction market data.

Reading market/event data is PUBLIC and requires no credentials. The optional
KALSHI_API_KEY (key ID) + KALSHI_API_SECRET (RSA private key PEM) are only used
to sign requests for authenticated endpoints (portfolio/trading, reserved for
future use); when present they are attached, when absent reads still work.

API base: https://api.elections.kalshi.com/trade-api/v2
Prices are returned as dollar strings ("0.1600" = 0.16). Market listings come
from the /events endpoint (with nested markets) because the flat /markets
listing is dominated by zero-volume multi-leg "KXMVE" collection markets.
"""
from __future__ import annotations
import base64
import logging
import re
import threading
import time as _time
from datetime import datetime, timezone
from typing import Optional

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding as _asym_padding
    _HAVE_CRYPTO = True
except ImportError:
    _HAVE_CRYPTO = False

try:
    from curl_cffi.requests import Session as CurlSession
    _HAVE_CURL = True
except ImportError:
    _HAVE_CURL = False
    import requests as _requests

from utils.models import KalshiMarket

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
_RATE_LIMIT_SLEEP = 0.05   # max ~20 req/sec
_REQUEST_TIMEOUT = 10
_MAX_EVENT_PAGES = 20      # cap paging through the events listing

# Sports game series tickers confirmed to have daily game markets.
# Dynamically extended at runtime via get_sports_series().
_TIER1_GAME_SERIES = [
    "KXMLBGAME",       # MLB (Baseball)
    "KXNBAGAME",       # NBA (Basketball)
    "KXWNBAGAME",      # WNBA
    "KXNFLGAME",       # NFL (Football)
    "KXNHLGAME",       # NHL (Hockey)
    "KXMLS",           # MLS (Soccer)
    "KXUCLGAME",       # UEFA Champions League
    "KXBUNDESLIGAGAME",# Bundesliga
    "KXSERIEAGAME",    # Serie A
    "KXLIGAMXGAME",    # Liga MX
    "KXATPGWINNER",    # ATP Tennis
    "KXWTAGAME",       # WTA Tennis
    "KXNCAAMBGAME",    # Men's College Basketball
    "KXFIFAGAME",      # FIFA (catch-all)
    "KXWSCGAME",       # World Soccer Cup game
    "KXFIFAWC",        # FIFA World Cup 2026
    "KXWC2026",        # World Cup 2026 (alternate ticker)
    "KXWORLDCUP",      # World Cup (alternate ticker)
    "KXPLLGAME",       # PLL (Lacrosse)
    "KXWNBAGAMESPLAYED", # WNBA
    "KXCFLGAME",       # CFL
    "KXAFLGAME",       # AFL
    "KXIIHFGAME",      # IIHF Hockey
]


def _expand_team_abbrev(sub: str, event_title: str) -> str:
    """Resolve 'City X' abbreviated team labels to full names using the event title.

    Kalshi abbreviates team names to 'City X' (single letter suffix), e.g.
    'Los Angeles A' for Angels, 'New York Y' for Yankees.  The full name is
    always present in the parent event title, so we extract it from there
    rather than maintaining a static lookup table.
    """
    if not sub or not event_title:
        return sub
    words = sub.split()
    if len(words) < 2 or len(words[-1]) != 1:
        return sub  # not an abbreviated pattern

    first_letter = words[-1].lower()
    prefix = " ".join(words[:-1]).lower()  # e.g. "los angeles"

    sides = re.split(r"\s+(?:vs\.?|[–—])\s+", event_title, maxsplit=1)
    for side in sides:
        side_stripped = side.strip()
        if side_stripped.lower().startswith(prefix):
            remainder = side_stripped.lower()[len(prefix):].strip()
            if remainder and remainder[0] == first_letter:
                clean = re.sub(r"\s*[-–—].*$", "", side_stripped).rstrip("?").strip()
                if clean:
                    return clean
    return sub


def _f(v) -> float:
    """Parse Kalshi numeric fields, which arrive as strings like '0.1600'."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class KalshiClient:
    """Thin REST client for reading Kalshi market data (no auth required)."""

    def __init__(self, api_key: str = "", api_secret: str = "") -> None:
        self._key_id = api_key.strip()
        # Normalize private key: handle literal \n from .env and strip whitespace
        pem = api_secret.strip().replace("\\n", "\n")
        self._private_key = None
        if pem and _HAVE_CRYPTO:
            try:
                self._private_key = serialization.load_pem_private_key(
                    pem.encode(), password=None
                )
            except Exception as exc:
                logger.warning("Kalshi: failed to load private key — %s", exc)
        elif pem and not _HAVE_CRYPTO:
            logger.warning("Kalshi: cryptography package not installed — pip install cryptography")
        if self._private_key:
            logger.info("Kalshi: RSA key loaded OK (key_id=%s)", self._key_id[:8] + "...")
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._local = threading.local()

    @property
    def enabled(self) -> bool:
        """True when signing credentials are present. Not required to read data."""
        return bool(self._key_id and self._private_key)

    def get_markets(self, limit: int = 500, status: str = "open") -> list[KalshiMarket]:
        """Return real, priced Kalshi markets, up to `limit`.

        Pulls from /events?with_nested_markets=true so each market carries its
        parent event's category/title, and skips zero-price collection markets.
        Requires no credentials.
        """
        out: list[KalshiMarket] = []
        cursor = ""
        try:
            for _ in range(_MAX_EVENT_PAGES):
                params = {
                    "limit": 200,
                    "status": status,
                    "with_nested_markets": "true",
                }
                if cursor:
                    params["cursor"] = cursor
                data = self._get("/events", params)
                events = data.get("events") or []
                for ev in events:
                    for raw in ev.get("markets") or []:
                        km = self._parse_market(raw, ev)
                        if km is not None:
                            out.append(km)
                            if len(out) >= limit:
                                return out
                cursor = data.get("cursor") or ""
                if not cursor:
                    break
        except Exception as exc:
            logger.warning("Kalshi get_markets failed: %s", exc)
        return out

    def get_sports_series(self) -> list[str]:
        """Return series tickers for game-level sports series (title contains 'Game').

        Queries /series?category=Sports and filters for titles that include 'Game',
        which identifies per-game binary markets (MLB, NBA, soccer leagues, etc.).
        Results include the tier-1 list as a fallback in case the endpoint is slow.
        """
        try:
            # Fetch enough to cover most game series — /series supports large limits
            data = self._get("/series", {"category": "Sports", "limit": 2500})
            all_series = data.get("series") or []
            # "football" removed — it matches NFL/CFL/college series, not just soccer.
            # Replaced with unambiguous soccer identifiers.
            _SOCCER_KEYWORDS = {"soccer", "world cup", "copa", "fifa", "mls", "liga",
                                 "bundesliga", "serie a", "premier", "fc", "futbol"}
            _AMERICAN_FOOTBALL_DENY = {"nfl", "ncaa football", "college football",
                                       "american football", "cfl", "afl"}
            game_tickers = [
                s["ticker"] for s in all_series
                if s.get("ticker") and (
                    "game" in (s.get("title") or "").lower()
                    or (
                        any(kw in (s.get("title") or "").lower() for kw in _SOCCER_KEYWORDS)
                        and not any(deny in (s.get("title") or "").lower()
                                    for deny in _AMERICAN_FOOTBALL_DENY)
                    )
                )
            ]
            if game_tickers:
                merged = list(dict.fromkeys(list(_TIER1_GAME_SERIES) + game_tickers))
                logger.info("Kalshi sports series: %d active (of %d total). Soccer/FIFA tickers: %s",
                            len(merged), len(all_series),
                            [t for t in merged if any(kw in t.lower() for kw in ("fifa", "wc", "mls", "ucl", "bundesliga", "serie", "liga", "wsc", "soccer"))])
                return merged
        except Exception as exc:
            logger.warning("Kalshi get_sports_series failed: %s", exc)
        return list(_TIER1_GAME_SERIES)

    def get_sports_game_markets(self, days_ahead: float = 7.0, max_series: int = 80) -> list[KalshiMarket]:
        """Return today's/this-week's game-level sports markets from all active series.

        Discovers game series dynamically, then fetches events for each in parallel
        (10 workers). Only returns markets whose close_time is within `days_ahead` days.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        series_tickers = self.get_sports_series()[:max_series]
        logger.info("Kalshi sports: fetching events from %d game series", len(series_tickers))

        cutoff = datetime.now(timezone.utc).timestamp() + days_ahead * 86_400

        def _fetch_series(ticker: str) -> list[KalshiMarket]:
            out: list[KalshiMarket] = []
            try:
                data = self._get("/events", {
                    "series_ticker": ticker,
                    "status": "open",
                    "limit": 50,
                    "with_nested_markets": "true",
                })
                for ev in data.get("events") or []:
                    # Only include near-term events
                    close_raw = ev.get("close_time") or ""
                    if close_raw:
                        try:
                            ev_close = datetime.fromisoformat(close_raw.replace("Z", "+00:00"))
                            if ev_close.timestamp() > cutoff:
                                continue
                        except Exception:
                            pass
                    for raw in ev.get("markets") or []:
                        km = self._parse_market(raw, ev)
                        if km is not None:
                            out.append(km)
            except Exception as exc:
                logger.debug("Kalshi sports series %s failed: %s", ticker, exc)
            return out

        results: list[KalshiMarket] = []
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_fetch_series, t): t for t in series_tickers}
            for fut in as_completed(futures):
                results.extend(fut.result())

        logger.info("Kalshi sports: fetched %d game markets across %d series",
                    len(results), len(series_tickers))
        return results

    def get_market(self, ticker: str) -> Optional[KalshiMarket]:
        """Return a single market by ticker (no event context)."""
        try:
            data = self._get(f"/markets/{ticker}")
            raw = data.get("market")
            return self._parse_market(raw, {}) if raw else None
        except Exception as exc:
            logger.debug("Kalshi get_market(%s) failed: %s", ticker, exc)
            return None

    def get_orderbook(self, ticker: str) -> dict:
        """Return raw orderbook dict with yes/no bids and asks."""
        try:
            data = self._get(f"/markets/{ticker}/orderbook")
            return data.get("orderbook") or {}
        except Exception as exc:
            logger.debug("Kalshi get_orderbook(%s) failed: %s", ticker, exc)
            return {}

    def _parse_market(self, raw: dict, event: dict) -> Optional[KalshiMarket]:
        if not raw:
            return None
        ticker = raw.get("ticker", "")
        # Skip multi-leg collection markets — they are noise with no real price.
        if ticker.startswith("KXMVE"):
            return None

        yes_bid = _f(raw.get("yes_bid_dollars"))
        yes_ask = _f(raw.get("yes_ask_dollars"))
        no_bid = _f(raw.get("no_bid_dollars"))
        no_ask = _f(raw.get("no_ask_dollars"))

        # Use ask prices — these are what you actually pay to enter a position,
        # matching what Kalshi's UI shows and giving accurate arb cost calculations.
        # NO ask = 1 - YES bid when Kalshi doesn't return no_ask directly.
        yes_price = yes_ask if yes_ask > 0 else (yes_bid if yes_bid > 0 else 0.0)
        no_price = no_ask if no_ask > 0 else (no_bid if no_bid > 0 else 0.0)

        if yes_price > 0 and no_price == 0:
            # NO ask = complement of YES bid (best available price to buy NO)
            no_price = round(1.0 - yes_bid, 4) if yes_bid > 0 else round(1.0 - yes_price, 4)
        elif no_price > 0 and yes_price == 0:
            yes_price = round(1.0 - no_bid, 4) if no_bid > 0 else round(1.0 - no_price, 4)

        # Drop markets with no tradable price at all.
        if yes_price <= 0 and no_price <= 0:
            return None

        # Build a human-readable, matchable title from the event question +
        # this market's outcome label (e.g. "Who will the next Pope be? — Parolin").
        event_title = (event or {}).get("title", "")
        market_title = raw.get("title", "")
        base_title = event_title or market_title
        sub = (raw.get("yes_sub_title") or "").strip()
        title = base_title
        if sub and sub.lower() not in base_title.lower() and sub.lower() != "mars":
            title = f"{base_title} — {sub}" if base_title else sub
        if not title:
            title = ticker

        category = (event or {}).get("category", "") or raw.get("category", "")

        # Expand abbreviated YES-side team labels ("Los Angeles A" → "Los Angeles Angels")
        # using the full team name already present in the event title.
        sub = _expand_team_abbrev(sub, base_title)

        # Derive the opposing team name (NO side) from the event title.
        no_sub = ""
        if sub and base_title:
            _sides = re.split(r"\s+(?:vs\.?|[–—])\s+", base_title, maxsplit=1)
            if len(_sides) == 2:
                for _side in _sides:
                    _clean = re.sub(r"\s*[-–—].*$", "", _side.strip()).rstrip("?").strip()
                    if _clean and sub.lower() not in _clean.lower():
                        no_sub = _clean
                        break

        close_time: Optional[datetime] = None
        for fld in ("close_time", "expiration_time", "expected_expiration_time"):
            raw_ts = raw.get(fld)
            if raw_ts:
                try:
                    if isinstance(raw_ts, (int, float)):
                        close_time = datetime.fromtimestamp(raw_ts, tz=timezone.utc)
                    else:
                        close_time = datetime.fromisoformat(
                            str(raw_ts).replace("Z", "+00:00")
                        )
                    break
                except Exception:
                    continue

        # For sports game markets, close_time is often on the parent event only.
        # Fall back to the event-level field so urgency scoring works correctly.
        if close_time is None:
            for fld in ("close_time", "expiration_time"):
                raw_ts = (event or {}).get(fld)
                if raw_ts:
                    try:
                        if isinstance(raw_ts, (int, float)):
                            close_time = datetime.fromtimestamp(raw_ts, tz=timezone.utc)
                        else:
                            close_time = datetime.fromisoformat(
                                str(raw_ts).replace("Z", "+00:00")
                            )
                        break
                    except Exception:
                        continue

        volume = _f(raw.get("volume_fp") or raw.get("volume_24h_fp") or 0)

        return KalshiMarket(
            ticker=ticker,
            title=title,
            category=category,
            yes_price=round(yes_price, 4),
            no_price=round(no_price, 4),
            volume=volume,
            close_time=close_time,
            tags=[category] if category else [],
            yes_sub_title=sub,
            no_sub_title=no_sub,
        )

    def _session(self) -> object:
        sess = getattr(self._local, "session", None)
        if sess is None:
            if _HAVE_CURL:
                sess = CurlSession(impersonate="chrome120")
            else:
                sess = _requests.Session()
            self._local.session = sess
        return sess

    def _sign_headers(self, method: str, path: str) -> dict:
        """Generate RSA-signed Kalshi auth headers when credentials are present."""
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if not self._private_key or not self._key_id:
            return h
        ts = str(int(_time.time() * 1000))
        full_path = "/trade-api/v2" + path
        message = (ts + method.upper() + full_path).encode()
        sig = self._private_key.sign(
            message,
            _asym_padding.PSS(
                mgf=_asym_padding.MGF1(hashes.SHA256()),
                salt_length=_asym_padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        h["KALSHI-ACCESS-KEY"] = self._key_id
        h["KALSHI-ACCESS-TIMESTAMP"] = ts
        h["KALSHI-ACCESS-SIGNATURE"] = base64.b64encode(sig).decode()
        return h

    def _get(self, path: str, params: dict | None = None, retries: int = 3) -> dict:
        url = _BASE_URL + path
        with self._lock:
            elapsed = _time.monotonic() - self._last_call
            sleep_for = max(0.0, _RATE_LIMIT_SLEEP - elapsed)
            self._last_call = _time.monotonic() + sleep_for  # reserve slot atomically
        if sleep_for > 0:
            _time.sleep(sleep_for)  # sleep outside lock so other threads can reserve concurrently

        session = self._session()
        for attempt in range(retries):
            try:
                resp = session.get(
                    url, params=params, headers=self._sign_headers("GET", path),
                    timeout=_REQUEST_TIMEOUT,
                )
                if resp.status_code == 401:
                    logger.warning("Kalshi auth rejected (only affects private endpoints): %s", resp.text[:200])
                    return {}
                if resp.status_code == 404:
                    logger.debug("Kalshi 404: %s", url)
                    return {}
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                if attempt < retries - 1:
                    backoff = 0.5 * (attempt + 1)
                    logger.debug("Kalshi request failed (attempt %d): %s", attempt + 1, exc)
                    _time.sleep(backoff)
                else:
                    raise
        return {}
