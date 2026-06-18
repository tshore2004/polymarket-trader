"""KalshiClient — read-only REST client for Kalshi prediction market data.

Requires KALSHI_API_KEY in .env for authenticated endpoints.
Without a key, all methods return empty results gracefully.

API base: https://trading-api.kalshi.com/trade-api/v2
Kalshi prices are in cents (0–99); this client converts them to 0.0–1.0.
"""
from __future__ import annotations
import logging
import threading
import time as _time
from datetime import datetime, timezone
from typing import Optional

try:
    from curl_cffi.requests import Session as CurlSession
    _HAVE_CURL = True
except ImportError:
    _HAVE_CURL = False
    import requests as _requests

from utils.models import KalshiMarket

logger = logging.getLogger(__name__)

_BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"
_RATE_LIMIT_SLEEP = 0.05   # max ~20 req/sec
_REQUEST_TIMEOUT = 10


class KalshiClient:
    """Thin REST client for reading Kalshi market data."""

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key.strip()
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._local = threading.local()

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def get_markets(self, limit: int = 200, status: str = "open") -> list[KalshiMarket]:
        """Return open Kalshi markets, up to `limit`."""
        if not self.enabled:
            return []
        try:
            params: dict = {"limit": min(limit, 200), "status": status}
            data = self._get("/markets", params)
            raw_markets = data.get("markets") or []
            return [self._parse_market(m) for m in raw_markets if m]
        except Exception as exc:
            logger.warning("Kalshi get_markets failed: %s", exc)
            return []

    def get_market(self, ticker: str) -> Optional[KalshiMarket]:
        """Return a single market by ticker."""
        if not self.enabled:
            return None
        try:
            data = self._get(f"/markets/{ticker}")
            raw = data.get("market")
            return self._parse_market(raw) if raw else None
        except Exception as exc:
            logger.debug("Kalshi get_market(%s) failed: %s", ticker, exc)
            return None

    def get_orderbook(self, ticker: str) -> dict:
        """Return raw orderbook dict with yes/no bids and asks."""
        if not self.enabled:
            return {}
        try:
            data = self._get(f"/markets/{ticker}/orderbook")
            return data.get("orderbook") or {}
        except Exception as exc:
            logger.debug("Kalshi get_orderbook(%s) failed: %s", ticker, exc)
            return {}

    def _parse_market(self, raw: dict) -> KalshiMarket:
        yes_bid = (raw.get("yes_bid") or 0) / 100.0
        yes_ask = (raw.get("yes_ask") or 0) / 100.0
        no_bid = (raw.get("no_bid") or 0) / 100.0
        no_ask = (raw.get("no_ask") or 0) / 100.0

        yes_price = (yes_bid + yes_ask) / 2.0 if (yes_bid or yes_ask) else 0.0
        no_price = (no_bid + no_ask) / 2.0 if (no_bid or no_ask) else 0.0

        if yes_price > 0 and no_price == 0:
            no_price = round(1.0 - yes_price, 4)
        elif no_price > 0 and yes_price == 0:
            yes_price = round(1.0 - no_price, 4)

        close_time: Optional[datetime] = None
        for field in ("close_time", "expiration_time", "expected_expiration_ts"):
            raw_ts = raw.get(field)
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

        return KalshiMarket(
            ticker=raw.get("ticker", ""),
            title=raw.get("title", raw.get("question", "")),
            category=raw.get("category", ""),
            yes_price=round(yes_price, 4),
            no_price=round(no_price, 4),
            volume=int(raw.get("volume", 0) or 0),
            close_time=close_time,
            tags=[raw.get("category", "")] if raw.get("category") else [],
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

    def _headers(self) -> dict:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
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
                    url, params=params, headers=self._headers(),
                    timeout=_REQUEST_TIMEOUT,
                )
                if resp.status_code == 401:
                    logger.warning("Kalshi API key unauthorized — check KALSHI_API_KEY in .env")
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
