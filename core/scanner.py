"""BackgroundScanner — daemon thread that runs SignalEngine on an interval."""
from __future__ import annotations
import copy
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from core.executor import TradeExecutor
from core.signal import SignalEngine
from utils.models import LeaderboardTrader, MarketConsensus, Signal

logger = logging.getLogger(__name__)


class ScanState(str, Enum):
    IDLE = "idle"
    SCANNING = "scanning"
    ERROR = "error"


@dataclass
class ScanSnapshot:
    state: ScanState = ScanState.IDLE
    last_scan_at: Optional[datetime] = None
    scan_duration_s: Optional[float] = None
    markets_loaded: int = 0
    signals: list[Signal] = field(default_factory=list)
    traders: list[LeaderboardTrader] = field(default_factory=list)
    consensuses: list[MarketConsensus] = field(default_factory=list)
    picks: list[MarketConsensus] = field(default_factory=list)
    news_signals: list[Signal] = field(default_factory=list)
    volume_signals: list[Signal] = field(default_factory=list)
    scan_mode: str = "all"
    balance: Optional[float] = None
    error: Optional[str] = None
    scan_count: int = 0
    scan_stage: str = ""
    scan_progress: float = 0.0


class BackgroundScanner:
    def __init__(
        self,
        engine: SignalEngine,
        executor: TradeExecutor,
        scan_interval: int,
        scan_mode: str = "all",
    ) -> None:
        self._engine = engine
        self._executor = executor
        self._scan_interval = scan_interval
        self._scan_mode = scan_mode
        self._lock = threading.Lock()
        self._snapshot = ScanSnapshot(scan_mode=scan_mode)
        self._manual_trigger = threading.Event()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="scanner", daemon=True)
        self._thread.start()
        logger.info("BackgroundScanner started (interval=%ds).", self._scan_interval)

    def stop(self) -> None:
        self._stop_event.set()
        self._manual_trigger.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        logger.info("BackgroundScanner stopped.")

    def trigger_scan(self, mode: str | None = None) -> bool:
        with self._lock:
            if self._snapshot.state == ScanState.SCANNING:
                return False
            if mode:
                self._scan_mode = mode
        self._manual_trigger.set()
        return True

    def set_mode(self, mode: str) -> None:
        with self._lock:
            self._scan_mode = mode

    def get_snapshot(self) -> ScanSnapshot:
        with self._lock:
            return copy.copy(self._snapshot)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self._run_scan()
            self._manual_trigger.wait(timeout=self._scan_interval)
            self._manual_trigger.clear()

    def _run_scan(self) -> None:
        with self._lock:
            self._snapshot.state = ScanState.SCANNING
            self._snapshot.error = None
            self._snapshot.scan_stage = "Starting..."
            self._snapshot.scan_progress = 2.0

        t0 = time.monotonic()
        with self._lock:
            current_mode = self._scan_mode

        def _progress(stage: str, pct: float) -> None:
            with self._lock:
                self._snapshot.scan_stage = stage
                self._snapshot.scan_progress = pct

        try:
            balance = self._executor.get_balance()
            signals = self._engine.scan(mode=current_mode, progress_cb=_progress)
            traders = self._engine._lb.traders
            consensuses = self._engine.last_consensuses
            elapsed = time.monotonic() - t0

            with self._lock:
                snap = self._snapshot
                snap.state = ScanState.IDLE
                snap.last_scan_at = datetime.now(timezone.utc)
                snap.scan_duration_s = round(elapsed, 1)
                snap.markets_loaded = self._engine.markets_loaded
                snap.signals = signals
                snap.traders = traders
                snap.consensuses = consensuses
                snap.picks = self._engine.last_picks
                snap.news_signals = self._engine.last_news_signals
                snap.volume_signals = self._engine.last_volume_signals
                snap.scan_mode = current_mode
                snap.balance = balance
                snap.error = None
                snap.scan_count += 1
                snap.scan_stage = ""
                snap.scan_progress = 100.0

            logger.info(
                "Scan #%d [%s] complete in %.1fs — %d signals, %d traders.",
                snap.scan_count, current_mode, elapsed, len(signals), len(traders),
            )

        except Exception as exc:
            elapsed = time.monotonic() - t0
            logger.error("Scan failed after %.1fs: %s", elapsed, exc)
            with self._lock:
                self._snapshot.state = ScanState.ERROR
                self._snapshot.error = str(exc)
                self._snapshot.scan_duration_s = round(elapsed, 1)
                self._snapshot.scan_stage = ""
                self._snapshot.scan_progress = 0.0
