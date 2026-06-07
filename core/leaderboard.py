from __future__ import annotations
import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from datetime import datetime, timezone
from config import Config
from core.api_client import PolymarketPublicClient
from utils.models import Market, Token, LeaderboardTrader, TraderPosition, MarketConsensus, TraderStake, TraderStats, Side
from utils.categories import detect_market_category
from core.starred_traders import StarredTraderStore

logger = logging.getLogger(__name__)

_MAX_WORKERS = 6
_MAX_PICKS_PER_EVENT = 2
# Cap how many position-held markets we enrich via Gamma per refresh. The rest are
# synthesized from the position payload, keeping scans fast and resilient to resets.
_MAX_GAMMA_ENRICH = 60

# ── Hedging Detection ─────────────────────────────────────────────────────────
# A trader who bets on >MAX_OUTCOMES_PER_EVENT outcomes in a single event is
# hedging / portfolio-diversifying, not showing conviction. Their positions in
# that event get downweighted or excluded.
_MAX_OUTCOMES_PER_EVENT = 2


def _opponent_label(question: str, held: str) -> str:
    """For a matchup market ('Team A vs. Team B'), return the side that isn't `held`.

    Returns "" when the question isn't a recognizable two-sided matchup, in which
    case callers fall back to a generic opposite label.
    """
    if not question or not held:
        return ""
    parts = re.split(r"\s+vs\.?\s+", question, flags=re.IGNORECASE)
    if len(parts) != 2:
        return ""
    a, b = parts[0].strip(), parts[1].strip()
    h = held.strip().lower()
    if h and h in a.lower():
        return b
    if h and h in b.lower():
        return a
    return ""


def _named_labels(question: str, held: str) -> tuple[str, str]:
    """Build (held_label, opposite_label) for a named (non Yes/No) outcome.

    Handles over/under totals ("O/U 212.5" → "Over 212.5" / "Under 212.5"),
    two-sided team matchups, and falls back to the raw label otherwise.
    """
    held = held.strip()
    low = held.lower()

    # Over/Under totals — pull the line number off the question and attach it.
    if low in ("over", "under", "o", "u"):
        m = re.search(r"(\d+(?:\.\d+)?)", question or "")
        line = f" {m.group(1)}" if m else ""
        over_lbl, under_lbl = f"Over{line}".strip(), f"Under{line}".strip()
        return (over_lbl, under_lbl) if low.startswith("o") else (under_lbl, over_lbl)

    # Team / named matchup.
    opp = _opponent_label(question, held)
    return held, (opp or "Other")


def _event_key(market: Market) -> str:
    """Group key for deduplication. Prefers API eventSlug; falls back to question tail."""
    if market.event_slug:
        return market.event_slug.lower()
    words = market.question.lower().split()
    return " ".join(w.strip("?.,!;:") for w in words[-4:])


def _market_from_position(pos: TraderPosition) -> "Market | None":
    """Synthesize a Market straight from a trader-position payload.

    Used when the generic market scan and the Gamma re-fetch both miss a market a
    top trader is holding (common for fast-fuse sports markets). The /positions
    payload carries both the held token (`asset`) and the other side
    (`oppositeAsset`), so the synthesized market is fully tradeable.
    """
    if not pos.market_id or not pos.title:
        return None
    held_outcome = (pos.outcome or "Yes").strip()
    low = held_outcome.lower()
    is_yes = low in ("yes", "1")
    # Named outcomes (e.g. "Cleveland Guardians") carry the real pick label; only
    # genuine binary markets use "Yes"/"No". Preserve the named label so the UI can
    # show the team/pick instead of falling back to YES/NO.
    is_named = low not in ("yes", "no", "1", "0", "")
    if is_named:
        held_label, other_label = _named_labels(pos.title, held_outcome)
    else:
        held_label = "Yes" if is_yes else "No"
        other_label = "No" if is_yes else "Yes"
    held_tok = Token(token_id=pos.token_id or "", outcome=held_label,
                     price=pos.cur_price or 0.0)
    other_tok = Token(token_id=pos.opposite_token_id or "", outcome=other_label)
    tokens = [held_tok, other_tok] if is_yes else [other_tok, held_tok]

    # If the position payload carries an end_date that's already past, mark the
    # synthesized market closed/inactive so it gets filtered before reaching
    # consensus. Without this, end_date=None synthesized markets return "ongoing"
    # from time_category and slip through the "past" guard in _to_consensus_list.
    now = datetime.now(timezone.utc)
    is_past = False
    if pos.end_date is not None:
        ed = pos.end_date if pos.end_date.tzinfo else pos.end_date.replace(tzinfo=timezone.utc)
        if ed < now:
            is_past = True

    # Resolved-market detection: if the current price is pinned at 0 or 1, the
    # market has already settled regardless of end_date presence.
    if pos.cur_price is not None and pos.cur_price in (0.0, 1.0):
        is_past = True

    return Market(
        condition_id=pos.market_id,
        question=pos.title,
        tokens=tokens,
        active=not is_past,
        closed=is_past,
        volume=0.0,
        end_date=pos.end_date,
        event_slug=pos.event_slug or pos.slug,
        tags=[],
    )


