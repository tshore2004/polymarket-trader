from __future__ import annotations
import json
import time
import logging
import threading
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import Optional
from curl_cffi import requests as cf_requests
from utils.models import Market, Token, OrderBook, OrderLevel, LeaderboardTrader, TraderPosition, TraderStats

logger = logging.getLogger(__name__)

_DATA_BASE = "https://data-api.polymarket.com"
_GAMMA_BASE = "https://gamma-api.polymarket.com"
_CLOB_BASE = "https://clob.polymarket.com"

_DEFAULT_TIMEOUT = 8
_RATE_LIMIT_SLEEP = 0.03   # ~33 req/sec max across all threads

# Cloudflare TLS fingerprint rotation — tried in order on curl (35) resets.
# chrome120 is listed first: it passes Cloudflare's bot checks on all Polymarket domains
# (chrome136 and chrome124 get reset on clob and data-api; chrome120 does not).
_IMPERSONATE_PROFILES = ["chrome120", "chrome136", "chrome124", "edge101"]

# Browser headers that data-api.polymarket.com's Cloudflare config checks.
# Without Origin/Referer the connection is often reset before the TLS handshake completes.
_DATA_API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://polymarket.com",
    "Referer": "https://polymarket.com/leaderboard",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
}


class PolymarketPublicClient:
    """Read-only HTTP client for public Polymarket APIs (no auth required)."""

    def __init__(self) -> None:
        # Thread-local sessions: each thread gets its own curl_cffi session per domain.
        # Sharing a single session across threads corrupts HTTP/2 connection state (curl 35).
        self._local = threading.local()
        # Global rate-limit lock — ensures at most 1 slot per _RATE_LIMIT_SLEEP seconds
        # across all threads, preventing Cloudflare from flagging burst traffic.
        self._rate_lock = threading.Lock()
        self._last_call = 0.0
        # Midpoint cache: {token_id: (timestamp, price)} — avoids redundant CLOB calls
        self._midpoint_cache: dict[str, tuple[float, float]] = {}
        self._midpoint_cache_ttl = 60.0  # seconds

    @staticmethod
    def _make_cf_session(profile: str = "chrome120", extra_headers: dict | None = None) -> cf_requests.Session:
        s = cf_requests.Session(impersonate=profile)
        s.headers.update({"Accept": "application/json"})
        if extra_headers:
            s.headers.update(extra_headers)
        return s

    def _domain_key(self, url: str) -> str:
        if _CLOB_BASE in url:
            return "clob"
        if _DATA_BASE in url:
            return "data"
        return "gamma"

    def _session_for(self, url: str) -> cf_requests.Session:
        """Get (or lazily create) the thread-local session for this domain."""
        key = self._domain_key(url)
        if not hasattr(self._local, "sessions"):
            self._local.sessions = {}
        if key not in self._local.sessions:
            extra = _DATA_API_HEADERS if key == "data" else None
            self._local.sessions[key] = self._make_cf_session(extra_headers=extra)
        return self._local.sessions[key]

    def _rotate_session_for(self, url: str) -> None:
        """Rotate to the next impersonation profile after a connection-level failure (per-thread)."""
        key = self._domain_key(url)
        if not hasattr(self._local, "sessions"):
            self._local.sessions = {}
        if not hasattr(self._local, "profile_idx"):
            self._local.profile_idx = {}
        idx = (self._local.profile_idx.get(key, 0) + 1) % len(_IMPERSONATE_PROFILES)
        self._local.profile_idx[key] = idx
        profile = _IMPERSONATE_PROFILES[idx]
        logger.debug("Rotating %s session to %s (thread: %s)", key, profile, threading.current_thread().name)
        extra = _DATA_API_HEADERS if key == "data" else None
        self._local.sessions[key] = self._make_cf_session(profile, extra_headers=extra)

    def _throttle(self) -> None:
        """Acquire one request slot — thread-safe, respects global rate limit."""
        while True:
            with self._rate_lock:
                now = time.monotonic()
                if now - self._last_call >= _RATE_LIMIT_SLEEP:
                    self._last_call = now
                    return
            time.sleep(0.01)

    def _get(self, url: str, params: dict | None = None, _retries: int = 5) -> dict | list:
        self._throttle()
        for attempt in range(_retries):
            try:
                resp = self._session_for(url).get(url, params=params, timeout=_DEFAULT_TIMEOUT)
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                exc_str = str(exc)
                is_connection_reset = "35" in exc_str or "10054" in exc_str or "reset" in exc_str.lower()
                is_timeout = "(28)" in exc_str or "timed out" in exc_str.lower()
                if is_connection_reset:
                    self._rotate_session_for(url)
                    if attempt < _retries - 1:
                        # Jitter prevents all parallel workers waking up simultaneously
                        # and triggering another Cloudflare burst-detection reset.
                        wait = 1.5 * (attempt + 1) + random.uniform(0.0, 0.8)
                        logger.debug("curl(35)/WinError 10054 on %s — retrying in %.1fs (attempt %d/%d)",
                                     url, wait, attempt + 1, _retries)
                        time.sleep(wait)
                        continue
                elif is_timeout and attempt < _retries - 1:
                    wait = 0.5 * (attempt + 1) + random.uniform(0.0, 0.3)
                    logger.debug("curl(28) timeout on %s — retrying in %.1fs (attempt %d/%d)",
                                 url, wait, attempt + 1, _retries)
                    time.sleep(wait)
                    continue
                # 404 means the resource doesn't exist (resolved market, missing token, etc.).
                # Callers already handle None/empty responses gracefully — no WARNING needed.
                is_not_found = "404" in exc_str
                if is_not_found:
                    logger.debug("404 not found %s %s", url, params)
                else:
                    logger.warning("API error %s %s: %s", url, params, exc)
                raise

    # ── Markets ──────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_market_dict(m: dict) -> Optional[Market]:
        condition_id = m.get("conditionId", "")
        question = m.get("question", "")
        if not condition_id or not question:
            return None

        token_ids_raw = m.get("clobTokenIds", [])
        if isinstance(token_ids_raw, str):
            try:
                token_ids = json.loads(token_ids_raw)
            except (json.JSONDecodeError, ValueError):
                token_ids = []
        else:
            token_ids = list(token_ids_raw)

        outcomes_raw = m.get("outcomes", "[]")
        if isinstance(outcomes_raw, str):
            try:
                outcomes = json.loads(outcomes_raw)
            except (json.JSONDecodeError, ValueError):
                outcomes = []
        else:
            outcomes = list(outcomes_raw)

        if len(token_ids) < 2:
            return None

        tokens = []
        for i, tid in enumerate(token_ids[:2]):
            outcome = outcomes[i] if i < len(outcomes) else ("Yes" if i == 0 else "No")
            tokens.append(Token(token_id=str(tid), outcome=str(outcome)))

        # Parse resolution date
        end_date: Optional[datetime] = None
        for _key in ("endDate", "endDateIso", "end_date_iso", "endDatetime"):
            raw_date = m.get(_key)
            if raw_date:
                try:
                    end_date = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
                except Exception:
                    pass
                else:
                    break

        # Event grouping slug — shared across markets in the same event category
        event_slug = ""
        for _key in ("eventSlug", "event_slug", "parentEventSlug"):
            val = m.get(_key)
            if val:
                event_slug = str(val)[:80]
                break

        # Tags — list of dicts with "slug", "label", or "name" keys
        tags_raw = m.get("tags", [])
        if isinstance(tags_raw, list):
            tags = []
            for t in tags_raw:
                if isinstance(t, dict):
                    slug = t.get("slug") or t.get("label") or t.get("name") or ""
                    if slug:
                        tags.append(str(slug).lower())
                elif isinstance(t, str) and t:
                    tags.append(t.lower())
        else:
            tags = []

        return Market(
            condition_id=condition_id,
            question=question,
            tokens=tokens,
            active=bool(m.get("active", True)),
            closed=bool(m.get("closed", False)),
            volume=float(m.get("volume", 0) or 0),
            end_date=end_date,
            event_slug=event_slug,
            tags=tags,
        )

    def get_markets(
        self,
        limit: int = 100,
        active_only: bool = True,
        tags_filter: list[str] | None = None,
        order: str | None = None,
        ascending: bool = True,
        end_date_min: str | None = None,
        end_date_max: str | None = None,
        liquidity_min: float | None = None,
    ) -> list[Market]:
        """Fetch active binary markets from the Gamma API (offset-paginated).

        tags_filter: if provided, only return markets whose tags overlap this set
        (case-insensitive). Pass None or empty list to return all categories.

        order/ascending/end_date_min/end_date_max/liquidity_min are best-effort
        server-side hints (Gamma supports them, but we never depend on them for
        correctness — any date window is re-checked client-side by the caller).
        """
        markets: list[Market] = []
        offset = 0
        page_size = min(limit, 100)

        while len(markets) < limit:
            params: dict = {
                "active": "true",
                "closed": "false",
                "limit": page_size,
                "offset": offset,
            }
            if order:
                params["order"] = order
                params["ascending"] = "true" if ascending else "false"
            if end_date_min:
                params["end_date_min"] = end_date_min
            if end_date_max:
                params["end_date_max"] = end_date_max
            if liquidity_min is not None:
                params["liquidity_num_min"] = liquidity_min
            try:
                data = self._get(f"{_GAMMA_BASE}/markets", params)
            except Exception:
                break

            raw = data if isinstance(data, list) else data.get("data", [])
            if not raw:
                break

            for m in raw:
                parsed = self._parse_market_dict(m)
                if parsed:
                    markets.append(parsed)

            offset += len(raw)
            if len(raw) < page_size:
                break

        if active_only:
            markets = [m for m in markets if m.active and not m.closed]

        if tags_filter:
            filter_set = {t.lower() for t in tags_filter}
            markets = [m for m in markets if any(tag in filter_set for tag in m.tags)]

        return markets[:limit]

    def get_near_term_markets(
        self,
        hours: int = 48,
        limit: int = 600,
        tags_filter: list[str] | None = None,
    ) -> list[Market]:
        """Fetch markets resolving within the next `hours`, soonest first.

        This is what surfaces *tonight's* sports games and other short-fuse events,
        which a generic unordered market scan often never reaches. The Gamma date
        window is a hint; the returned list is filtered client-side by end_date so
        the window is always honoured even if the server ignores the params.
        """
        now = datetime.now(timezone.utc)
        window_end = now + timedelta(hours=hours)
        markets = self.get_markets(
            limit=limit,
            tags_filter=tags_filter,
            order="endDate",
            ascending=True,
            end_date_min=now.isoformat(),
            end_date_max=window_end.isoformat(),
        )
        result = []
        for m in markets:
            if m.end_date is None:
                continue
            ed = m.end_date
            if ed.tzinfo is None:
                ed = ed.replace(tzinfo=timezone.utc)
            secs = (ed - now).total_seconds()
            if 0 < secs <= hours * 3600:
                result.append(m)
        result.sort(key=lambda mm: mm.end_date or now)
        return result

    def get_markets_by_condition_ids(self, condition_ids: list[str]) -> list[Market]:
        """Fetch markets by condition ID — used to resolve trader positions not in the main market list.

        Fetches all provided IDs in parallel (5 workers). No cap on input length.
        """
        if not condition_ids:
            return []

        def _fetch_one(cid: str) -> "Market | None":
            try:
                data = self._get(f"{_GAMMA_BASE}/markets", {"condition_ids": cid})
                raw = data if isinstance(data, list) else data.get("data", [])
                for m_raw in raw:
                    parsed = self._parse_market_dict(m_raw)
                    if parsed and parsed.condition_id == cid:
                        return parsed
            except Exception as exc:
                logger.debug("Failed to fetch market %s: %s", cid, exc)
            return None

        results: list[Market] = []
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_fetch_one, cid): cid for cid in condition_ids}
            for fut in as_completed(futures):
                m = fut.result()
                if m is not None:
                    results.append(m)
        return results

    def get_orderbook(self, token_id: str) -> OrderBook:
        """Fetch the live orderbook for a single token."""
        try:
            data = self._get(f"{_CLOB_BASE}/book", {"token_id": token_id})
        except Exception:
            return OrderBook(token_id=token_id)

        def parse_levels(raw: list) -> list[OrderLevel]:
            levels = []
            for entry in raw:
                try:
                    levels.append(OrderLevel(price=float(entry["price"]), size=float(entry["size"])))
                except (KeyError, ValueError):
                    pass
            return levels

        bids = sorted(parse_levels(data.get("bids", [])), key=lambda x: -x.price)
        asks = sorted(parse_levels(data.get("asks", [])), key=lambda x: x.price)
        return OrderBook(token_id=token_id, bids=bids, asks=asks)

    def get_midpoint(self, token_id: str) -> Optional[float]:
        if not token_id:
            return None
        # Check cache first
        cached = self._midpoint_cache.get(token_id)
        if cached:
            ts, price = cached
            if time.monotonic() - ts < self._midpoint_cache_ttl:
                return price
        try:
            data = self._get(f"{_CLOB_BASE}/midpoint", {"token_id": token_id})
            price = float(data["mid"])
            self._midpoint_cache[token_id] = (time.monotonic(), price)
            return price
        except Exception:
            return None

    # ── Leaderboard ──────────────────────────────────────────────────────────

    def get_leaderboard(
        self,
        window: str = "1m",
        limit: int = 50,
        min_profit: float = 0,
        min_volume: float = 0,
    ) -> list[LeaderboardTrader]:
        data = self._fetch_leaderboard_raw(window, limit)
        if data is None:
            return []

        traders = []
        for row in (data if isinstance(data, list) else data.get("data", [])):
            # v1 API: pnl/vol; legacy API: profit/volume
            profit = float(row.get("pnl", row.get("profit", 0)) or 0)
            volume = float(row.get("vol", row.get("volume", 0)) or 0)
            if profit < min_profit or volume < min_volume:
                continue
            traders.append(
                LeaderboardTrader(
                    address=row.get("proxyWallet", row.get("proxyAddress", row.get("address", ""))),
                    name=row.get("userName", row.get("name", row.get("pseudonym", ""))) or "",
                    profit=profit,
                    volume=volume,
                    num_trades=int(row.get("numTrades", row.get("trades", 0)) or 0),
                    pct_positive=float(row.get("percentPositive", 0) or 0),
                )
            )

        # Normalize scores: reward CONSISTENT winners, not one-hit wonders.
        #
        # Components:
        #   - profit_norm (25%): raw profit, but dampened by consistency factor
        #   - volume_norm (15%): shows active participation
        #   - win_rate    (30%): pct_positive is the strongest consistency signal
        #   - consistency (30%): log-scaled trade count — need ~50 trades to be "proven"
        #
        # A trader with 3 trades and $80k profit scores much lower than one with
        # 200 trades, 63% win rate, and $15k profit.
        if traders:
            import math
            max_profit = max(t.profit for t in traders) or 1.0
            max_vol = max(t.volume for t in traders) or 1.0
            for t in traders:
                profit_norm = t.profit / max_profit
                volume_norm = t.volume / max_vol
                # Win rate: API returns 0–1. Default to 0.5 only if genuinely no data.
                # Note: at this stage closed_positions hasn't been enriched yet,
                # so we use the raw pct_positive from the leaderboard API.
                win_rate = t.pct_positive if t.pct_positive > 0 else 0.5
                # Consistency: log-scaled trade count. 50 trades → ~1.0, 5 trades → ~0.41
                consistency = min(1.0, math.log10(1 + t.num_trades) / math.log10(51))
                # Profit is only meaningful when backed by enough trades
                t.score = (
                    0.25 * profit_norm * consistency
                    + 0.15 * volume_norm
                    + 0.30 * win_rate
                    + 0.30 * consistency
                )

        return sorted(traders, key=lambda t: t.score, reverse=True)

    @staticmethod
    def _window_to_time_period(window: str) -> str:
        """Map legacy window strings to the v1 API timePeriod values."""
        mapping = {"1d": "DAY", "1w": "WEEK", "1m": "MONTH", "all": "ALL"}
        return mapping.get(window.lower(), "MONTH")

    def _fetch_leaderboard_raw(self, window: str, limit: int) -> list | dict | None:
        """Fetch leaderboard from the v1 endpoint; fall back to legacy path on failure.

        data-api.polymarket.com uses stricter Cloudflare bot detection than gamma-api.
        Sessions for this domain include Origin/Referer headers (see _DATA_API_HEADERS)
        and we cycle through all curl_cffi profiles before giving up.
        """
        time_period = self._window_to_time_period(window)
        endpoints = [
            (f"{_DATA_BASE}/v1/leaderboard", {"timePeriod": time_period, "orderBy": "PNL", "limit": min(limit, 50), "offset": 0}),
            # Fallback: non-versioned path has lighter Cloudflare bot detection
            (f"{_DATA_BASE}/leaderboard", {"timePeriod": time_period, "orderBy": "PNL", "limit": min(limit, 50), "offset": 0}),
        ]
        # Enough retries to try every impersonation profile at least once.
        retries_per_endpoint = len(_IMPERSONATE_PROFILES) + 1
        for url, params in endpoints:
            try:
                return self._get(url, params, _retries=retries_per_endpoint)
            except Exception as exc:
                logger.debug("Leaderboard attempt %s failed: %s", url, exc)

        logger.warning(
            "All leaderboard endpoints unavailable — leaderboard signals disabled this cycle."
        )
        return None

    def get_trader_stats(self, address: str) -> TraderStats:
        """Fetch aggregate trader stats from v1/user-stats + activity/positions.

        Combines three endpoints:
        - v1/user-stats: prediction count, largest win, join date
        - activity (all types): wins (REDEEM), buys, sells for PnL chart
        - positions (curPrice=0): losing markets (resolved losers)

        Win rate = unique_redeemed_markets / (unique_redeemed + unique_lost).
        Raw activity rows are saved in stats.activity_cache for the profile chart.
        """
        stats = TraderStats(address=address)

        # 1) Aggregate stats from v1/user-stats
        try:
            data = self._get(
                f"{_DATA_BASE}/v1/user-stats",
                {"proxyAddress": address},
            )
            if isinstance(data, dict):
                stats.predictions = int(data.get("trades", 0) or 0)
                stats.largest_win = float(data.get("largestWin", 0) or 0)
                join_raw = data.get("joinDate")
                if join_raw and join_raw != "":
                    try:
                        stats.join_date = datetime.fromisoformat(
                            str(join_raw).replace("Z", "+00:00")
                        )
                    except Exception:
                        pass
        except Exception as exc:
            logger.debug("user-stats fetch failed for %s: %s", address, exc)

        # 2) Fetch ALL activity (no type filter) — gets REDEEM, BUY, SELL, etc.
        all_activity: list[dict] = []
        win_markets: set[str] = set()
        try:
            data = self._get(
                f"{_DATA_BASE}/activity",
                {"user": address, "limit": 500},
            )
            all_activity = data if isinstance(data, list) else data.get("data", [])
        except Exception as exc:
            logger.debug("All-activity fetch failed for %s: %s", address, exc)

        # If all-activity failed, try REDEEM-only (more reliable, less Cloudflare scrutiny)
        if not all_activity:
            try:
                data = self._get(
                    f"{_DATA_BASE}/activity",
                    {"user": address, "limit": 500, "type": "REDEEM"},
                )
                all_activity = data if isinstance(data, list) else data.get("data", [])
            except Exception as exc:
                logger.debug("REDEEM activity fetch failed for %s: %s", address, exc)

        # Parse activity: extract wins and build chart cache
        # Activity API returns type=TRADE with side=BUY/SELL (not type=BUY/SELL).
        # Other types: REDEEM, SPLIT, MERGE, REWARD, CONVERSION.
        for row in all_activity:
            row_type = str(row.get("type", "")).upper()
            row_side = str(row.get("side", "")).upper()
            cid = row.get("conditionId")
            usdc = float(row.get("usdcSize", 0) or 0)

            if row_type == "REDEEM" and cid:
                win_markets.add(cid)
                stats.total_closed_pnl += usdc

            # Cache for profile chart — include side so the JS chart can
            # distinguish BUY vs SELL within TRADE rows.
            ts_raw = row.get("timestamp") or row.get("createdAt")
            if ts_raw:
                # Activity API returns timestamp as unix seconds (int),
                # not ISO string. Handle both formats.
                try:
                    if isinstance(ts_raw, (int, float)):
                        ts = datetime.fromtimestamp(ts_raw, tz=timezone.utc)
                    else:
                        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                    stats.activity_cache.append({
                        "timestamp": ts.isoformat(),
                        "type": row_type or "UNKNOWN",
                        "side": row_side,  # BUY or SELL (only set on TRADE rows)
                        "conditionId": cid or "",
                        "usdcSize": usdc,
                        "title": str(row.get("title", "") or ""),
                        "outcome": str(row.get("outcome", "") or ""),
                    })
                except Exception:
                    pass

        stats.winning_positions = len(win_markets)
        # Sort activity chronologically for