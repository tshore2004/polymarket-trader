from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Side(str, Enum):
    YES = "YES"
    NO = "NO"
    BOTH = "BOTH"   # arb: buy both sides


class SignalType(str, Enum):
    LEADERBOARD = "LEADERBOARD"
    FAIR_VALUE = "FAIR_VALUE"
    NEWS = "NEWS"
    VOLUME_SPIKE = "VOLUME_SPIKE"
    MULTI = "MULTI"  # multiple factors contributed


@dataclass
class Token:
    token_id: str
    outcome: str   # "Yes" or "No"
    price: float = 0.0


@dataclass
class Market:
    condition_id: str
    question: str
    tokens: list[Token]
    active: bool = True
    closed: bool = False
    volume: float = 0.0
    end_date: Optional[datetime] = None
    event_slug: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def time_category(self) -> str:
        if self.end_date is None:
            return "ongoing"
        ed = self.end_date
        if ed.tzinfo is None:
            ed = ed.replace(tzinfo=timezone.utc)
        secs = (ed - datetime.now(timezone.utc)).total_seconds()
        if secs < 0:
            return "past"
        if secs < 86_400:
            return "tonight"
        if secs < 172_800:
            return "tomorrow"
        if secs < 604_800:
            return "this_week"
        if secs < 2_592_000:
            return "this_month"
        return "later"

    @property
    def urgency_score(self) -> float:
        if self.end_date is None:
            return 0.0
        ed = self.end_date
        if ed.tzinfo is None:
            ed = ed.replace(tzinfo=timezone.utc)
        secs = (ed - datetime.now(timezone.utc)).total_seconds()
        if secs <= 0:
            return 0.0
        if secs < 86_400:
            return 1.0
        if secs < 172_800:
            return 0.8
        if secs < 604_800:
            return 0.5
        if secs < 2_592_000:
            return 0.2
        return 0.0

    @property
    def yes_token(self) -> Optional[Token]:
        for t in self.tokens:
            if t.outcome.lower() in ("yes", "1"):
                return t
        return self.tokens[0] if self.tokens else None

    @property
    def no_token(self) -> Optional[Token]:
        for t in self.tokens:
            if t.outcome.lower() in ("no", "0"):
                return t
        return self.tokens[1] if len(self.tokens) > 1 else None


@dataclass
class OrderLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: list[OrderLevel] = field(default_factory=list)
    asks: list[OrderLevel] = field(default_factory=list)

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def ask_liquidity(self) -> float:
        return sum(l.size for l in self.asks)

    @property
    def bid_liquidity(self) -> float:
        return sum(l.size for l in self.bids)


@dataclass
class LeaderboardTrader:
    address: str
    name: str
    profit: float
    volume: float
    num_trades: int          # predictions count (distinct markets traded)
    pct_positive: float      # win rate 0.0-1.0 from closed positions
    score: float = 0.0       # normalized 0-1 composite score
    starred: bool = False    # user has starred this trader for tracking
    largest_win: float = 0.0
    join_date: Optional[datetime] = None
    closed_positions: int = 0
    winning_positions: int = 0

    @property
    def profit_per_trade(self) -> float:
        return self.profit / self.num_trades if self.num_trades > 0 else 0.0

    @property
    def consistency_grade(self) -> str:
        """Letter grade reflecting trader quality.

        Requirements for each grade:
        - A: ≥10 closed positions AND combined ≥ 0.75
        - B: ≥5 closed positions AND combined ≥ 0.55
        - C: combined ≥ 0.35 (or insufficient data)
        - D: everything else

        Win rate uses actual pct_positive; only falls back to 0.5 if we
        truly have no closed position data (closed_positions == 0).
        """
        import math
        trade_score = min(1.0, math.log10(1 + self.num_trades) / math.log10(51))

        # Use real win rate when we have closed position data;
        # only default to 0.5 when there's genuinely no data.
        if self.closed_positions >= 3:
            wr = self.pct_positive  # trust the real rate
        elif self.closed_positions > 0:
            # Bayesian shrinkage toward 0.5 for tiny samples
            wr = (self.pct_positive * self.closed_positions + 0.5 * 3) / (self.closed_positions + 3)
        else:
            wr = 0.5  # no data — neutral assumption

        combined = 0.5 * trade_score + 0.5 * wr

        # Enforce minimum closed positions for top grades
        if combined >= 0.75 and self.closed_positions >= 10:
            return "A"
        if combined >= 0.55 and self.closed_positions >= 5:
            return "B"
        if combined >= 0.35:
            return "C"
        return "D"


@dataclass
class TraderStats:
    """Enriched trader quality data from v1/user-stats + activity/positions."""
    address: str
    predictions: int = 0
    largest_win: float = 0.0
    join_date: Optional[datetime] = None
    closed_positions: int = 0       # wins + losses
    winning_positions: int = 0      # unique markets with REDEEM events
    losing_positions: int = 0       # unique markets resolved as losses
    total_closed_pnl: float = 0.0
    activity_cache: list = field(default_factory=list)  # raw activity for chart

    @property
    def win_rate(self) -> float:
        if self.closed_positions == 0:
            return 0.0
        return self.winning_positions / self.closed_positions

    @property
    def days_active(self) -> int:
        if self.join_date is None:
            return 0
        jd = self.join_date if self.join_date.tzinfo else self.join_date.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - jd).days)


@dataclass
class TraderPosition:
    trader_address: str
    market_id: str
    outcome: str
    size: float
    avg_price: float
    current_value: float = 0.0
    title: str = ""
    slug: str = ""
    event_slug: str = ""
    end_date: Optional[datetime] = None
    token_id: str = ""
    opposite_token_id: str = ""
    cur_price: float = 0.0


@dataclass
class TraderStake:
    name: str
    address: str
    score: float
    size: float


@dataclass
class MarketConsensus:
    market: Market
    yes_weight: float
    no_weight: float
    num_traders_yes: int
    num_traders_no: int
    total_volume_backing: float
    traders: list[LeaderboardTrader] = field(default_factory=list)
    yes_stakes: list[TraderStake] = field(default_factory=list)
    no_stakes: list[TraderStake] = field(default_factory=list)
    daily_score: float = 0.0
    category: str = ""
    subcategory: str = ""
    avg_dominant_win_rate: float = 0.0
    horizon: str = ""
    dominant_position_value: float = 0.0
    copy_score: float = 0.0

    @property
    def dominant_side(self) -> Side:
        return Side.YES if self.yes_weight >= self.no_weight else Side.NO

    @property
    def confidence(self) -> float:
        total = self.yes_weight + self.no_weight
        if total == 0:
            return 0.0
        return abs(self.yes_weight - self.no_weight) / total

    @property
    def dominant_weight(self) -> float:
        return max(self.yes_weight, self.no_weight)

    @property
    def num_traders_dominant(self) -> int:
        if self.yes_weight >= self.no_weight:
            return self.num_traders_yes
        return self.num_traders_no

    @property
    def dominant_stakes(self) -> list["TraderStake"]:
        return self.yes_stakes if self.yes_weight >= self.no_weight else self.no_stakes


@dataclass
class ScoreBreakdown:
    leaderboard: float = 0.0
    fair_value_edge: float = 0.0
    line_movement: float = 0.0
    news_momentum: float = 0.0
    urgency: float = 0.0

    @property
    def total(self) -> float:
        return min(
            100.0,
            self.leaderboard + self.fair_value_edge
            + self.line_movement + self.news_momentum + 