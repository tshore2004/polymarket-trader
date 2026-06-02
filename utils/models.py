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
    event_slug: str = ""          # event grouping key (e.g. "2026-world-cup")
    tags: list[str] = field(default_factory=list)

    @property
    def time_category(self) -> str:
        """Returns the timing bucket for display: tonight/tomorrow/this_week/this_month/later/ongoing."""
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
        """0.0–1.0 boost weight: higher for markets resolving sooner."""
        if self.end_date is None:
            return 0.0
        ed = self.end_date
        if ed.tzinfo is None:
            ed = ed.replace(tzinfo=timezone.utc)
        secs = (ed - datetime.now(timezone.utc)).total_seconds()
        if secs <= 0:
            return 0.0
        if secs < 86_400:
            return 1.0   # tonight
        if secs < 172_800:
            return 0.8   # tomorrow
        if secs < 604_800:
            return 0.5   # this week
        if secs < 2_592_000:
            return 0.2   # this month
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
    num_trades: int
    pct_positive: float
    score: float = 0.0     # normalized 0–1 composite score


@dataclass
class TraderPosition:
    trader_address: str
    market_id: str
    outcome: str
    size: float
    avg_price: float
    current_value: float = 0.0
    # Extra fields carried straight from the /positions payload so we can resolve
    # the market without a fragile Gamma round-trip (the old code dropped these,
    # which is why top-trader positions silently failed to become picks).
    title: str = ""
    slug: str = ""
    event_slug: str = ""
    end_date: Optional[datetime] = None
    token_id: str = ""              # the held outcome's CLOB token id ("asset")
    opposite_token_id: str = ""     # the other outcome's token id ("oppositeAsset")
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
    yes_weight: float         # sum of trader scores backing YES
    no_weight: float          # sum of trader scores backing NO
    num_traders_yes: int
    num_traders_no: int
    total_volume_backing: float
    traders: list[LeaderboardTrader] = field(default_factory=list)
    yes_stakes: list[TraderStake] = field(default_factory=list)
    no_stakes: list[TraderStake] = field(default_factory=list)
    daily_score: float = 0.0              # urgency × confidence × win_rate × count_factor × 100
    category: str = ""                    # e.g. "sports", "politics", "crypto"
    subcategory: str = ""                 # e.g. "nfl", "nba", "soccer"
    avg_dominant_win_rate: float = 0.0    # average pct_positive of traders on dominant side
    horizon: str = ""                     # "tonight" | "short" | "long" — copy-trade time bucket
    dominant_position_value: float = 0.0  # total $ the convicted traders hold on the dominant side
    copy_score: float = 0.0               # 0–100 strength of the smart-money copy signal

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
        """The per-trader stakes backing the dominant side (largest first)."""
        return self.yes_stakes if self.yes_weight >= self.no_weight else self.no_stakes


@dataclass
class ScoreBreakdown:
    """Individual factor scores that compose the final signal score."""
    leaderboard: float = 0.0       # 0–30: conviction-weighted consensus
    fair_value_edge: float = 0.0   # 0–30: mispricing vs external odds
    line_movement: float = 0.0     # 0–20: volume spikes + price momentum
    news_momentum: float = 0.0     # 0–10: trending news relevance
    urgency: float = 0.0           # 0–10: sooner resolution = higher

    @property
    def total(self) -> float:
        return min(100.0, self.leaderboard + self.fair_value_edge +
                   self.line_movement + self.news_momentum + self.urgency)

    def explain(self) -> str:
        parts = []
        if self.leaderboard > 0:
            parts.append(f"Leaderboard {self.leaderboard:.0f}/30")
        if self.fair_value_edge > 0:
            parts.append(f"Edge {self.fair_value_edge:.0f}/30")
        if self.line_movement > 0:
            parts.append(f"Momentum {self.line_movement:.0f}/20")
        if self.news_momentum > 0:
            parts.append(f"News {self.news_momentum:.0f}/10")
        if self.urgency > 0:
            parts.append(f"Urgency {self.urgency:.0f}/10")
        return " | ".join(parts)


@dataclass
class Signal:
    market: Market
    combined_score: float     # 0–100
    signal_type: SignalType
    recommended_side: Side
    recommended_price: float
    scores: ScoreBreakdown = field(default_factory=ScoreBreakdown)
    consensus: Optional[MarketConsensus] = None
    explanation: str = ""
    fair_value: Optional[float] = None   # external implied probability, if available
    edge_pct: float = 0.0               # polymarket price vs fair value difference


@dataclass
class TradeResult:
    success: bool
    market_question: str
    side: str
    price: float
    size: float
    order_id: Optional[str] = None
    error: Optional[str] = None
