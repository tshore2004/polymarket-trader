"""Tests for sports market scoring (Kalshi tab) and arb matching (Arbitrage tab)."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from core.arbitrage import (
    CrossPlatformArbScanner,
    _cities_from_text,
    _extract_vs_sides,
    _sports_match_confidence,
)
from utils.models import KalshiMarket, Market, Token


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now_plus(hours: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _kalshi_market(
    ticker="KXMLBGAME-CHC-STL",
    title="Chicago Cubs vs St. Louis Cardinals",
    yes_sub_title="Chicago C",
    category="Baseball",
    yes_price=0.55,
    no_price=0.47,
    close_time_hours: float | None = 12.0,
) -> KalshiMarket:
    return KalshiMarket(
        ticker=ticker,
        title=title,
        category=category,
        yes_price=yes_price,
        no_price=no_price,
        volume=1000.0,
        close_time=_now_plus(close_time_hours) if close_time_hours is not None else None,
        yes_sub_title=yes_sub_title,
    )


def _poly_market(
    question="Will the Chicago Cubs win?",
    yes_outcome="Chicago Cubs",
    no_outcome="No",
    yes_price=0.40,
    no_price=0.62,
    end_hours: float | None = 12.0,
) -> Market:
    tokens = [
        Token(token_id="yes_tok", outcome=yes_outcome, price=yes_price),
        Token(token_id="no_tok", outcome=no_outcome, price=no_price),
    ]
    return Market(
        condition_id="poly_cond_1",
        question=question,
        tokens=tokens,
        active=True,
        closed=False,
        volume=5000.0,
        end_date=_now_plus(end_hours) if end_hours is not None else None,
    )


def _config():
    cfg = MagicMock()
    cfg.arb_match_threshold = 0.45
    cfg.arb_min_roi = 0.01
    cfg.arb_soft_min_edge = 0.04
    cfg.fee_rate = 0.02
    return cfg


# ── KalshiMarket urgency ──────────────────────────────────────────────────────

class TestKalshiMarketUrgency:
    def test_urgency_zero_when_no_close_time(self):
        km = _kalshi_market(close_time_hours=None)
        assert km.urgency_score == 0.0

    def test_urgency_one_within_24h(self):
        km = _kalshi_market(close_time_hours=6)
        assert km.urgency_score == 1.0

    def test_urgency_08_within_48h(self):
        km = _kalshi_market(close_time_hours=30)
        assert km.urgency_score == 0.8

    def test_urgency_05_within_week(self):
        km = _kalshi_market(close_time_hours=100)
        assert km.urgency_score == 0.5

    def test_time_category_tonight(self):
        km = _kalshi_market(close_time_hours=6)
        assert km.time_category == "tonight"

    def test_time_category_tomorrow(self):
        km = _kalshi_market(close_time_hours=30)
        assert km.time_category == "tomorrow"

    def test_time_category_past(self):
        km = _kalshi_market(close_time_hours=-1)
        assert km.time_category == "past"

    def test_time_category_ongoing_when_no_close_time(self):
        km = _kalshi_market(close_time_hours=None)
        assert km.time_category == "ongoing"


# ── Sports market scoring threshold ──────────────────────────────────────────

class TestSportsMarketScoreThreshold:
    def test_urgency_alone_exceeds_threshold(self):
        """Urgency from close_time within 24h = 15.0 > 2.0 threshold."""
        km = _kalshi_market(close_time_hours=6)
        urgency_score = km.urgency_score * 15.0
        assert urgency_score >= 2.0

    def test_no_close_time_urgency_zero(self):
        km = _kalshi_market(close_time_hours=None)
        assert km.urgency_score * 15.0 == 0.0

    def test_this_week_urgency_still_above_threshold(self):
        km = _kalshi_market(close_time_hours=100)  # ~4 days out
        assert km.urgency_score * 15.0 >= 2.0


# ── City extraction ───────────────────────────────────────────────────────────

class TestCitiesFromText:
    def test_standard_vs_format(self):
        cities = _cities_from_text("Chicago Cubs vs St. Louis Cardinals")
        assert len(cities) >= 2
        assert "chicago" in cities

    def test_binary_question_no_vs(self):
        # "Will the Cubs win?" has no "vs" — confirms the gap this fix addresses
        cities = _cities_from_text("Will the Cubs win?")
        assert len(cities) < 2

    def test_multi_word_city(self):
        cities = _cities_from_text("New York Yankees vs Boston Red Sox")
        assert "new york" in cities
        assert "boston" in cities

    def test_kalshi_abbreviated_teams(self):
        sides = _extract_vs_sides("Chicago C vs St. Louis C")
        assert sides is not None
        assert "Chicago C" in sides[0] or "Chicago C" in sides[1]

    def test_sports_confidence_matching_cities(self):
        conf = _sports_match_confidence(
            "Chicago Cubs vs St. Louis Cardinals",
            "Chicago C vs St. Louis C",
        )
        assert conf >= 0.85

    def test_sports_confidence_non_matching(self):
        conf = _sports_match_confidence(
            "New York Yankees vs Boston Red Sox",
            "Chicago Cubs vs St. Louis Cardinals",
        )
        assert conf == 0.0


# ── Single-team arb matching ──────────────────────────────────────────────────

class TestSingleTeamMatching:
    def test_build_token_city_index_indexes_yes_outcomes(self):
        scanner = CrossPlatformArbScanner(_config())
        pm = _poly_market(yes_outcome="Chicago Cubs")
        index = scanner._build_token_city_index([pm])
        assert "chicago" in index
        assert pm in index["chicago"]

    def test_build_token_city_index_skips_yes_no_outcomes(self):
        scanner = CrossPlatformArbScanner(_config())
        pm = _poly_market(yes_outcome="Yes", no_outcome="No")
        index = scanner._build_token_city_index([pm])
        assert len(index) == 0

    def test_single_team_match_finds_by_city(self):
        scanner = CrossPlatformArbScanner(_config())
        pm = _poly_market(yes_outcome="Chicago Cubs", end_hours=12)
        index = scanner._build_token_city_index([pm])
        km = _kalshi_market(yes_sub_title="Chicago C", close_time_hours=12)
        match, conf = scanner._best_sports_match_single_team(km, index)
        assert match is not None
        assert match.condition_id == pm.condition_id
        assert conf >= 0.70

    def test_single_team_match_boosts_confidence_when_times_close(self):
        scanner = CrossPlatformArbScanner(_config())
        pm = _poly_market(yes_outcome="Chicago Cubs", end_hours=12)
        index = scanner._build_token_city_index([pm])
        km = _kalshi_market(yes_sub_title="Chicago C", close_time_hours=12)
        _, conf = scanner._best_sports_match_single_team(km, index)
        assert conf == 0.80  # times within 36h -> boosted

    def test_single_team_match_no_yes_sub_title_falls_back_to_title(self):
        scanner = CrossPlatformArbScanner(_config())
        pm = _poly_market(yes_outcome="Chicago Cubs", end_hours=12)
        index = scanner._build_token_city_index([pm])
        # No yes_sub_title; should extract "chicago" from "Chicago Cubs vs ..."
        km = _kalshi_market(yes_sub_title="", close_time_hours=12)
        match, conf = scanner._best_sports_match_single_team(km, index)
        assert match is not None

    def test_single_team_no_match_different_city(self):
        scanner = CrossPlatformArbScanner(_config())
        pm = _poly_market(yes_outcome="Boston Red Sox", end_hours=12)
        index = scanner._build_token_city_index([pm])
        km = _kalshi_market(yes_sub_title="Chicago C", close_time_hours=12)
        match, conf = scanner._best_sports_match_single_team(km, index)
        assert match is None


# ── End-to-end arb opportunity detection ─────────────────────────────────────

class TestArbScannerSportsOpportunity:
    def test_soft_arb_via_single_team_match(self):
        """Poly YES=Cubs @ 0.45, Kalshi yes=0.57, no=0.59 -> no TRUE_ARB, gap=0.12 -> SOFT_ARB.
        cost1 = 0.45 + 0.59 = 1.04 * 1.02 = 1.06 >= 1.0 (no true arb)
        cost2 = 0.55 + 0.57 = 1.12 * 1.02 = 1.14 >= 1.0 (no true arb)
        gap = |0.45 - 0.57| = 0.12 >= 0.04 soft edge -> SOFT_ARB
        """
        scanner = CrossPlatformArbScanner(_config())
        pm = _poly_market(
            question="Will the Chicago Cubs win?",
            yes_outcome="Chicago Cubs",
            yes_price=0.45,
            no_price=0.57,
            end_hours=12,
        )
        km = _kalshi_market(
            yes_price=0.57,
            no_price=0.59,
            yes_sub_title="Chicago C",
            close_time_hours=12,
        )
        opps = scanner.find_opportunities([pm], [km])
        soft_arbs = [o for o in opps if o.arb_type == "SOFT_ARB"]
        assert len(soft_arbs) >= 1
        assert soft_arbs[0].poly_ticker == pm.condition_id
        assert soft_arbs[0].kalshi_ticker == km.ticker

    def test_true_arb_via_single_team_match(self):
        """Poly YES=Cubs @ 0.35, Kalshi no_price=0.60 -> combined=0.95 < 1 -> TRUE_ARB."""
        scanner = CrossPlatformArbScanner(_config())
        pm = _poly_market(
            question="Will the Chicago Cubs win?",
            yes_outcome="Chicago Cubs",
            yes_price=0.35,
            no_price=0.67,
            end_hours=12,
        )
        km = _kalshi_market(
            yes_price=0.42,
            no_price=0.60,
            yes_sub_title="Chicago C",
            close_time_hours=12,
        )
        opps = scanner.find_opportunities([pm], [km])
        true_arbs = [o for o in opps if o.arb_type == "TRUE_ARB"]
        assert len(true_arbs) >= 1

    def test_no_opportunity_when_prices_aligned(self):
        """Poly YES=Cubs @ 0.50, Kalshi yes=0.51 -> gap=0.01 < 0.04 soft edge -> no opp."""
        scanner = CrossPlatformArbScanner(_config())
        pm = _poly_market(
            question="Will the Chicago Cubs win?",
            yes_outcome="Chicago Cubs",
            yes_price=0.50,
            no_price=0.52,
            end_hours=12,
        )
        km = _kalshi_market(
            yes_price=0.51,
            no_price=0.51,
            yes_sub_title="Chicago C",
            close_time_hours=12,
        )
        opps = scanner.find_opportunities([pm], [km])
        assert opps == []

    def test_past_kalshi_market_excluded(self):
        scanner = CrossPlatformArbScanner(_config())
        pm = _poly_market(yes_outcome="Chicago Cubs", yes_price=0.35, end_hours=12)
        km = _kalshi_market(yes_price=0.42, no_price=0.60, close_time_hours=-1)
        opps = scanner.find_opportunities([pm], [km])
        assert opps == []

    def test_head_to_head_poly_market_still_matches(self):
        """Original city-pair path still works for 'A vs B' Polymarket questions."""
        scanner = CrossPlatformArbScanner(_config())
        pm = _poly_market(
            question="Chicago Cubs vs St. Louis Cardinals: Winner?",
            yes_outcome="Chicago Cubs",
            yes_price=0.40,
            end_hours=12,
        )
        km = _kalshi_market(
            title="Chicago Cubs vs St. Louis Cardinals",
            yes_sub_title="Chicago C",
            yes_price=0.55,
            no_price=0.47,
            close_time_hours=12,
        )
        opps = scanner.find_opportunities([pm], [km])
        assert len(opps) >= 1


# ── Prop/spread false-match regression ───────────────────────────────────────

class TestPropSpreadFalseMatch:
    """Kalshi prop markets (goal-spread, BTTS, etc.) must not match Poly
    markets of a different type just because they share city names."""

    def test_kalshi_goal_spread_does_not_match_poly_btts(self):
        """Regression: 'Netherlands vs. Sweden: Both Teams to Score' (Poly)
        was falsely matched to a Kalshi market whose yes_sub_title was
        'Sweden wins by more than 2.5 goals', producing a nonsense arb.
        The Kalshi prop filter should drop the market before city matching runs."""
        scanner = CrossPlatformArbScanner(_config())
        # Poly BTTS market — different contract than a moneyline
        pm = _poly_market(
            question="Netherlands vs. Sweden: Both Teams to Score",
            yes_outcome="Yes",
            no_outcome="No",
            yes_price=0.615,
            no_price=0.385,
            end_hours=8,
        )
        # Kalshi goal-spread market — title looks like a moneyline but isn't
        km = KalshiMarket(
            ticker="KXSOCCER-NED-SWE-2.5",
            title="Netherlands vs. Sweden",
            category="Soccer",
            yes_price=0.02,
            no_price=0.98,
            volume=500.0,
            close_time=_now_plus(8),
            yes_sub_title="Sweden wins by more than 2.5 goals",
        )
        opps = scanner.find_opportunities([pm], [km])
        assert opps == [], (
            f"Expected no opportunities but got {opps}. "
            "Kalshi goal-spread market is being falsely matched to a BTTS Poly market."
        )

    def test_kalshi_prop_btts_sub_title_also_filtered(self):
        """Kalshi markets whose yes_sub_title contains 'both teams' are skipped."""
        scanner = CrossPlatformArbScanner(_config())
        pm = _poly_market(
            question="Germany vs. France: Match Winner",
            yes_outcome="Germany",
            no_outcome="France",
            yes_price=0.50,
            no_price=0.52,
            end_hours=8,
        )
        km = KalshiMarket(
            ticker="KXSOCCER-GER-FRA-BTTS",
            title="Germany vs. France",
            category="Soccer",
            yes_price=0.60,
            no_price=0.42,
            volume=300.0,
            close_time=_now_plus(8),
            yes_sub_title="Both teams to score",
        )
        opps = scanner.find_opportunities([pm], [km])
        assert opps == [], (
            "BTTS sub_title on Kalshi should be filtered before city matching."
        )

    def test_poly_btts_excluded_from_city_index(self):
        """Poly BTTS markets should not appear in the city-pair index."""
        scanner = CrossPlatformArbScanner(_config())
        btts = _poly_market(
            question="Tunisia vs. Japan: Both Teams to Score",
            yes_outcome="Yes",
            no_outcome="No",
            yes_price=0.45,
            no_price=0.57,
        )
        winner = _poly_market(
            question="Tunisia vs. Japan: Match Winner",
            yes_outcome="Tunisia",
            no_outcome="Japan",
            yes_price=0.50,
            no_price=0.52,
        )
        # Override condition_id so they're distinct
        winner.condition_id = "poly_cond_2"  # type: ignore[attr-defined]
        index = scanner._build_city_index([btts, winner])
        cities = frozenset({"tunisia", "japan"})
        matches = index.get(cities, [])
        cids = {m.condition_id for m in matches}
        assert btts.condition_id not in cids, "BTTS market must not be in city index"
        assert winner.condition_id in cids, "Winner market should still be indexed"


# ── Alignment-failure false-arb regression ───────────────────────────────────
# Regression for: Poly YES Angels + Kalshi NO A's displayed as +26.5% guaranteed
# arb.  Both legs pay if Angels win — it's a correlated double bet, not an arb.
# Root cause: yes_sub_title="A's" stripped to "as" which had no city match, so
# alignment returned None and the non-sports YES/NO formula ran incorrectly.

def _angels_as_poly(yes_price=0.385, no_price=0.615, end_hours=8):
    tokens = [
        Token(token_id="yes_tok", outcome="Los Angeles Angels", price=yes_price),
        Token(token_id="no_tok", outcome="Athletics", price=no_price),
    ]
    return Market(
        condition_id="poly_angels_as",
        question="Los Angeles Angels vs. Athletics: Winner?",
        tokens=tokens,
        active=True,
        closed=False,
        volume=5000.0,
        end_date=_now_plus(end_hours),
    )


class TestAlignmentFailureFalseArb:
    """When city-pair matching succeeds but team-level alignment fails
    (Kalshi yes_sub_title='A\\'s' can't be resolved to a city), the system
    must NOT fall through to the non-sports YES/NO formula.  That formula
    incorrectly treats Poly YES (Angels win) + Kalshi NO (A's lose = Angels win)
    as complementary legs, producing a fake guaranteed-profit arb."""

    def test_nickname_abbreviation_does_not_produce_false_arb(self):
        """Regression: Angels @ 38.5c (Poly YES) + A's NO @ 39c (Kalshi)
        displayed as +26.5% guaranteed arb.  Both legs pay if Angels win,
        neither pays if A's win — not an arb at all."""
        scanner = CrossPlatformArbScanner(_config())
        pm = _angels_as_poly(yes_price=0.385, no_price=0.615)
        km = KalshiMarket(
            ticker="KXMLB-LAA-OAK",
            title="Los Angeles Angels vs. Athletics",
            category="Baseball",
            yes_price=0.61,
            no_price=0.39,
            volume=1000.0,
            close_time=_now_plus(8),
            yes_sub_title="A's",
        )
        opps = scanner.find_opportunities([pm], [km])
        assert opps == [], (
            "yes_sub_title=A's breaks alignment; non-sports formula must not run."
        )

    def test_full_team_name_resolves_via_substring_fallback(self):
        """Strategy 3b substring: yes_sub_title='Athletics' matches the Poly
        'Athletics' token, so a real arb IS surfaced when prices allow."""
        scanner = CrossPlatformArbScanner(_config())
        # Athletics cheaper on Poly (0.37) + Kalshi NO A's (0.40) = 0.77 < 1 → TRUE_ARB
        tokens = [
            Token(token_id="yes_tok", outcome="Los Angeles Angels", price=0.63),
            Token(token_id="no_tok", outcome="Athletics", price=0.37),
        ]
        pm = Market(
            condition_id="poly_angels_as_2",
            question="Los Angeles Angels vs. Athletics: Winner?",
            tokens=tokens,
            active=True,
            closed=False,
            volume=5000.0,
            end_date=_now_plus(8),
        )
        km = KalshiMarket(
            ticker="KXMLB-LAA-OAK-2",
            title="Los Angeles Angels vs. Athletics",
            category="Baseball",
            yes_price=0.62,
            no_price=0.40,
            volume=1000.0,
            close_time=_now_plus(8),
            yes_sub_title="Athletics",
        )
        opps = scanner.find_opportunities([pm], [km])
        true_arbs = [o for o in opps if o.arb_type == "TRUE_ARB"]
        assert len(true_arbs) >= 1, (
            "yes_sub_title='Athletics' should match Poly 'Athletics' token via "
            "substring fallback and surface the real arb."
        )


# ── World Cup group stage regression ─────────────────────────────────────────

class TestWorldCupGroupStage:
    """Kalshi game markets whose event title contains 'Group Stage' must NOT
    be filtered as prop markets.  The group-stage tournament-progression filter
    should only apply to yes_sub_title, not the event title."""

    def test_group_stage_game_is_not_filtered(self):
        """Argentina vs Brazil in FIFA World Cup Group Stage should be matched,
        not silently dropped by the prop filter."""
        scanner = CrossPlatformArbScanner(_config())
        pm = _poly_market(
            question="Argentina vs. Brazil: Winner?",
            yes_outcome="Argentina",
            no_outcome="Brazil",
            yes_price=0.45,
            no_price=0.57,
            end_hours=12,
        )
        km = KalshiMarket(
            ticker="KXFIFAWC-ARG-BRA",
            # Event title contains "Group Stage" — must NOT be treated as a prop
            title="Argentina vs. Brazil - FIFA World Cup 2026 Group Stage",
            category="Soccer",
            yes_price=0.57,
            no_price=0.55,
            volume=5000.0,
            close_time=_now_plus(12),
            yes_sub_title="Argentina",  # plain team name — not a prop
        )
        opps = scanner.find_opportunities([pm], [km])
        assert len(opps) >= 1, (
            "World Cup group stage game was filtered as a prop market because 'Group Stage' "
            "appeared in the event title.  Tournament-progression filter should only check "
            "yes_sub_title, not the full event title."
        )

    def test_tournament_progression_sub_title_is_still_filtered(self):
        """A Kalshi market whose yes_sub_title indicates advancement (not a game result)
        must still be filtered, even if the event title is a valid 'A vs B' matchup."""
        scanner = CrossPlatformArbScanner(_config())
        pm = _poly_market(
            question="Argentina vs. Brazil: Winner?",
            yes_outcome="Argentina",
            no_outcome="Brazil",
            yes_price=0.50,
            no_price=0.52,
            end_hours=12,
        )
        km = KalshiMarket(
            ticker="KXFIFAWC-ARG-ADV",
            title="Argentina vs. Brazil",
            category="Soccer",
            yes_price=0.60,
            no_price=0.42,
            volume=1000.0,
            close_time=_now_plus(12),
            # sub_title reveals this is a "will advance?" market, not a game winner
            yes_sub_title="Argentina to advance from Group Stage",
        )
        opps = scanner.find_opportunities([pm], [km])
        assert opps == [], (
            "yes_sub_title='Argentina to advance from Group Stage' should still be filtered "
            "by the tournament-progression prop filter."
        )

    def test_at_separator_matches_teams(self):
        """Polymarket 'away @ home' format should be parsed correctly."""
        scanner = CrossPlatformArbScanner(_config())
        pm = _poly_market(
            question="New York Yankees @ Boston Red Sox",
            yes_outcome="New York Yankees",
            no_outcome="Boston Red Sox",
            yes_price=0.45,
            no_price=0.57,
            end_hours=12,
        )
        km = _kalshi_market(
            title="New York Yankees vs Boston Red Sox",
            yes_sub_title="New York Yankees",
            yes_price=0.57,
            no_price=0.55,
            close_time_hours=12,
        )
        opps = scanner.find_opportunities([pm], [km])
        assert len(opps) >= 1, (
            "'@ ' separator in Polymarket question should be treated as 'vs' for city extraction."
        )
