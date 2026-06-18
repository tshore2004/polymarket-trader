"""KalshiScanner — background scanner for Kalshi prediction market signals.

Scoring (4 factors, no leaderboard — Kalshi has no public leaderboard):
  fair_value_edge  0–40  (reuses FairValueAnalyzer / Odds API)
  volume_movement  0–30  (order book imbalance + spread tightness)
  news_momentum    0–15  (reuses NewsSentimentAnalyzer)
  urgency          0–15  (time to close)
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
from core.kalshi_client import KalshiClient
from utils.models import KalshiMarket, KalshiSignal

logger = logging.getLogger(__name__)


class KalshiScanState(str, Enum):
    IDLE = "idle"
    SCANNING = "scanning"
    ERROR = "error"


@dataclass
class KalshiSnapshot:
    state: KalshiScanState = KalshiScanState.IDLE
    signals: list[KalshiSignal] = field(default_factory=list)
    markets_loaded: int = 0
    last_scan_at: Optional[datetime] = None
    scan_duration_s: Optional[float] = None
    error: Optional[str] = None
    enabled: bool = True


class KalshiScanner:
    """Runs periodic Kalshi market scans and exposes a snapshot for the API."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._client = KalshiClient(config.kalshi_api_key)
        self._snapshot = KalshiSnapshot(enabled=config.kalshi_enabled)
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._fv = None
        self._news = None

    def start(self) -> None:
        if not self._config.kalshi_enabled:
            logger.info("Kalshi scanner disabled (KALSHI_ENABLED=false)")
            return
        if not self._client.enabled:
            logger.info("Kalshi scanner: no API key configured — tab will show setup prompt")
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="KalshiScanner")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def get_snapshot(self) -> KalshiSnapshot:
        with self._lock:
            return self._snapshot

    def trigger_scan(self) -> bool:
        if not self._client.enabled:
            return False
        t = threading.Thread(target=self._scan_once, daemon=True, name="KalshiScanOnce")
        t.start()
        return True

    def _loop(self) -> None:
        self._scan_once()
        while not self._stop_event.wait(timeout=self._config.scan_interval):
            self._scan_once()

    def _scan_once(self) -> None:
        self._set_state(KalshiScanState.SCANNING)
        t0 = _time.monotonic()
        try:
            markets = self._client.get_markets(limit=200)
            markets = [m for m in markets if m.time_category != "past"]
            signals = self._score_markets(markets)
            signals.sort(key=lambda s: s.combined_score, reverse=True)

            with self._lock:
                self._snapshot = KalshiSnapshot(
                    state=KalshiScanState.IDLE,
                    signals=signals,
                    markets_loaded=len(markets),
                    last_scan_at=datetime.utcnow(),
                    scan_duration_s=round(_time.monotonic() - t0, 2),
                    enabled=True,
                )
        except Exception as exc:
            logger.exception("Kalshi scan error: %s", exc)
            with self._lock:
                self._snapshot = KalshiSnapshot(
                    state=KalshiScanState.ERROR,
                    error=str(exc),
                    enabled=True,
                )

    def _set_state(self, state: KalshiScanState) -> None:
        with self._lock:
            prev = self._snapshot
            self._snapshot = KalshiSnapshot(
                state=state,
                signals=prev.signals,
                markets_loaded=prev.markets_loaded,
                last_scan_at=prev.last_scan_at,
                enabled=True,
            )

    def _score_markets(self, markets: list[KalshiMarket]) -> list[KalshiSignal]:
        fv_analyzer = self._get_fv_analyzer()
        news_analyzer = self._get_news_analyzer()

        if fv_analyzer:
            try:
                fv_analyzer.refresh()
            except Exception:
                pass
        if news_analyzer:
            try:
                news_analyzer.refresh()
            except Exception:
                pass

        signals = []
        for market in markets:
            sig = self._score_one(market, fv_analyzer, news_analyzer)
            if sig and sig.combined_score >= 5.0:
                signals.append(sig)
        return signals

    def _score_one(self, market: KalshiMarket, fv_analyzer, news_analyzer) -> Optional[KalshiSignal]:
        try:
            fv_score, fair_value, fv_source, edge_pct, recommended_side = (
                self._fair_value_score(market, fv_analyzer)
            )

            vol_score = self._volume_score(market)

            news_score = 0.0
            if news_analyzer and self._config.news_enabled:
                try:
                    raw_score, _ = news_analyzer.score_market(market)
                    news_score = min(15.0, raw_score * 0.3)
                except Exception:
                    pass

            urg_score = round(market.urgency_score * 15.0, 2)
            combined = round(fv_score + vol_score + news_score + urg_score, 2)

            parts = []
            if fv_score > 0:
                parts.append(f"Edge {fv_score:.0f}/40 ({fv_source})")
            if vol_score > 0:
                parts.append(f"Volume {vol_score:.0f}/30")
            if news_score > 0:
                parts.append(f"News {news_score:.0f}/15")
            if urg_score > 0:
                parts.append(f"Urgency {urg_score:.0f}/15")
            explanation = " | ".join(parts) or "No dominant signal"

            return KalshiSignal(
                market=market,
                combined_score=combined,
                recommended_side=recommended_side,
                fair_value=fair_value,
                edge_pct=edge_pct,
                explanation=explanation,
                fair_value_score=fv_score,
                volume_score=vol_score,
                news_score=news_score,
                urgency_score_val=urg_score,
            )
        except Exception as exc:
            logger.debug("Kalshi score_one failed for %s: %s", market.ticker, exc)
            return None

    def _fair_value_score(
        self, market: KalshiMarket, fv_analyzer
    ) -> tuple[float, Optional[float], str, float, str]:
        if not fv_analyzer:
            return 0.0, None, "", 0.0, "YES"
        try:
            fv, source = fv_analyzer.estimate_fair_value(market)
        except Exception:
            return 0.0, None, "", 0.0, "YES"

        if fv is None:
            return 0.0, None, "", 0.0, "YES"

        yes_edge = fv - market.yes_price
        no_edge = (1.0 - fv) - market.no_price

        if yes_edge >= no_edge and yes_edge > 0:
            score = min(40.0, yes_edge * 400.0)
            return round(score, 2), round(fv, 4), source, round(yes_edge, 4), "YES"
        elif no_edge > 0:
            score = min(40.0, no_edge * 400.0)
            return round(score, 2), round(fv, 4), source, round(no_edge, 4), "NO"
        return 0.0, round(fv, 4), source, 0.0, "YES"

    def _volume_score(self, market: KalshiMarket) -> float:
        if market.yes_price <= 0 or market.no_price <= 0:
            return 0.0
        spread = abs(1.0 - (market.yes_price + market.no_price))
        if spread > 0.15:
            return 0.0
        tightness = max(0.0, 1.0 - spread / 0.15)
        vol_norm = min(1.0, (market.volume or 0) / 5000.0)
        return round(min(30.0, tightness * vol_norm * 30.0), 2)

    def _get_fv_analyzer(self):
        if self._fv is not None:
            return self._fv
        try:
            from core.api_client import PolymarketPublicClient
            from core.fair_value import FairValueAnalyzer
            client = PolymarketPublicClient()
            self._fv = FairValueAnalyzer(client, odds_api_key=self._config.odds_api_key)
        except Exception as exc:
            logger.debug("FairValueAnalyzer unavailable for Kalshi: %s", exc)
            self._fv = None
        return self._fv

    def _get_news_analyzer(self):
        if self._news is not None:
            return self._news
        try:
            from core.news_sentiment import NewsSentimentAnalyzer
            self._news = NewsSentimentAnalyzer(self._config)
        except Exception as exc:
            logger.debug("NewsSentimentAnalyzer unavailable for Kalshi: %s", exc)
            self._news = None
        return self._news
