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

        yes_price = (yes_bid + yes_ask) / 2.0 if (yes_bid or yes_ask) else 0.0
        no_price = (no_bid + no_ask) / 2.0 if (no_bid or no_ask) else 0.0

        if yes_price > 0 and no_price == 0:
            no_price = round(1.0 - yes_price, 4)
        elif no_price > 0 and yes_price == 0:
            yes_price = round(1.0 - no_price, 4)

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
            if elapsed < _RATE_LIMIT_SLEEP:
                _time.sleep(_RATE_LIMIT_SLEEP - elapsed)
            self._last_call = _time.monotonic()

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
