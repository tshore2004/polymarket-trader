"""Tests for LeaderboardAnalyzer — no network required."""
from __future__ import annotations
import pytest
from unittest.mock import MagicMock
from utils.models import (
    Market, Token, LeaderboardTrader, TraderPosition, MarketConsensus, Side
)
from core.leaderboard import LeaderboardAnalyzer


def _config(min_traders=3):
    cfg = MagicMock()
    cfg.leaderboard_min_traders = min_traders
    return cfg


def _market(condition_id: str = "cond1") -> Market:
    return Market(
        condition_id=condition_id,
        question=f"Market {condition_id}?",
        tokens=[Token("yes_tok", "Yes"), Token("no_tok", "No")],
        volume=10_000.0,
    )


def _trader(address: str, score: float = 0.5, volume: float = 50_000.0) -> LeaderboardTrader:
    return LeaderboardTrader(
        address=address,
        name=f"Trader {address}",
        profit=1000.0,
        volume=volume,
        num_trades=100,
        pct_positive=0.6,
        score=score,
    )


def _pos(address: str, market_id: str, outcome: str, size: float = 100.0) -> TraderPosition:
    return TraderPosition(
        trader_address=address,
        market_id=market_id,
        outcome=outcome,
        size=size,
        avg_price=0.5,
    )


# ── score() ──────────────────────────────────────────────────────────────────

class TestLeaderboardScore:
    def _analyzer(self):
        return LeaderboardAnalyzer(MagicMock(), _config())

    def _consensus(self, yes_w, no_w, num_yes, num_no) -> MarketConsensus:
        return MarketConsensus(
            market=_market(),
            yes_weight=yes_w,
            no_weight=no_w,
            num_traders_yes=num_yes,
            num_traders_no=num_no,
            total_volume_backing=50_000.0,
        )

    def test_score_within_0_to_30(self):
        # Leaderboard component is now a 0–30 band.
        con = self._consensus(0.9, 0.1, 10, 2)
        score = self._analyzer().score(con)
        assert 0.0 <= score <= 30.0

    def test_score_more_traders_scores_higher(self):
        few = self._consensus(0.8, 0.2, 2, 0)
        many = self._consensus(0.8, 0.2, 6, 0)
        a = self._analyzer()
        assert a.score(many) >= a.score(few)

    def test_score_higher_confidence_scores_higher(self):
        low = self._consensus(0.6, 0.4, 4, 0)   # confidence 0.2
        high = self._consensus(1.0, 0.0, 4, 0)  # confidence 1.0
        a = self._analyzer()
        assert a.score(high) > a.score(low)

    def test_score_zero_confidence(self):
        con = self._consensus(0.5, 0.5, 5, 5)
        assert self._analyzer().score(con) == 0.0

    def test_score_zero_weights(self):
        con = self._consensus(0.0, 0.0, 0, 0)
        assert self._analyzer().score(con) == 0.0


# ── build_consensus() ────────────────────────────────────────────────────────

class TestBuildConsensus:
    def _analyzer_with_data(self, traders, positions_by_addr):
        a = LeaderboardAnalyzer(MagicMock(), _config(min_traders=2))
        a._traders = traders
        a._positions = positions_by_addr
        return a

    def test_consensus_yes_dominant(self):
        m = _market("cond1")
        traders = [_trader(f"t{i}", score=0.8) for i in range(4)]
        positions = {
            "t0": [_pos("t0", "cond1", "Yes", 100)],
            "t1": [_pos("t1", "cond1", "Yes", 100)],
            "t2": [_pos("t2", "cond1", "Yes", 100)],
            "t3": [_pos("t3", "cond1", "No", 50)],
        }
        a = self._analyzer_with_data(traders, positions)
        results = a.build_consensus([m])

        assert len(results) == 1
        c = results[0]
        assert c.dominant_side == Side.YES
        assert c.num_traders_yes == 3
        assert c.num_traders_no == 1
        assert c.yes_weight > c.no_weight

    def test_consensus_filters_by_min_traders(self):
        m = _market("cond1")
        traders = [_trader("t0", score=0.9), _trader("t1", score=0.8)]
        positions = {
            "t0": [_pos("t0", "cond1", "Yes", 100)],
            "t1": [_pos("t1", "cond1", "Yes", 100)],
        }
        # min_traders=3, only 2 on dominant side → filtered out
        a = LeaderboardAnalyzer(MagicMock(), _config(min_traders=3))
        a._traders = traders
        a._positions = positions
        results = a.build_consensus([m])
        assert results == []

    def test_consensus_skips_unknown_markets(self):
        m = _market("cond1")
        traders = [_trader("t0"), _trader("t1"), _trader("t2")]
        positions = {
            "t0": [_pos("t0", "OTHER_MARKET", "Yes", 100)],
            "t1": [_pos("t1", "OTHER_MARKET", "Yes", 100)],
            "t2": [_pos("t2", "OTHER_MARKET", "Yes", 100)],
        }
        a = self._analyzer_with_data(traders, positions)
        results = a.build_consensus([m])
        assert results == []

    def test_consensus_no_traders_returns_empty(self):
        a = LeaderboardAnalyzer(MagicMock(), _config())
        a._traders = []
        a._positions = {}
        assert a.build_consensus([_market()]) == []

    def test_consensus_weight_is_score_times_size(self):
        m = _market("cond1")
        t0 = _trader("t0", score=1.0)
        t1 = _trader("t1", score=0.5)
        t2 = _trader("t2", score=0.5)
        positions = {
            "t0": [_pos("t0", "cond1", "Yes", 200)],  # weight = 1.0 * 200 = 200
            "t1": [_pos("t1", "cond1", "Yes", 100)],  # weight = 0.5 * 100 = 50
            "t2": [_pos("t2", "cond1", "No",  100)],  # weight = 0.5 * 100 = 50
        }
        a = LeaderboardAnalyzer(MagicMock(), _config(min_traders=2))
        a._traders = [t0, t1, t2]
        a._positions = positions
        results = a.build_consensus([m])
        assert len(results) == 1
        c = results[0]
        assert abs(c.yes_weight - 250.0) < 1e-9
        assert abs(c.no_weight - 50.0) < 1e-9

    def test_consensus_sorted_by_confidence_times_weight(self):
        m1, m2 = _market("cond1"), _market("cond2")
        traders = [_trader(f"t{i}", score=0.8) for i in range(8)]
        positions = {
            # cond1: 4 YES, 0 NO — very confident but lower weight
            "t0": [_pos("t0", "cond1", "Yes", 50)],
            "t1": [_pos("t1", "cond1", "Yes", 50)],
            "t2": [_pos("t2", "cond1", "Yes", 50)],
            "t3": [_pos("t3", "cond1", "Yes", 50)],
            # cond2: 4 YES, 0 NO — same structure with larger size
            "t4": [_pos("t4", "cond2", "Yes", 500)],
            "t5": [_pos("t5", "cond2", "Yes", 500)],
            "t6": [_pos("t6", "cond2", "Yes", 500)],
            "t7": [_pos("t7", "cond2", "No",  200)],
        }
        a = LeaderboardAnalyzer(MagicMock(), _config(min_traders=2))
        a._traders = traders
        a._positions = positions
        results = a.build_consensus([m1, m2])
        assert len(results) == 2
        # higher confidence*weight should come first
        first_key = results[0].confidence * results[0].dominant_weight
        second_key = results[1].confidence * results[1].dominant_weight
        assert first_key >= second_key