def _position_usd(size: float, avg_price: float) -> float:
    """Approximate dollars a trader has committed to a position.

    Polymarket positions report `size` in shares and `avg_price` in [0,1].
    Cost basis ≈ shares × entry price. When avg_price is missing we fall back to
    raw share count as a rough proxy so the position isn't silently dropped.
    """
    if avg_price and avg_price > 0:
        return size * avg_price
    return size


def _horizon_for(market: Market, short_term_hours: int) -> str:
    """Bucket a market by time-to-resolution: tonight / short / long."""
    if market.end_date is None:
        return "long"
    ed = market.end_date
    if ed.tzinfo is None:
        ed = ed.replace(tzinfo=timezone.utc)
    secs = (ed - datetime.now(timezone.utc)).total_seconds()
    if secs <= 0:
        return "long"
    if secs <= 86_400:
        return "tonight"
    if secs <= short_term_hours * 3600:
        return "short"
    return "long"


def _urgency_mult(market: Market) -> float:
    """Sort multiplier: earlier-resolving markets float higher."""
    if market.end_date is None:
        return 1.0
    ed = market.end_date
    if ed.tzinfo is None:
        ed = ed.replace(tzinfo=timezone.utc)
    secs = (ed - datetime.now(timezone.utc)).total_seconds()
    if secs <= 0:
        return 1.0
    if secs < 86_400:     return 2.0
    if secs < 172_800:    return 1.6
    if secs < 604_800:    return 1.3
    if secs < 2_592_000:  return 1.1
    return 1.0


