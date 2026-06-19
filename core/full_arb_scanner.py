"""FullArbScanner — background scanner that scans ALL open Poly + Kalshi markets for arb.

Unlike the signal-based arb check in server.py, this scanner fetches the full
universe of open markets on both platforms and runs CrossPlatformArbScanner on them.
Results are cached in a snapshot and refreshed every arb_scan_interval seconds.
"""
from __future__ import annotations
import logging
import threading
import time as _time
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
        # Run immediately on first start
        self._scan_now.set()
        while not self._stop_event.is_set():
            self._scan_now.wait(timeout=self._config.arb_scan_interval)
            if self._stop_event.is_set():
                break
            self._scan_now.clear()
            self._run_scan()

    def _run_scan(self) -> None:
        with self._lock:
            self._snapshot = ArbSnapshot(state=ArbScanState.SCANNING,
                                         opportunities=self._snapshot.opportunities,
                                         poly_count=self._snapshot.poly_count,
                                         kalshi_count=self._snapshot.kalshi_count,
                                         last_scan_at=self._snapshot.last_scan_at)
        t0 = _time.monotonic()
        try:
            logger.info("FullArbScanner: fetching all open markets...")
            poly_markets = self._poly_client.get_markets(limit=2000, active_only=True)
            kalshi_markets = self._kalshi_client.get_markets(limit=500)
            logger.info("FullArbScanner: poly=%d kalshi=%d — running arb scan",
                        len(poly_markets), len(kalshi_markets))
            opportunities = self._arb_scanner.find_opportunities(poly_markets, kalshi_markets)
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
