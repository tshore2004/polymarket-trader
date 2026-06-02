"""Offline tests for the smart-money copy-trade strategy (no network)."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from utils.models import LeaderboardTrader, TraderPosition, Side
from core.leaderboard import (
    LeaderboardAnalyzer, _market_from_position, _horizon_for, _position_usd,
)

NOW = datetime.now(timezone.utc)


def _cfg():
    cfg = MagicMock()
    cfg.leaderboard_min_traders = 2
    return cfg


def _trader(addr, score=0.8):
    return LeaderboardTrader(addr, addr, 1e6, 5e6, 100, 0.0, score=score)


def _tonight_pos(addr):
    return TraderPosition(
        trader_address=addr, market_id="gameTonight", outcome="Yes", size=400, avg_price=0.5,
        title="Lakers beat Celtics tonight?", end_date=NOW + timedelta(hours=5),
        token_id="tok_" + addr, opposite_token_id="opp_" + addr, cur_price=0.55,
    )


def _long_pos(addr):
    return TraderPosition(
        trader_address=addr, market_id="champ2026", outcome="No", size=2000, avg_price=0.6,
        title="Will Team X win the 2026 championship?", end_date=NOW + timedelta(days=40),
        token_id="ltok_" + addr, opposite_token_id="lopp_" + addr,
    )


def _analyzer(positions):
    a = LeaderboardAnalyzer(MagicMock(), _cfg())
    a._traders = [_trader("0xA"), _trader("0xB"), _trader("0xC")]
    a._positions = positions
    # Force the Gamma enrichment path to fail so synthesis is exercised.
    a._client.get_markets_by_condition_ids = MagicMock(side_effect=Exception("cf reset"))
    return a


class TestHelpers:
    def test_position_usd_uses_cost_basis(self):
        assert _position_usd(400, 0.5) == 200.0

    def test_position_usd_falls_back_to_shares(self):
        assert _position_usd(400, 0.0) == 400.0

    def test_horizon_tonight(self):
        m = _market_from_position(_tonight_pos("0xA"))
        assert _horizon_for(m, 48) == "tonight"

    def test_horizon_long(self):
        m = _market_from_position(_long_pos("0xA"))
        assert _horizon_for(m, 48) == "long"

    def test_market_from_position_has_both_tokens(self):
        m = _market_from_position(_tonight_pos("0xA"))
        assert m is not None
        assert m.yes_token.token_id == "tok_0xA"
        assert m.no_token.token_id == "opp_0xA"

    def test_market_from_position_requires_title(self):
        pos = _tonight_pos("0xA")
        pos.title = ""
        assert _market_from_position(pos) is None


class TestCopyPicks:
    def test_picks_built_from_positions_only(self):
        # No markets passed in — universe comes entirely from positions (synthesized).
        a = _analyzer({
            "0xA": [_tonight_pos("0xA"), _long_pos("0xA")],
            "0xB": [_tonight_pos("0xB"), _long_pos("0xB")],
            "0xC": [_tonight_pos("0xC")],
        })
        picks = a.build_copy_picks([], min_position_usd=100.0, short_term_hours=48)
        assert len(picks) == 2
        horizons = {p.horizon for p in picks}
        assert horizons == {"tonight", "long"}
        # Both markets were synthesized straight from positions
        assert len(a.held_markets()) == 2

    def test_min_position_usd_filters_dust(self):
        dust = TraderPosition(
            trader_address="0xA", market_id="dust", outcome="Yes", size=10, avg_price=0.5,
            title="Tiny market?", end_date=NOW + timedelta(hours=5),
        )  # $5 position — below the $100 conviction floor
        a = _analyzer({"0xA": [dust]})
        picks = a.build_copy_picks([], min_position_usd=100.0, short_term_hours=48)
        assert picks == []

    def test_copy_score_rewards_more_traders(self):
        three = _analyzer({
            "0xA": [_tonight_pos("0xA")],
            "0xB": [_tonight_pos("0xB")],
            "0xC": [_tonight_pos("0xC")],
        }).build_copy_picks([], min_position_usd=50.0)
        one = _analyzer({"0xA": [_tonight_pos("0xA")]}).build_copy_picks([], min_position_usd=50.0)
        assert three[0].copy_score > one[0].copy_score

    def test_dominant_side_and_value(self):
        a = _analyzer({
            "0xA": [_tonight_pos("0xA")],
            "0xB": [_tonight_pos("0xB")],
        })
        picks = a.build_copy_picks([], min_position_usd=50.0)
        p = picks[0]
        assert p.dominant_side == Side.YES
        # 2 traders × 400 shares × $0.50 = ~$400 committed
        assert p.dominant_position_value > 300
        assert len(p.dominant_stakes) == 2
