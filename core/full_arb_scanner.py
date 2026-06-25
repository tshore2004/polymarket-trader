"""FullArbScanner — background scanner that scans ALL open Poly + Kalshi markets for arb.

Unlike the signal-based arb check in server.py, this scanner fetches the full
universe of open markets on both platforms and runs CrossPlatformArbScanner on them.
Results are cached in a snapshot and refreshed every arb_scan_interval seconds.
"""
from __future__ import annotations
import dataclasses
import logging
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed2
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from config import Config
from core.api_client import PolymarketPublicClient
from core.arbitrage import CrossPlatformArbScanner
from core.kalshi_client import KalshiClient
from utils.models import ArbitrageOpportunity

logger = logging.getLogger(__name__)


class ArbScanState(str, Enum):
    IDLE = "idle"
    SCANNING = "scanning"
    ERROR = "error"


@dataclass
class ArbSnapshot:
    state: ArbScanState = ArbScanState.IDLE
    opportunities: list[ArbitrageOpportunity] = field(default_factory=list)
    poly_count: int = 0
    kalshi_count: int = 0
    last_scan_at: Optional[datetime] = None
    scan_duration_s: Optional[float] = None
    error: Optional[str] = None


class FullArbScanner:
    """Periodically fetches all open markets on both platforms and finds arb opportunities."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._poly_client = PolymarketPublicClient()
        self._kalshi_client = KalshiClient(config.kalshi_api_key, config.kalshi_api_secret)
        self._arb_scanner = CrossPlatformArbScanner(config)
        self._snapshot = ArbSnapshot()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._scan_now = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="FullArbScanner")
        self._thread.start()
        logger.info("FullArbScanner started (interval=%ds)", self._config.arb_scan_interval)

    def stop(self) -> None:
        self._stop_event.set()
        self._scan_now.set()
        if self._thread:
            self._thread.join(timeout=5)

    def trigger_scan(self) -> bool:
        with self._lock:
            if self._snapshot.state == ArbScanState.SCANNING:
                return False
        self._scan_now.set()
        return True

    def get_snapshot(self) -> ArbSnapshot:
        with self._lock:
            return self._snapshot

    def _loop(self) -> None:
        self._scan_now.set()
        while not self._stop_event.is_set():
            self._scan_now.wait(timeout=self._config.arb_scan_interval)
            if self._stop_event.is_set():
                break
            self._scan_now.clear()
            self._run_scan()

    def _run_scan(self) -> None:
        with self._lock:
            self._snapshot = ArbSnapshot(
                state=ArbScanState.SCANNING,
                opportunities=self._snapshot.opportunities,
                poly_count=self._snapshot.poly_count,
                kalshi_count=self._snapshot.kalshi_count,
                last_scan_at=self._snapshot.last_scan_at,
            )
        t0 = _time.monotonic()
        try:
            logger.info("FullArbScanner: fetching all open markets...")
            with ThreadPoolExecutor(max_workers=4) as pool:
                # Sort by 24h volume so high-activity sports markets (MLB, World Cup, etc.)
                # always appear in the first page rather than being buried by offset ordering.
                poly_fut = pool.submit(
                    self._poly_client.get_markets, 2000, True, None, "volume24hr", False
                )
                # Use a wide window (336h = 2 weeks) because sports moneylines on Polymarket
                # carry settlement end dates up to 1-2 weeks out, not the actual game time.
                # Without this, a 72h window misses tomorrow's game moneylines entirely.
                poly_near_fut = pool.submit(self._poly_client.get_near_term_markets, 336, 500)
                kalshi_fut = pool.submit(self._kalshi_client.get_markets, 500)
                # days_ahead=14 — matches the expanded Poly window above; Kalshi events with
                # no close_time already pass the cutoff, so this is a no-op for MLB/soccer.
                sports_fut = pool.submit(self._kalshi_client.get_sports_game_markets, 14.0, 80)
                poly_markets = poly_fut.result()
                poly_near = poly_near_fut.result()
                kalshi_base = kalshi_fut.result()
                kalshi_sports = sports_fut.result()

            seen_poly: set[str] = {m.condition_id for m in poly_markets}
            for pm in poly_near:
                if pm.condition_id not in seen_poly:
                    poly_markets.append(pm)
                    seen_poly.add(pm.condition_id)

            seen_tickers: set[str] = {m.ticker for m in kalshi_base}
            for km in kalshi_sports:
                if km.ticker not in seen_tickers:
                    kalshi_base.append(km)
                    seen_tickers.add(km.ticker)
            kalshi_markets = kalshi_base

            logger.info(
                "FullArbScanner: poly=%d (incl %d near-term) kalshi=%d (incl %d sports) — running arb scan",
                len(poly_markets), len(poly_near), len(kalshi_markets), len(kalshi_sports),
            )
            opportunities = self._arb_scanner.find_opportunities(poly_markets, kalshi_markets)
            opportunities = self._refresh_kalshi_prices(opportunities)

            duration = _time.monotonic() - t0
            logger.info("FullArbScanner: found %d opportunities in %.1fs",
                        len(opportunities), duration)
            with self._lock:
                self._snapshot = ArbSnapshot(
                    state=ArbScanState.IDLE,
                    opportunities=opportunities,
                    poly_count=len(poly_markets),
                    kalshi_count=len(kalshi_markets),
                    last_scan_at=datetime.utcnow(),
                    scan_duration_s=round(duration, 1),
                )
        except Exception as exc:
            duration = _time.monotonic() - t0
            logger.warning("FullArbScanner error: %s", exc)
            with self._lock:
                self._snapshot = ArbSnapshot(
                    state=ArbScanState.ERROR,
                    opportunities=self._snapshot.opportunities,
                    poly_count=self._snapshot.poly_count,
                    kalshi_count=self._snapshot.kalshi_count,
                    last_scan_at=self._snapshot.last_scan_at,
                    scan_duration_s=round(duration, 1),
                    error=str(exc),
                )

    def _refresh_kalshi_prices(
        self, opportunities: list[ArbitrageOpportunity]
    ) -> list[ArbitrageOpportunity]:
        """Fetch live Kalshi prices for every matched ticker and update the opportunities.

        Batch scan prices can be minutes stale by the time arb matching completes.
        Re-fetches each matched ticker via the individual market endpoint (fast, accurate)
        and recomputes roi_pct so displayed values are current.
        """
        if not opportunities:
            return opportunities

        tickers = list({o.kalshi_ticker for o in opportunities})
        logger.info("FullArbScanner: refreshing live prices for %d Kalshi tickers", len(tickers))

        fresh: dict[str, object] = {}
        with ThreadPoolExecutor(max_workers=min(10, len(tickers))) as pool:
            futures = {pool.submit(self._kalshi_client.get_market, t): t for t in tickers}
            for fut in _as_completed2(futures):
                try:
                    km = fut.result()
                    if km:
                        fresh[km.ticker] = km
                except Exception as exc:
                    logger.debug("Kalshi price refresh failed: %s", exc)

        fee = self._config.fee_rate
        refreshed: list[ArbitrageOpportunity] = []
        for opp in opportunities:
            km = fresh.get(opp.kalshi_ticker)
            if km is None:
                refreshed.append(opp)
                continue

            if opp.arb_type == "TRUE_ARB":
                new_kalshi_price = km.no_price if "NO" in opp.kalshi_action else km.yes_price
                # Poly prices from Gamma API are mid/last-trade, not executable ask prices.
                # Add 1c conservative slippage buffer so displayed ROI reflects reality.
                poly_exec = round(min(opp.poly_price + 0.01, 0.99), 4)
                cost = poly_exec + new_kalshi_price
                new_roi = round((1.0 / (cost * (1 + fee)) - 1.0) * 100, 2)
                new_poly_price = poly_exec
            else:
                # Soft arb: use the Kalshi leg that matches kalshi_action.
                # The proximity heuristic (pick YES/NO closest to stored price) is wrong:
                # if prices move after scan time, it can silently flip to the wrong contract.
                new_kalshi_price = km.yes_price if "YES" in opp.kalshi_action else km.no_price
                new_roi = round(-abs(opp.poly_price - new_kalshi_price) * 100, 2)
                new_poly_price = opp.poly_price

            refreshed.append(dataclasses.replace(
                opp,
                poly_price=new_poly_price,
                kalshi_price=round(new_kalshi_price, 4),
                roi_pct=new_roi,
            ))

        return refreshed
