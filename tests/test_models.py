"""Tests for data model properties — no network required."""
from __future__ import annotations
import pytest
from utils.models import (
    Token, Market, OrderBook, OrderLevel,
    MarketConsensus, Side, ScoreBreakdown,
)


def _market(*outcomes) -> Market:
    tokens = [Token(token_id=f"tid_{o}", outcome=o) for o in outcomes]
    return Market(condition_id="c1", question="Q?", tokens=tokens)


# ── Market.yes_token / no_token ───────────────────────────────────────────────

class TestMarketTokens:
    def test_yes_token_by_outcome_label(self):
        m = _market("Yes", "No")
        assert m.yes_token.outcome == "Yes"

    def test_no_token_by_outcome_label(self):
        m = _market("Yes", "No")
        assert m.no_token.outcome == "No"

    def test_yes_token_case_insensitive(self):
        m = _market("YES", "NO")
        assert m.yes_token is not None
        assert m.yes_token.outcome == "YES"

    def test_no_token_case_insensitive(self):
        m = _market("YES", "NO")
        assert m.no_token is not None
        assert m.no_token.outcome == "NO"

    def test_yes_token_numeric_outcome(self):
        m = _market("1", "0")
        assert m.yes_token.outcome == "1"

    def test_no_token_numeric_outcome(self):
        m = _market("1", "0")
        assert m.no_token.outcome == "0"

    def test_yes_token_fallback_to_first(self):
        # Unrecognised labels → fallback to index 0 for yes
        m = _market("Win", "Lose")
        assert m.yes_token.outcome == "Win"

    def test_no_token_fallback_to_second(self):
        m = _market("Win", "Lose")
        assert m.no_token.outcome == "Lose"

    def test_yes_token_empty_tokens_returns_none(self):
        m = Market(condition_id="c", question="Q?", tokens=[])
        assert m.yes_token is None

    def test_no_token_single_token_returns_none(self):
        m = Market(condition_id="c", question="Q?", tokens=[Token("t", "Yes")])
        assert m.no_token is None


# ── OrderBook ─────────────────────────────────────────────────────────────────

class TestOrderBook:
    def test_best_ask_returns_lowest_ask(self):
        book = OrderBook(
            token_id="t",
            asks=[OrderLevel(0.52, 10), OrderLevel(0.50, 5), OrderLevel(0.55, 8)],
        )
        # asks are sorted ascending in api_client; test the property directly
        book.asks.sort(key=lambda x: x.price)
        assert book.best_ask == 0.50

    def test_best_bid_returns_highest_bid(self):
        book = OrderBook(
            token_id="t",
            bids=[OrderLevel(0.48, 10), OrderLevel(0.45, 5), OrderLevel(0.50, 8)],
        )
        book.bids.sort(key=lambda x: -x.price)
        assert book.best_bid == 0.50

    def test_best_ask_none_when_empty(self):
        book = OrderBook(token_id="t")
        assert book.best_ask is None

    def test_best_bid_none_when_empty(self):
        book = OrderBook(token_id="t")
        assert book.best_bid is None

    def test_ask_liquidity_sum(self):
        book = OrderBook(
            token_id="t",
            asks=[OrderLevel(0.50, 10), OrderLevel(0.51, 20)],
        )
        assert book.ask_liquidity == 30.0

    def test_bid_liquidity_sum(self):
        book = OrderBook(
            token_id="t",
            bids=[OrderLevel(0.49, 15), OrderLevel(0.48, 5)],
        )
        assert book.bid_liquidity == 20.0


# ── ScoreBreakdown ────────────────────────────────────────────────────────────

class TestScoreBreakdown:
    def test_total_sums_all_factors(self):
        s = ScoreBreakdown(leaderboard=20, fair_value_edge=15, line_movement=10, news_momentum=5, urgency=8)
        assert s.total == 58.0

    def test_total_capped_at_100(self):
        s = ScoreBreakdown(leaderboard=30, fair_value_edge=30, line_movement=20, news_momentum=10, urgency=10)
        assert s.total == 100.0

    def test_explain_only_shows_nonzero(self):
        s = ScoreBreakdown(leaderboard=20, fair_value_edge=0, line_movement=10)
        explanation = s.explain()
        assert "Leaderboard" in explanation
        assert "Momentum" in explanation
        assert "Edge" not in explanation


# ── MarketConsensus ───────────────────────────────────────────────────────────

class TestMarketConsensus:
    def _consensus(self, yes_w: float, no_w: float, ny: int, nn: int) -> MarketConsensus:
        return MarketConsensus(
            market=_market("Yes", "No"),
            yes_weight=yes_w,
            no_weight=no_w,
            num_traders_yes=ny,
            num_traders_no=nn,
            total_volume_backing=50_000.0,
        )

    def test_dominant_side_yes(self):
        c = self._consensus(0.8, 0.2, 5, 2)
        assert c.dominant_side == Side.YES

    def test_dominant_side_no(self):
        c = self._consensus(0.3, 0.7, 2, 5)
        assert c.dominant_side == Side.NO

    def test_confidence_lopsided(self):
        c = self._consensus(0.9, 0.1, 5, 1)
        # (0.9 - 0.1) / 1.0 = 0.8
        assert abs(c.confidence - 0.8) < 1e-9

    def test_confidence_equal_weights(self):
        c = self._consensus(0.5, 0.5, 3, 3)
        assert c.confidence == 0.0

    def test_confidence_zero_weights(self):
        c = self._consensus(0.0, 0.0, 0, 0)
        assert c.confidence == 0.0

    def test_num_traders_dominant_yes(self):
        c = self._consensus(0.8, 0.2, 5, 2)
        assert c.num_traders_dominant == 5

    def test_num_traders_dominant_no(self):
        c = self._consensus(0.2, 0.8, 2, 7)
        assert c.num_traders_dominant == 7