class LeaderboardAnalyzer:
    """
    Fetches top traders and aggregates their open positions.

    Hedging fix: for each trader in each event, counts how many distinct outcomes
    they hold. If > _MAX_OUTCOMES_PER_EVENT, the trader is treated as hedging:
      - Concentration filter: positions excluded from consensus count
      - Relative size weight: if not excluded, weight = (this position / max position in event)
    """

    def __init__(self, client: PolymarketPublicClient, config: Config) -> None:
        self._client = client
        self._config = config
        self._traders: list[LeaderboardTrader] = []
        self._positions: dict[str, list[TraderPosition]] = {}
        self._extra_markets: dict[str, Market] = {}
        self._starred = StarredTraderStore()
        self._activity_cache: dict[str, list[dict]] = {}  # address → activity rows

    def refresh(self) -> None:
        logger.info("Refreshing leaderboard...")
        new_traders = self._client.get_leaderboard(
            window=self._config.leaderboard_window,
            limit=self._config.leaderboard_top_n * 2,
            min_profit=self._config.leaderboard_min_profit,
            min_volume=self._config.leaderboard_min_volume,
        )[: self._config.leaderboard_top_n]

        if not new_traders:
            if self._traders:
                logger.warning(
                    "Leaderboard fetch failed — reusing %d cached traders from previous scan.",
                    len(self._traders),
                )
            else:
                logger.warning("Leaderboard returned no traders.")
            return  # Keep existing self._traders / self._positions intact

        self._traders = new_traders

        # Mark starred traders
        starred_addrs = self._starred.get_addresses()
        for t in self._traders:
            t.starred = t.address.lower() in starred_addrs

        # Also include starred traders who fell off the leaderboard
        leaderboard_addrs = {t.address.lower() for t in self._traders}
        starred_not_on_lb = starred_addrs - leaderboard_addrs
        if starred_not_on_lb:
            logger.info("Including %d starred traders not on leaderboard.", len(starred_not_on_lb))
            starred_entries = self._starred.get_all()
            for st in starred_entries:
                if st.address.lower() in starred_not_on_lb:
                    self._traders.append(LeaderboardTrader(
                        address=st.address,
                        name=st.name or st.address[:10] + "…",
                        profit=0.0, volume=0.0, num_trades=0,
                        pct_positive=0.0, score=0.0, starred=True,
                    ))

        logger.info("Fetching positions + stats for %d traders (%d starred)...",
                     len(self._traders), sum(1 for t in self._traders if t.starred))
        self._positions = self._fetch_all_positions(self._traders)
        self._extra_markets = {}
        total_pos = sum(len(v) for v in self._positions.values())

        # Enrich traders with real stats (prediction count, win rate, etc.)
        self._enrich_trader_stats(self._traders)

        logger.info("Leaderboard refresh complete — %d open positions across top traders.", total_pos)

    def _fetch_all_positions(
        self, traders: list[LeaderboardTrader]
    ) -> dict[str, list[TraderPosition]]:
        results: dict[str, list[TraderPosition]] = {}
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            futures = {
                pool.submit(self._client.get_trader_positions, t.address): t.address
                for t in traders
            }
            for fut in as_completed(futures):
                addr = futures[fut]
                try:
                    results[addr] = fut.result()
                except Exception as exc:
                    logger.debug("Position fetch failed for %s: %s", addr, exc)
                    results[addr] = []
        return results

    def _enrich_trader_stats(self, traders: list[LeaderboardTrader]) -> None:
        """Fetch real stats (prediction count, win rate) for each trader in parallel.

        The v1 leaderboard API doesn't return numTrades or percentPositive, so
        without this enrichment all traders show 0 trades and grade D. This calls
        v1/user-stats + closed positions per trader and backfills the fields, then
        re-scores everyone.

        Activity data (for the profile chart) is cached in self._activity_cache
        keyed by trader address.
        """
        stats_map: dict[str, TraderStats] = {}
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            futures = {
                pool.submit(self._client.get_trader_stats, t.address): t.address
                for t in traders
            }
            for fut in as_completed(futures):
                addr = futures[fut]
                try:
                    stats_map[addr] = fut.result()
                except Exception as exc:
                    logger.debug("Stats enrichment failed for %s: %s", addr, exc)

        for t in traders:
            st = stats_map.get(t.address)
            if not st:
                continue
            t.num_trades = st.predictions
            t.largest_win = st.largest_win
            t.join_date = st.join_date
            t.closed_positions = st.closed_positions
            t.winning_positions = st.winning_positions
            # Use real win rate from closed positions if we have enough data;
            # fall back to a neutral 0.5 if too few closed positions.
            if st.closed_positions >= 3:
                t.pct_positive = st.win_rate
            elif st.closed_positions > 0:
                # Bayesian shrinkage: blend observed rate toward 0.5 when sample is tiny
                t.pct_positive = (st.win_rate * st.closed_positions + 0.5 * 3) / (st.closed_positions + 3)

            # Cache activity for profile chart
            if st.activity_cache:
                self._activity_cache[t.address] = st.activity_cache

        # Re-score with real data
        self._rescore_traders(traders)
        enriched = sum(1 for t in traders if stats_map.get(t.address))
        logger.info("Enriched %d/%d traders with real stats.", enriched, len(traders))

    @staticmethod
    def _rescore_traders(traders: list[LeaderboardTrader]) -> None:
        """Recompute normalized scores using the same formula as api_client, but
        now with real num_trades and pct_positive values.

        Win rate: uses actual pct_positive when closed_positions >= 3;
        Bayesian-shrunk toward 0.5 for 1-2 closed; 0.5 default only when
        there's genuinely no closed position data.
        """
        if not traders:
            return
        max_profit = max(t.profit for t in traders) or 1.0
        max_vol = max(t.volume for t in traders) or 1.0
        for t in traders:
            profit_norm = t.profit / max_profit
            volume_norm = t.volume / max_vol
            if t.closed_positions >= 3:
                win_rate = t.pct_positive
            elif t.closed_positions > 0:
                win_rate = (t.pct_positive * t.closed_positions + 0.5 * 3) / (t.closed_positions + 3)
            else:
                win_rate = 0.5  # no data
            consistency = min(1.0, math.log10(1 + t.num_trades) / math.log10(51))
            t.score = (
                0.25 * profit_norm * consistency
                + 0.15 * volume_norm
                + 0.30 * win_rate
                + 0.30 * consistency
            )

    def _build_market_lookup(self, markets: list[Market]) -> dict[str, Market]:
        lookup: dict[str, Market] = {m.condition_id: m for m in markets}
        lookup.update(self._extra_markets)

        # Richest position payload per market (largest $ wins), so synthesized
        # markets carry the best title/date/token we have.
        pos_by_mid: dict[str, TraderPosition] = {}
        best_usd: dict[str, float] = {}
        for positions in self._positions.values():
            for pos in positions:
                if not pos.market_id:
                    continue
                usd = _position_usd(pos.size, pos.avg_price)
                if usd >= best_usd.get(pos.market_id, -1.0):
                    best_usd[pos.market_id] = usd
                    pos_by_mid[pos.market_id] = pos

        unknown = [mid for mid in pos_by_mid if mid not in lookup]
        if not unknown:
            return lookup

        # Best-effort Gamma enrichment (full tokens + volume), capped to the
        # biggest positions so the scan stays fast and survives Cloudflare resets.
        unknown.sort(key=lambda mid: best_usd.get(mid, 0.0), reverse=True)
        to_fetch = unknown[:_MAX_GAMMA_ENRICH]
        logger.info("Resolving %d markets from trader positions (%d via Gamma, rest synthesized)...",
                    len(unknown), len(to_fetch))
        try:
            fetched = self._client.get_markets_by_condition_ids(to_fetch)
            for m in fetched:
                if isinstance(m, Market):
                    self._extra_markets[m.condition_id] = m
                    lookup[m.condition_id] = m
        except Exception as exc:
            logger.warning("Extra market fetch failed: %s", exc)

        # Synthesize anything still missing directly from the position payload.
        synth_n = 0
        for mid in unknown:
            if mid not in lookup:
                synth = _market_from_position(pos_by_mid[mid])
                if synth:
                    self._extra_markets[mid] = synth
                    lookup[mid] = synth
                    synth_n += 1
        if synth_n:
            logger.info("Synthesized %d markets directly from trader positions.", synth_n)

        return lookup

    # ── Hedging Detection ────────────────────────────────────────────────────

    def _compute_trader_event_stats(
        self, market_lookup: dict[str, Market]
    ) -> dict[str, dict[str, list[tuple[str, float]]]]:
        """
        For each trader, group their positions by event_key.
        Returns: {trader_address: {event_key: [(market_id, size), ...]}}
        """
        stats: dict[str, dict[str, list[tuple[str, float]]]] = defaultdict(lambda: defaultdict(list))
        for trader in self._traders:
            for pos in self._positions.get(trader.address, []):
                mid = pos.market_id
                if mid not in market_lookup:
                    continue
                market = market_lookup[mid]
                ek = _event_key(market)
                stats[trader.address][ek].append((mid, pos.size))
        return stats

    def _get_conviction_weight(
        self,
        trader_address: str,
        market_id: str,
        position_size: float,
        event_stats: dict[str, dict[str, list[tuple[str, float]]]],
        market_lookup: dict[str, Market],
    ) -> float:
        """
        Returns a 0.0–1.0 conviction weight for this trader's position.
        - 0.0 if the trader is hedging (>MAX_OUTCOMES in this event)
        - Otherwise, position_size / max_position_in_event (relative conviction)
        """
        market = market_lookup.get(market_id)
        if not market:
            return 0.0

        ek = _event_key(market)
        trader_positions_in_event = event_stats.get(trader_address, {}).get(ek, [])

        # Count distinct markets (outcomes) this trader holds in this event
        distinct_markets = len(set(mid for mid, _ in trader_positions_in_event))

        if distinct_markets > _MAX_OUTCOMES_PER_EVENT:
            # Hedging — exclude entirely
            return 0.0

        # Relative size: this position vs their largest in the event
        max_size = max((s for _, s in trader_positions_in_event), default=1.0)
        if max_size <= 0:
            return 1.0
        return min(1.0, position_size / max_size)

    # ── Aggregation ──────────────────────────────────────────────────────────

    def _aggregate(
        self,
        market_lookup: dict[str, Market],
        event_stats: dict[str, dict[str, list[tuple[str, float]]]],
        min_position_usd: float = 0.0,
    ) -> dict[str, dict]:
        """Aggregate trader positions by market, applying conviction weights.

        min_position_usd > 0 drops dust/exploratory positions so only genuine
        conviction bets feed the copy signal.
        """
        agg: dict[str, dict] = {}
        for trader in self._traders:
            for pos in self._positions.get(trader.address, []):
                mid = pos.market_id
                if mid not in market_lookup:
                    continue

                # Skip positions where curPrice indicates the market already settled
                if pos.cur_price is not None and pos.cur_price in (0.0, 1.0):
                    continue

                if min_position_usd > 0 and _position_usd(pos.size, pos.avg_price) < min_position_usd:
                    continue  # too small to signal conviction

                conviction = self._get_conviction_weight(
                    trader.address, mid, pos.size, event_stats, market_lookup
                )
                if conviction <= 0:
                    continue  # hedger — skip entirely

                if mid not in agg:
                    agg[mid] = {"market": market_lookup[mid], "Yes": [], "No": []}
                side = "Yes" if pos.outcome.lower() in ("yes", "1") else "No"
                agg[mid][side].append((trader, pos.size, pos.avg_price, conviction))
        return agg

    def _to_consensus_list(
        self, agg: dict[str, dict], min_dominant: int, short_term_hours: int = 48
    ) -> list[MarketConsensus]:
        trader_lookup = {t.address: t for t in self._traders}
        consensuses: list[MarketConsensus] = []
        now = datetime.now(timezone.utc)

        # Diagnostic (DEBUG level): log every market that reaches this filter so
        # stale/resolved picks can be traced. Enable with LOG_LEVEL=DEBUG.
        # Format: "past-filter: <question> | end_date=<date> | time_category=<cat>"

        for mid, data in agg.items():
            market: Market = data["market"]
            logger.debug(
                "past-filter: %r | end_date=%s | time_category=%s | closed=%s | active=%s",
                market.question, market.end_date, market.time_category, market.closed, market.active,
            )
            if market.time_category == "past" or market.closed or not market.active:
                continue
            # Secondary guard: explicit UTC comparison catches markets where
            # end_date is set but time_category hasn't been re-evaluated since
            # the market expired (e.g. stale synthesized markets).
            if market.end_date is not None:
                ed = market.end_date if market.end_date.tzinfo else market.end_date.replace(tzinfo=timezone.utc)
                if ed < now:
                    continue
            yes_entries = data["Yes"]  # (trader, size, avg_price, conviction)
            no_entries = data["No"]

            # Conviction-weighted scores
            yes_w = sum(t.score * size * conv for t, size, _, conv in yes_entries)
            no_w = sum(t.score * size * conv for t, size, _, conv in no_entries)
            if yes_w + no_w == 0:
                continue

            num_yes = len(yes_entries)
            num_no = len(no_entries)
            if max(num_yes, num_no) < min_dominant:
                continue

            seen: dict[str, LeaderboardTrader] = {}
            for t, _, _, _ in yes_entries + no_entries:
                seen[t.address] = t
            all_traders = list(seen.values())
            total_vol = sum(t.volume for t in all_traders if t.address in trader_lookup)

            yes_stakes = [
                TraderStake(
                    name=t.name or t.address[:10] + "…",
                    address=t.address,
                    score=round(t.score, 4),
                    size=round(size * conv, 2),  # adjusted by conviction
                )
                for t, size, _, conv in sorted(yes_entries, key=lambda x: x[0].score * x[3], reverse=True)
            ]
            no_stakes = [
                TraderStake(
                    name=t.name or t.address[:10] + "…",
                    address=t.address,
                    score=round(t.score, 4),
                    size=round(size * conv, 2),
                )
                for t, size, _, conv in sorted(no_entries, key=lambda x: x[0].score * x[3], reverse=True)
            ]

            con = MarketConsensus(
                market=market,
                yes_weight=yes_w,
                no_weight=no_w,
                num_traders_yes=num_yes,
                num_traders_no=num_no,
                total_volume_backing=total_vol,
                traders=sorted(all_traders, key=lambda t: t.score, reverse=True)[:5],
                yes_stakes=yes_stakes,
                no_stakes=no_stakes,
            )

            # Daily score: urgency × confidence × avg_win_rate × count_factor × 100
            dominant_entries = yes_entries if yes_w >= no_w else no_entries
            avg_win_rate = (
                sum(t.pct_positive for t, _, _, _ in dominant_entries) / len(dominant_entries)
                if dominant_entries else 0.5
            )
            # Average conviction of dominant-side traders
            avg_conviction = (
                sum(conv for _, _, _, conv in dominant_entries) / len(dominant_entries)
                if dominant_entries else 0.0
            )
            count_factor = min((num_yes + num_no) / 5.0, 1.0)
            con.daily_score = round(
                _urgency_mult(market) * con.confidence * avg_win_rate * avg_conviction * count_factor * 100, 1
            )
            con.avg_dominant_win_rate = round(avg_win_rate, 4)
            con.category, con.subcategory = detect_market_category(market)
            con.horizon = _horizon_for(market, short_term_hours)

            # Total real dollars the convicted traders hold on the dominant side.
            dominant_usd = sum(
                _position_usd(size, avg_price) * conv
                for _, size, avg_price, conv in dominant_entries
            )
            con.dominant_position_value = round(dominant_usd, 2)

            # ── Copy-strength score (0–100): how strong is the smart-money signal? ──
            #   how many top traders agree, how good they are, how much they agree,
            #   and how many real dollars they've committed.
            n_dom = len(dominant_entries)
            count_bonus = min(1.0, n_dom / 3.0)            # 3+ traders → full marks
            # The v1 leaderboard often omits percentPositive; fall back to a neutral
            # 0.5 so missing win-rate data doesn't zero out an otherwise strong signal.
            winrate_factor = min(1.0, avg_win_rate) if avg_win_rate > 0 else 0.5
            confidence_factor = con.confidence             # one-sidedness 0–1
            dollar_factor = min(1.0, math.log10(1.0 + dominant_usd) / 4.0)  # ~$10k → 1.0
            con.copy_score = round(
                100.0 * (
                    0.35 * count_bonus
                    + 0.25 * winrate_factor
                    + 0.20 * confidence_factor
                    + 0.20 * dollar_factor
                ),
                1,
            )

            consensuses.append(con)

        return sorted(consensuses, key=lambda c: c.confidence * c.dominant_weight, reverse=True)

    def build_consensus(self, markets: list[Market]) -> list[MarketConsensus]:
        """Markets where >= leaderboard_min_traders agree (with hedging filter applied)."""
        if not self._traders:
            return []
        lookup = self._build_market_lookup(markets)
        event_stats = self._compute_trader_event_stats(lookup)
        agg = self._aggregate(lookup, event_stats)
        return self._to_consensus_list(agg, min_dominant=self._config.leaderboard_min_traders)

    def build_picks(self, markets: list[Market]) -> list[MarketConsensus]:
        """All markets with any convicted (non-hedging) trader position."""
        if not self._traders:
            return []
        lookup = self._build_market_lookup(markets)
        now = datetime.now(timezone.utc)
        lookup = {
    