from __future__ import annotations
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from config import Config
from core.api_client import PolymarketPublicClient
from core.leaderboard import LeaderboardAnalyzer
from core.news_sentiment import NewsSentimentAnalyzer
from core.volume_tracker import VolumeTracker
from core.fair_value import FairValueAnalyzer
from core.pinnacle import PinnacleClient
from utils.categories import detect_market_category
from utils.models import (
    Market, Signal, SignalType, Side, ScoreBreakdown,
    MarketConsensus,
)

logger = logging.getLogger(__name__)

_VALID_MODES = {"all", "leaderboard", "news", "volume", "today"}


class SignalEngine:
    """
    Multi-factor signal engine. Scoring (0–100):
      - Leaderboard conviction: 0–30 (hedging-filtered consensus)
      - Fair value edge:        0–30 (external odds vs Polymarket price)
      - Line movement:          0–20 (volume spikes + price momentum)
      - News momentum:          0–10 (trending headlines)
      - Urgency:                0–10 (sooner resolution = higher)
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._client = PolymarketPublicClient()
        self._lb = LeaderboardAnalyzer(self._client, config)
        self._news = NewsSentimentAnalyzer(config)
        self._vol = VolumeTracker(config.volume_spike_threshold)
        _pinnacle = PinnacleClient(
            getattr(config, "pinnacle_username", ""),
            getattr(config, "pinnacle_password", ""),
        )
        self._fv = FairValueAnalyzer(
            self._client,
            getattr(config, "odds_api_key", ""),
            pinnacle=_pinnacle if _pinnacle.enabled else None,
        )

        self._markets: list[Market] = []
        self._universe: list[Market] = []
        self._last_market_refresh: float = 0.0
        self._last_lb_refresh: float = 0.0
        self._last_consensuses: list[MarketConsensus] = []
        self._last_picks: list[MarketConsensus] = []
        self._last_news_signals: list[Signal] = []
        self._last_volume_signals: list[Signal] = []

    # ── Public ────────────────────────────────────────────────────────────────

    def scan(self, mode: str = "all", progress_cb=None) -> list[Signal]:
        mode = mode.lower().strip()
        if mode not in _VALID_MODES:
            logger.warning("Unknown scan mode %r — defaulting to 'all'", mode)
            mode = "all"

        def _p(stage: str, pct: float) -> None:
            if progress_cb:
                progress_cb(stage, pct)

        now = time.monotonic()

        # Refresh market list
        if now - self._last_market_refresh > self._config.market_refresh:
            _p("Loading markets...", 8)
            tags = self._config.market_tags_filter if self._config.market_tags_filter else None
            self._markets = self._client.get_markets(limit=1000, tags_filter=tags)
            # Short-term modes also pull markets resolving soon (e.g. tonight's games),
            # which a generic unordered scan frequently never reaches.
            if mode in ("all", "today"):
                _p("Loading near-term events...", 14)
                near = self._client.get_near_term_markets(
                    hours=max(self._config.short_term_hours, 24),
                    tags_filter=tags,
                )
                self._markets = self._merge_markets(self._markets, near)
                logger.info("Pulled %d near-term markets (<= %dh).",
                            len(near), max(self._config.short_term_hours, 24))
            self._last_market_refresh = now
            logger.info("Loaded %d active markets.", len(self._markets))
        _p("Markets loaded", 20)

        # Leaderboard refresh — do this BEFORE choosing the working set, because the
        # smart-money picks define which markets we care about (incl. ones the top
        # traders hold that weren't in the generic market scan).
        if mode in ("all", "leaderboard", "today"):
            if now - self._last_lb_refresh > self._config.leaderboard_refresh:
                _p("Refreshing leaderboard traders...", 30)
                self._lb.refresh()
                self._last_lb_refresh = now
        _p("Leaderboard ready", 45)

        # Fair value refresh
        _p("Refreshing fair value data...", 50)
        self._fv.refresh()

        # Smart-money copy picks (position-driven universe + hedging filter)
        copy_picks: list[MarketConsensus] = []
        if mode in ("all", "leaderboard", "today"):
            _p("Building smart-money consensus...", 60)
            copy_picks = self._lb.build_copy_picks(
                self._markets,
                min_position_usd=self._config.copy_min_position_usd,
                short_term_hours=self._config.short_term_hours,
            )
            self._last_consensuses = copy_picks
            self._last_picks = copy_picks

        # Build the scoring universe: generic markets + everything the top traders
        # actually hold (so a tonight game the scan missed still becomes a pick).
        self._universe = self._combined_universe(copy_picks)

        # Determine working set
        if mode == "today":
            working_markets = self._filter_short_term(self._universe, self._config.short_term_hours)
            logger.info("Today mode: %d markets resolving within %dh.",
                        len(working_markets), self._config.short_term_hours)
        else:
            working_markets = self._universe

        # Volume signals: build BEFORE updating tracker
        if self._config.volume_spike_enabled and mode in ("all", "volume", "news", "today"):
            _p("Computing volume signals...", 70)
            self._last_volume_signals = self._build_volume_signals(working_markets)
        else:
            self._last_volume_signals = []

        if self._config.volume_spike_enabled:
            self._vol.update(working_markets)

        # News signals
        if self._config.news_enabled and mode in ("all", "news", "today"):
            _p("Processing news signals...", 78)
            self._news.refresh()
            self._last_news_signals = self._build_news_signals(working_markets)
        else:
            self._last_news_signals = []

        # Build multi-factor signals for all markets
        _p("Scoring markets...", 85)
        signals = self._build_multifactor_signals(
            working_markets, copy_picks,
            self._last_news_signals, self._last_volume_signals,
            mode=mode,
        )

        _p("Ranking results...", 95)
        logger.info(
            "Scan summary — Markets: %d | Universe: %d | Working: %d | Traders: %d | "
            "Copy picks: %d | FV edges: %d | News: %d | Vol: %d | Qualifying: %d",
            len(self._markets),
            len(self._universe),
            len(working_markets),
            len(self._lb.traders),
            len(copy_picks),
            sum(1 for s in signals if s.scores.fair_value_edge > 0),
            len(self._last_news_signals),
            len(self._last_volume_signals),
            len(signals),
        )
        return sorted(signals, key=lambda s: s.combined_score, reverse=True)

    @property
    def markets_loaded(self) -> int:
        return len(self._markets)

    @property
    def last_consensuses(self) -> list[MarketConsensus]:
        return self._last_consensuses

    @property
    def last_picks(self) -> list[MarketConsensus]:
        return self._last_picks

    def pick_signals(self) -> list[Signal]:
        """Fully-priced Signals for every smart-money copy pick (all horizons).

        Unlike scan(), this ignores the short-term working-set filter so the daily
        report can show top-trader picks resolving tonight *and* long-dated holds.
        """
        if not self._last_picks:
            return []
        pick_markets = [c.market for c in self._last_picks]
        return self._build_multifactor_signals(
            pick_markets, self._last_picks,
            self._last_news_signals, self._last_volume_signals,
            mode="all",
        )

    @property
    def last_news_signals(self) -> list[Signal]:
        return self._last_news_signals

    @property
    def last_volume_signals(self) -> list[Signal]:
        return self._last_volume_signals

    def get_todays_events(self) -> dict[str, list[Market]]:
        """Return short-term markets grouped by event slug / category."""
        source = self._universe or self._markets
        todays = self._filter_short_term(source, self._config.short_term_hours)
        grouped: dict[str, list[Market]] = {}
        for m in todays:
            key = m.event_slug or m.question[:40]
            grouped.setdefault(key, []).append(m)
        return grouped

    def get_all_events(self) -> dict[str, list[Market]]:
        """Return all markets grouped by event slug for the interactive browser."""
        source = self._universe or self._markets
        grouped: dict[str, list[Market]] = {}
        for m in source:
            key = m.event_slug or m.question[:40]
            grouped.setdefault(key, []).append(m)
        return grouped

    # ── Private ───────────────────────────────────────────────────────────────

    @staticmethod
    def _merge_markets(*lists: list[Market]) -> list[Market]:
        """Union markets across lists, de-duplicated by condition_id (first wins)."""
        merged: dict[str, Market] = {}
        for lst in lists:
            for m in lst:
                if m.condition_id and m.condition_id not in merged:
                    merged[m.condition_id] = m
        return list(merged.values())

    def _combined_universe(self, copy_picks: list[MarketConsensus]) -> list[Market]:
        """Generic markets + every market the top traders hold (resolved on demand)."""
        pick_markets = [c.market for c in copy_picks]
        return self._merge_markets(self._markets, self._lb.held_markets(), pick_markets)

    @staticmethod
    def _filter_today(markets: list[Market]) -> list[Market]:
        """Return markets that resolve within the next 24 hours."""
        return SignalEngine._filter_short_term(markets, 24)

    @staticmethod
    def _filter_short_term(markets: list[Market], hours: int) -> list[Market]:
        """Return markets that resolve within the next `hours` hours, soonest first."""
        now = datetime.now(timezone.utc)
        scored: list[tuple[float, Market]] = []
        for m in markets:
            if m.end_date is None:
                continue
            ed = m.end_date
            if ed.tzinfo is None:
                ed = ed.replace(tzinfo=timezone.utc)
            secs = (ed - now).total_seconds()
            if 0 < secs <= hours * 3600:
                scored.append((secs, m))
        scored.sort(key=lambda pair: pair[0])
        return [m for _, m in scored]

    def _build_multifactor_signals(
        self,
        markets: list[Market],
        consensuses: list[MarketConsensus],
        news_signals: list[Signal],
        volume_signals: list[Signal],
        mode: str = "all",
    ) -> list[Signal]:
        """Score every market using multiple factors and return qualifying signals."""
        threshold = (
            self._config.min_signal_score_today if mode == "today"
            else self._config.min_signal_score
        )
        con_by_id = {c.market.condition_id: c for c in consensuses}
        news_by_id = {s.market.condition_id: s for s in news_signals}
        vol_by_id = {s.market.condition_id: s for s in volume_signals}

        # Pre-filter to qualifying markets and determine which tokens need midpoints
        qualifying: list[tuple[Market, MarketConsensus | None, Signal | None, Signal | None, Side]] = []
        tokens_to_fetch: dict[str, str] = {}  # token_id → condition_id

        for market in markets:
            if market.time_category == "past" or market.closed or not market.active:
                continue
            # Skip markets with pinned token prices — outcome already determined
            # even if the closed flag hasn't propagated from the API yet.
            yt = market.yes_token
            nt = market.no_token
            if (yt and yt.price >= 0.95) or (nt and nt.price >= 0.95):
                continue
            mid = market.condition_id
            con = con_by_id.get(mid)
            news_sig = news_by_id.get(mid)
            vol_sig = vol_by_id.get(mid)
            is_liquid_tonight = (market.urgency_score == 1.0 and market.volume >= 10_000)
            has_info = bool(con or vol_sig or news_sig or is_liquid_tonight)
            if not has_info:
                continue

            if con:
                side = Side.YES if con.dominant_side == Side.YES else Side.NO
            elif vol_sig:
                side = vol_sig.recommended_side
            else:
                side = Side.YES

            qualifying.append((market, con, news_sig, vol_sig, side))
            token = market.yes_token if side == Side.YES else market.no_token
            if token and token.token_id:
                tokens_to_fetch[token.token_id] = mid

        # Batch-fetch midpoints in parallel (much faster than sequential)
        midpoints: dict[str, float] = {}
        if tokens_to_fetch:
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = {
                    pool.submit(self._client.get_midpoint, tid): tid
                    for tid in tokens_to_fetch
                }
                for fut in as_completed(futures):
                    tid = futures[fut]
                    try:
                        result = fut.result()
                        if result is not None:
                            midpoints[tid] = result
                    except Exception:
                        pass

        signals: list[Signal] = []

        for market, con, news_sig, vol_sig, side in qualifying:
            mid = market.condition_id

            scores = ScoreBreakdown()

            # 1. Leaderboard conviction — category-aware cap
            # Sports (97% of picks): top traders are price-setters, copying them is lagged.
            # Non-sports (politics, crypto, news): informed traders have genuine edge.
            # Calibration at n=100 shows leaderboard anti-predictive in sports, directionally
            # positive in non-sports. Apply a 0.4x sports discount until n=250 for reanalysis.
            if con:
                cat, _ = detect_market_category(market)
                lb_multiplier = 0.4 if cat == "sports" else 1.2
                scores.leaderboard = round(min(10.0, 0.10 * con.copy_score) * lb_multiplier, 2)

            # 2. Get cached midpoint price
            token = market.yes_token if side == Side.YES else market.no_token
            price = 0.5
            if token and token.token_id:
                price = midpoints.get(token.token_id, 0.5)

            # Skip near-resolved markets (outcome already determined, closed flag lagging)
            if price <= 0.03 or price >= 0.97:
                logger.debug("Skipping near-resolved market %s (price=%.3f)", mid, price)
                continue

            # 3. Fair value edge (0–30)
            edge_pct, fair_value, fv_source = self._fv.edge(
                market, side.value, price
            )
            scores.fair_value_edge = round(self._fv.score(edge_pct), 2)

            # 4. Line movement / volume (0–20)
            if vol_sig:
                # Scale the volume signal's score to 0-20 range
                raw_vol = vol_sig.scores.line_movement if vol_sig.scores else vol_sig.combined_score
                scores.line_movement = round(min(20.0, raw_vol), 2)

            # 5. News momentum (0–10)
            if news_sig:
                # News was scored 0-50 before, scale to 0-10
                raw_news = news_sig.combined_score
                scores.news_momentum = round(min(10.0, raw_news / 5.0), 2)

            # 6. Urgency (0–10)
            scores.urgency = round(market.urgency_score * 10.0, 2)

            combined = round(scores.total, 2)
            # Smart-money picks always surface (that's the whole point); other
            # markets must clear the score threshold.
            if con is None and combined < threshold:
                continue

            # Determine signal type
            contributing = sum([
                scores.leaderboard > 0,
                scores.fair_value_edge > 0,
                scores.line_movement > 0,
                scores.news_momentum > 0,
            ])
            if contributing >= 2:
                sig_type = SignalType.MULTI
            elif scores.leaderboard > 0:
                sig_type = SignalType.LEADERBOARD
            elif scores.fair_value_edge > 0:
                sig_type = SignalType.FAIR_VALUE
            elif scores.line_movement > 0:
                sig_type = SignalType.VOLUME_SPIKE
            elif scores.news_momentum > 0:
                sig_type = SignalType.NEWS
            else:
                sig_type = SignalType.LEADERBOARD

            # Build explanation
            parts = [scores.explain()]
            if con:
                names = ", ".join(t.name or t.address[:8] for t in con.traders[:3]) or "—"
                parts.append(
                    f"{con.num_traders_dominant} top trader(s) backing {con.dominant_side.value} "
                    f"(${con.dominant_position_value:,.0f} held, {con.confidence*100:.0f}% agreement, "
                    f"{con.avg_dominant_win_rate*100:.0f}% win rate) — copy strength {con.copy_score:.0f}/100"
                )
                parts.append(f"Smart money: {names}")
            if fv_source:
                parts.append(fv_source)
                if edge_pct > 0:
                    parts.append(f"Edge: +{edge_pct*100:.1f}% vs fair value")

            signals.append(Signal(
                market=market,
                combined_score=combined,
                signal_type=sig_type,
                recommended_side=side,
                recommended_price=round(price, 4),
                scores=scores,
                consensus=con,
                explanation=" | ".join(parts),
                fair_value=fair_value,
                fv_source=fv_source,
                edge_pct=edge_pct,
            ))

        return signals

    def _build_news_signals(self, markets: list[Market]) -> list[Signal]:
        results = self._news.get_signals(markets)
        signals = []
        for market, news_score, explanation in results:
            yes_token = market.yes_token
            price = self._client.get_midpoint(yes_token.token_id) if yes_token else None
            scores = ScoreBreakdown(news_momentum=round(min(10.0, news_score / 5.0), 2))
            signals.append(Signal(
                market=market,
                combined_score=round(news_score, 2),
                signal_type=SignalType.NEWS,
                recommended_side=Side.YES,
                recommended_price=round(price or 0.5, 4),
                scores=scores,
                explanation=explanation,
            ))
        return signals

    def _build_volume_signals(self, markets: list[Market]) -> list[Signal]:
        spikes = self._vol.get_spikes(markets)
        price_moves = self._vol.get_price_moves(markets)

        seen: set[str] = set()
        signals: list[Signal] = []

        # First-run fallback: no spike history yet → score top-quartile by raw volume
        if not spikes and not price_moves and not self._vol._prev_volumes:
            high_vol = self._vol.get_high_volume_markets(markets)
            for market, score, explanation in high_vol:
                if market.condition_id in seen:
                    continue
                seen.add(market.condition_id)
                yes = market.yes_token
                price = yes.price if yes and yes.price > 0 else 0.5
                scores = ScoreBreakdown(line_movement=score)
                signals.append(Signal(
                    market=market,
                    combined_score=score,
                    signal_type=SignalType.VOLUME_SPIKE,
                    recommended_side=Side.YES,
                    recommended_price=round(price, 4),
                    scores=scores,
                    explanation=explanation,
                ))
            return sorted(signals, key=lambda s: -s.combined_score)

        for market, ratio, explanation in spikes:
            if market.condition_id in seen:
                continue
            seen.add(market.condition_id)
            raw_score = round(min(20.0, (ratio - 1.0) / 4.0 * 20.0), 2)
            yes = market.yes_token
            price = yes.price if yes and yes.price > 0 else 0.5
            scores = ScoreBreakdown(line_movement=raw_score)
            signals.append(Signal(
                market=market,
                combined_score=raw_score,
                signal_type=SignalType.VOLUME_SPIKE,
                recommended_side=Side.YES,
                recommended_price=round(price, 4),
                scores=scores,
                explanation=explanation,
            ))

        for market, delta, explanation in price_moves:
            if market.condition_id in seen:
                continue
            seen.add(market.condition_id)
            raw_score = round(min(10.0, abs(delta) / 0.2 * 10.0), 2)
            yes = market.yes_token
            price = yes.price if yes and yes.price > 0 else 0.5
            side = Side.YES if delta > 0 else Side.NO
            scores = ScoreBreakdown(line_movement=raw_score)
            signals.append(Signal(
                market=market,
                combined_score=raw_score,
                signal_type=SignalType.VOLUME_SPIKE,
                recommended_side=side,
                recommended_price=round(price, 4),
                scores=scores,
                explanation=explanation,
            ))

        return sorted(signals, key=lambda s: -s.combined_score)
