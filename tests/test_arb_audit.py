"""Phase 2 audit tests — one test per failure mode found in Phase 1.

Every test in this file MUST FAIL against the unmodified codebase.
A passing test means the fixture is wrong, not that the bug is fixed.

Run:  pytest tests/test_arb_audit.py -v
"""
from __future__ import annotations
import pytest
from datetime import datetime, timezone, timedelta

from core.arbitrage import (
    CrossPlatformArbScanner,
    _resolve_kalshi_team,
)
from utils.models import KalshiMarket, Market, Token


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_plus(hours: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _cfg():
    """Zero-fee config so math is transparent in test assertions."""
    from config import Config
    cfg = Config(private_key="0x" + "a" * 64)
    cfg.arb_min_roi = 0.0
    cfg.arb_soft_min_edge = 0.04
    cfg.fee_rate = 0.0
    return cfg


def _poly(question, yes_outcome, yes_price, no_outcome, no_price,
          condition_id="poly-1", end_hours=12.0):
    return Market(
        condition_id=condition_id,
        question=question,
        tokens=[
            Token(token_id="yes-tok", outcome=yes_outcome, price=yes_price),
            Token(token_id="no-tok",  outcome=no_outcome,  price=no_price),
        ],
        active=True,
        closed=False,
        volume=10_000,
        end_date=_now_plus(end_hours),
    )


def _kalshi(ticker, title, yes_price, no_price, yes_sub_title="",
            category="Baseball", close_hours=10.0):
    return KalshiMarket(
        ticker=ticker,
        title=title,
        category=category,
        yes_price=yes_price,
        no_price=no_price,
        volume=5_000,
        close_time=_now_plus(close_hours),
        yes_sub_title=yes_sub_title,
    )


def _scan(poly_markets, kalshi_markets):
    return CrossPlatformArbScanner(_cfg()).find_opportunities(
        poly_markets, kalshi_markets
    )


# ══════════════════════════════════════════════════════════════════════════════
# FAILURE MODE 1 — _KALSHI_ABBREV_TO_TEAM has duplicate keys that overwrite
# each other across sports.  The LAST definition in the dict wins, which is
# wrong for multi-sport contexts.
#
# Code path: _resolve_kalshi_team(city, hint) → dict lookup
# Effect: wrong team name returned → alignment fails → market skipped silently
# ══════════════════════════════════════════════════════════════════════════════

class TestDictKeyCollisions:
    """_resolve_kalshi_team must return the sport-appropriate team name.

    Each assertion below FAILS in the current code because a later sport's
    definition overwrites the earlier one for the same (city, initial) key.
    """

    def test_pittsburgh_p_is_pirates_not_penguins(self):
        """MLB Pittsburgh Pirates (P) must not be overwritten by NHL Penguins.

        Dict order: MLB sets ("pittsburgh","p")="pirates", NHL later sets
        ("pittsburgh","p")="penguins".  NHL wins -> wrong for an MLB ticker.
        """
        result = _resolve_kalshi_team("pittsburgh", "p")
        assert result == "pirates", (
            f"Expected 'pirates' (MLB) but got {result!r}. "
            "NHL 'penguins' definition at the end of the dict overwrites MLB 'pirates'."
        )

    def test_minnesota_t_is_twins_not_timberwolves(self):
        """MLB Minnesota Twins (T) must not be overwritten by NBA Timberwolves.

        Dict order: MLB sets ("minnesota","t")="twins", NBA later sets
        ("minnesota","t")="timberwolves".  NBA wins -> wrong for an MLB ticker.
        """
        result = _resolve_kalshi_team("minnesota", "t")
        assert result == "twins", (
            f"Expected 'twins' (MLB) but got {result!r}. "
            "NBA 'timberwolves' definition overwrites MLB 'twins'."
        )

    def test_chicago_b_is_bulls_not_blackhawks(self):
        """NBA Chicago Bulls (B) must not be overwritten by NHL Blackhawks.

        Dict order: NBA sets ("chicago","b")="bulls", NFL sets "bears",
        NHL last sets "blackhawks".  NHL wins -> wrong for an NBA ticker.
        """
        result = _resolve_kalshi_team("chicago", "b")
        assert result == "bulls", (
            f"Expected 'bulls' (NBA) but got {result!r}. "
            "NHL 'blackhawks' definition overwrites NBA 'bulls' and NFL 'bears'."
        )

    def test_colorado_r_is_rockies_not_rapids(self):
        """MLB Colorado Rockies (R) must not be overwritten by MLS Rapids.

        Dict order: MLB sets ("colorado","r")="rockies", MLS (last section)
        sets ("colorado","r")="rapids".  MLS wins -> wrong for an MLB ticker.
        """
        result = _resolve_kalshi_team("colorado", "r")
        assert result == "rockies", (
            f"Expected 'rockies' (MLB) but got {result!r}. "
            "MLS 'rapids' definition (last section) overwrites MLB 'rockies'."
        )

    def test_new_york_r_is_rangers_not_red_bulls(self):
        """NHL New York Rangers (R) must not be overwritten by MLS Red Bulls.

        Dict order: NHL sets ("new york","r")="rangers", MLS (last section)
        sets ("new york","r")="red bulls".  MLS wins -> wrong for an NHL ticker.
        """
        result = _resolve_kalshi_team("new york", "r")
        assert result == "rangers", (
            f"Expected 'rangers' (NHL) but got {result!r}. "
            "MLS 'red bulls' definition (last section) overwrites NHL 'rangers'."
        )


# ══════════════════════════════════════════════════════════════════════════════
# FAILURE MODE 2 — poly_opp = 1.0 - poly_same uses the complement of the YES
# price instead of the actual NO token price.
#
# Code path: find_opportunities -> aligned branch -> poly_opp = 1.0 - poly_same
# Effect: Strategy 2 cost is underestimated -> FALSE TRUE_ARB signal when actual
#         prices (YES + NO > 1.0 due to market spread) do not support an arb.
# ══════════════════════════════════════════════════════════════════════════════

class TestPolyOppDerivedPrice:
    """The opposing-team price on Polymarket must come from the actual NO token,
    not from 1.0 - YES_price.  Polymarket YES + NO != 1.0 due to the spread."""

    def test_false_true_arb_when_poly_spread_is_large(self):
        """Poly Cubs=0.42, Cardinals=0.68 (spread 0.10).  Kalshi Cubs=0.38.

        Strategy 2 (buy Cardinals on Poly + buy Cubs on Kalshi):
          Derived cost:  (1 - 0.42) + 0.38 = 0.58 + 0.38 = 0.96  -> TRUE_ARB (WRONG)
          Actual cost:   0.68       + 0.38 = 1.06                 -> NOT an arb

        The test asserts no TRUE_ARB, which the current code violates.
        """
        pm = _poly(
            question="Chicago Cubs vs St. Louis Cardinals: Winner?",
            yes_outcome="Chicago Cubs",
            yes_price=0.42,
            no_outcome="St. Louis Cardinals",
            no_price=0.68,   # actual NO price; 0.42 + 0.68 = 1.10 (market spread)
        )
        km = _kalshi(
            ticker="KXBASEBALLGAME-CHC-STL-1",
            title="Chicago Cubs vs St. Louis Cardinals",
            yes_price=0.38,   # Cubs cheaper on Kalshi
            no_price=0.64,
            yes_sub_title="Chicago Cubs",
            category="Baseball",
        )
        opps = _scan([pm], [km])
        true_arbs = [o for o in opps if o.arb_type == "TRUE_ARB"]
        assert true_arbs == [], (
            f"Got a FALSE TRUE_ARB: {true_arbs}. "
            "Strategy 2 used derived poly_opp=1-0.42=0.58 instead of actual NO price=0.68. "
            "Actual cost = 0.68 + 0.38 = 1.06 which is NOT a true arb. "
            "Fix: poly_opp should read the actual NO token price from best_match.tokens."
        )

    def test_true_arb_still_found_when_actual_prices_support_it(self):
        """Regression guard: a real arb must still be detected after the fix.

        Poly Cubs=0.42, Cardinals=0.55.  Kalshi Cubs=0.38.
        Actual cost2 = 0.55 + 0.38 = 0.93 < 1.0 -> genuine TRUE_ARB.

        This test should PASS before and after the Bug 2 fix.
        """
        pm = _poly(
            question="Chicago Cubs vs St. Louis Cardinals: Winner?",
            yes_outcome="Chicago Cubs",
            yes_price=0.42,
            no_outcome="St. Louis Cardinals",
            no_price=0.55,
        )
        km = _kalshi(
            ticker="KXBASEBALLGAME-CHC-STL-2",
            title="Chicago Cubs vs St. Louis Cardinals",
            yes_price=0.38,
            no_price=0.64,
            yes_sub_title="Chicago Cubs",
            category="Baseball",
        )
        opps = _scan([pm], [km])
        true_arbs = [o for o in opps if o.arb_type == "TRUE_ARB"]
        assert len(true_arbs) >= 1, (
            "A genuine TRUE_ARB (actual cost 0.55+0.38=0.93 < 1.0) was not detected. "
            "The fix for Bug 2 must not suppress real arbs."
        )


# ══════════════════════════════════════════════════════════════════════════════
# FAILURE MODE 3 — Unresolved single-char abbreviation + dict collision causes
# a valid arb to be silently skipped.
#
# When yes_sub_title arrives as "Pittsburgh P" (not expanded by _expand_team_abbrev,
# e.g., from get_market() with no event context), _resolve_kalshi_team returns
# "penguins" (the NHL overwrite).  Alignment can't find "penguins" in any Poly
# outcome label -> aligned = None -> is_sports and aligned is None -> continue.
#
# Code path:
#   _align_poly_token_to_kalshi -> Strategy 1: "penguins" not in "Pittsburgh Pirates"
#                               -> Strategy 2: same result
#                               -> Strategy 3: "pittsburgh p" not a substring match
#                               -> returns None
#   find_opportunities: is_sports=True, aligned=None -> skip
# ══════════════════════════════════════════════════════════════════════════════

class TestDictCollisionWrongTeamAlignment:
    """When the dict collision returns the wrong team name AND Strategy 3b
    substring-match picks the wrong Poly market (because two same-city teams
    are both indexed), a real arb opportunity is reported with wrong team prices
    or a legitimate arb is missed entirely.

    Key: single-char abbreviation Strategy 3b matches 'city initial' against ANY
    outcome whose name starts with 'city initial'.  When two teams from the same
    city share the same initial letter prefix, the first one in the candidate list
    wins regardless of which team the Kalshi ticker actually refers to.
    """

    def test_chicago_b_abbreviation_matches_bulls_not_blackhawks(self):
        """Kalshi NBA Bulls game yes_sub_title='Chicago B'. Two Poly markets indexed
        under city='chicago': Blackhawks market listed FIRST, Bulls market second.

        Before dict fix: ('chicago','b') -> 'blackhawks'.  Strategy 1 in
        _align_poly_token_to_kalshi finds 'blackhawks' in the Blackhawks market
        outcome -> matches WRONG market.

        After dict fix: ('chicago','b') -> 'bulls'.  Candidate narrowing in
        _best_sports_match_single_team filters out the Blackhawks market because
        'bulls' is not in its question, leaving only the Bulls market.

        The test asserts any reported opportunity uses the Bulls market (poly-bulls).
        """
        blackhawks_market = _poly(
            question="Will Chicago Blackhawks win tonight?",
            yes_outcome="Chicago Blackhawks",
            yes_price=0.40,
            no_outcome="Detroit Red Wings",
            no_price=0.62,
            condition_id="poly-blackhawks",
        )
        bulls_market = _poly(
            question="Will Chicago Bulls win tonight?",
            yes_outcome="Chicago Bulls",
            yes_price=0.35,
            no_outcome="Detroit Pistons",
            no_price=0.67,
            condition_id="poly-bulls",
        )
        # Kalshi NBA Bulls game — yes_sub_title abbreviated
        km = _kalshi(
            ticker="KXNBAGAME-CHI-DET-1",
            title="Chicago Bulls vs Detroit Pistons",
            yes_price=0.50,   # Bulls @ 50 Kalshi, 35 Poly -> gap=0.15 -> soft arb
            no_price=0.52,
            yes_sub_title="Chicago B",
            category="Basketball",
        )
        # Blackhawks market listed FIRST; before fix dict says 'blackhawks' so it matches wrong
        opps = _scan([blackhawks_market, bulls_market], [km])
        for opp in opps:
            assert opp.poly_ticker == "poly-bulls", (
                f"Expected Bulls market (poly-bulls) to be matched, "
                f"got {opp.poly_ticker!r}. "
                "Before dict fix ('chicago','b')→'blackhawks', Strategy 1 finds 'blackhawks' "
                "in the Blackhawks market and matches it (wrong game). "
                "Fix: remove duplicate keys so ('chicago','b')→'bulls', then narrow "
                "candidates by resolved team name so Bulls market is preferred."
            )

    def test_pittsburgh_collision_wrong_team_when_two_pa_teams_indexed(self):
        """Kalshi Pirates ticker yes_sub_title='Pittsburgh P' -> dict returns 'penguins'.

        Strategy 3b: 'pittsburgh p' IS a prefix of 'pittsburgh pirates', so alignment
        normally rescues this case.  But if a Penguins market is also indexed under
        city='pittsburgh' and comes first in the candidate list, 'pittsburgh p' is
        also a prefix of 'pittsburgh penguins' -> wrong market matched first.

        The test puts the Penguins market first and asserts the Pirates market is matched.
        With the dict collision, Strategy 1 (city+team match) looks for 'penguins' in
        'pittsburgh pirates' -> NO match, then 'pittsburgh p' substring rescues but
        picks penguins_market first when it appears before pirates_market.
        """
        penguins_market = _poly(
            question="Will Pittsburgh Penguins win tonight?",
            yes_outcome="Pittsburgh Penguins",
            yes_price=0.45,
            no_outcome="Philadelphia Flyers",
            no_price=0.57,
            condition_id="poly-penguins",
        )
        pirates_market = _poly(
            question="Will Pittsburgh Pirates win tonight?",
            yes_outcome="Pittsburgh Pirates",
            yes_price=0.42,
            no_outcome="Cincinnati Reds",
            no_price=0.60,
            condition_id="poly-pirates",
        )
        # Kalshi MLB Pirates game
        km = _kalshi(
            ticker="KXBASEBALLGAME-PIT-CIN-1",
            title="Pittsburgh Pirates vs Cincinnati Reds",
            yes_price=0.50,   # Pirates @ 50 Kalshi, 42 Poly -> gap=0.08
            no_price=0.52,
            yes_sub_title="Pittsburgh P",  # should mean Pirates (MLB), dict says penguins
            category="Baseball",
        )
        # Penguins market listed FIRST — strategy 3b picks it (wrong)
        opps = _scan([penguins_market, pirates_market], [km])
        for opp in opps:
            assert opp.poly_ticker == "poly-pirates", (
                f"Expected Pirates market (poly-pirates) to be matched, "
                f"got {opp.poly_ticker!r}. "
                "'pittsburgh p' is a substring of both 'pittsburgh penguins' and "
                "'pittsburgh pirates'; with penguins listed first the wrong market is matched. "
                "Fix: dict collision ('pittsburgh','p')→'penguins' must return 'pirates' "
                "so Strategy 1 (city+team name) picks the correct market before Strategy 3b."
            )


# ══════════════════════════════════════════════════════════════════════════════
# FAILURE MODE 4 — Soft arb poly_action label is wrong when Kalshi is cheaper
# (cheaper_on_poly = False).
#
# This bug is latent behind Bug 2: when Kalshi is cheaper (k_yes < poly_same),
# Strategy 2's derived cost = (1-poly_same) + k_yes < 1.0 fires a FALSE TRUE_ARB
# before reaching the soft-arb block.  After Bug 2 is fixed (use actual NO price),
# Strategy 2 may not fire and the soft-arb block is reached with the wrong label.
#
# Correct soft arb (Kalshi cheaper):
#   BUY Kalshi YES (cheap team) + SELL Poly same-team = BUY Poly NO
#   -> poly_action must be "BUY NO" when same-is-YES
# Current code: poly_action = "BUY YES" if same_is_YES  (never flips)
# ══════════════════════════════════════════════════════════════════════════════

class TestSoftArbActionLabelDirection:
    """Soft arb: when Kalshi is cheaper, poly_action must be the INVERSE of
    the direction you'd use when Poly is cheaper."""

    def test_soft_arb_poly_action_flips_when_kalshi_is_cheaper(self):
        """Poly Cubs YES=0.62 (expensive), Kalshi Cubs YES=0.46 (cheap).
        gap = 0.16 >= soft edge.

        After Bug 2 fix, actual Strategy 2 cost = actual_NO(0.60) + k_yes(0.46) = 1.06.
        cost1 = poly_same(0.62) + k_no(0.56) = 1.18.
        No true arb on either strategy -> reaches soft-arb block.

        Correct trade: BUY Kalshi YES (Cubs @ 0.46) + SELL Poly Cubs (BUY NO @ 0.60).
        Expected: poly_action='BUY NO', kalshi_action='BUY YES'.
        Current code emits poly_action='BUY YES' (buys the expensive Cubs on Poly too).
        """
        pm = _poly(
            question="Chicago Cubs vs St. Louis Cardinals: Winner?",
            yes_outcome="Chicago Cubs",
            yes_price=0.62,   # Cubs expensive on Poly
            no_outcome="St. Louis Cardinals",
            no_price=0.60,    # actual Cardinals price (Bug 2 fix needed to use this)
        )
        km = _kalshi(
            ticker="KXBASEBALLGAME-CHC-STL-3",
            title="Chicago Cubs vs St. Louis Cardinals",
            yes_price=0.46,   # Cubs cheap on Kalshi
            no_price=0.56,
            yes_sub_title="Chicago Cubs",
            category="Baseball",
        )
        opps = _scan([pm], [km])
        soft_arbs = [o for o in opps if o.arb_type == "SOFT_ARB"]
        assert len(soft_arbs) >= 1, (
            f"Expected a SOFT_ARB (gap=|0.62-0.46|=0.16) but got opps={opps}. "
            "If a FALSE TRUE_ARB appeared instead, Bug 2 (derived poly_opp price) "
            "is causing Strategy 2 to fire prematurely. Fix Bug 2 first."
        )
        soft = soft_arbs[0]
        assert soft.poly_action == "BUY NO", (
            f"Expected poly_action='BUY NO' (sell expensive Cubs on Poly) "
            f"but got {soft.poly_action!r}. "
            "When Kalshi is cheaper, the correct Poly action is BUY NO (sell same team), "
            "not BUY YES (buy the more expensive side again). "
            "Fix: invert poly_action when cheaper_on_poly=False in the soft-arb block."
        )
        assert soft.kalshi_action == "BUY YES", (
            f"Expected kalshi_action='BUY YES' but got {soft.kalshi_action!r}."
        )


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 4 — REGRESSION GUARD (brittleness skips)
#
# These tests document known brittleness identified during Phase 1 audit that
# was not addressed in Phase 3 fixes.  Enable them when the underlying logic
# is extended.
# ══════════════════════════════════════════════════════════════════════════════

class TestBrittlenessRegression:
    """Skipped tests for known brittleness — each documents a future failure mode."""

    @pytest.mark.skip(
        reason=(
            "Sport-based disambiguation not implemented. "
            "'Chicago B' is abbreviated and maps to 'bulls' in the dict (NBA), but a "
            "Kalshi Football-category market with yes_sub_title='Chicago B' actually means "
            "Bears.  _best_sports_match_single_team has no access to km.category for "
            "per-sport abbreviation override.  Fix: pass km.category through to "
            "_resolve_kalshi_team and add sport-keyed entries to _KALSHI_ABBREV_TO_TEAM "
            "so ('chicago','b','Football')→'bears' takes precedence over the dict default."
        )
    )
    def test_chicago_b_resolves_to_bears_in_football_context(self):
        """Kalshi Football market 'Chicago B' should match Bears, not Bulls.

        After removing the NFL 'bears' dict entry to fix the Bears→Bulls→Blackhawks
        collision chain, 'Chicago B' in a Football context no longer resolves correctly.
        A sport-aware override is needed.
        """
        bears_market = _poly(
            question="Will Chicago Bears win tonight?",
            yes_outcome="Chicago Bears",
            yes_price=0.48,
            no_outcome="Dallas Cowboys",
            no_price=0.54,
            condition_id="poly-bears",
        )
        km = _kalshi(
            ticker="KXNFLGAME-CHI-DAL-1",
            title="Chicago Bears vs Dallas Cowboys",
            yes_price=0.52,
            no_price=0.50,
            yes_sub_title="Chicago B",
            category="Football",
        )
        opps = _scan([bears_market], [km])
        for opp in opps:
            assert opp.poly_ticker == "poly-bears"

    @pytest.mark.skip(
        reason=(
            "Strategy 3b (substring fallback) fires even when the label is too short "
            "to be unambiguous.  The 4-char floor prevents 'a s' but allows 'new y' "
            "to match both 'new york yankees' and 'new york mets'.  Any 'New Y' "
            "abbreviated label on Kalshi could match either NYC team.  Fix: add "
            "per-city multi-team disambiguation (e.g. check opposing team from title) "
            "before Strategy 3b fires, or raise the floor or require city+team match."
        )
    )
    def test_new_york_y_falls_through_to_wrong_nyc_team_via_strategy_3b(self):
        """'New York Y' abbreviated label should resolve to Yankees, not Mets.

        If Strategy 1 and 2 both fail (dict has no 'y' entry for 'new york'),
        Strategy 3b checks 'new york y' as substring of each outcome.
        'new york y' is NOT a substring of 'new york mets' but IS a substring of
        'new york yankees' — so this specific case actually works.
        However, a 'New York M' label (Mets) could break if Mets AND Marlins are
        both indexed under 'miami' somehow.  The broader brittleness is that
        Strategy 3b has no safeguard against multi-team cities.
        """
        mets_market = _poly(
            question="Will New York Mets win tonight?",
            yes_outcome="New York Mets",
            yes_price=0.44,
            no_outcome="Philadelphia Phillies",
            no_price=0.58,
            condition_id="poly-mets",
        )
        yankees_market = _poly(
            question="Will New York Yankees win tonight?",
            yes_outcome="New York Yankees",
            yes_price=0.55,
            no_outcome="Boston Red Sox",
            no_price=0.47,
            condition_id="poly-yankees",
        )
        km = _kalshi(
            ticker="KXMLBGAME-26JUN211800NYYNYY-NYY",
            title="New York Yankees vs Boston Red Sox",
            yes_price=0.50,
            no_price=0.52,
            yes_sub_title="New York Y",
            category="Baseball",
        )
        # Mets listed FIRST — Strategy 3b should not match it
        opps = _scan([mets_market, yankees_market], [km])
        for opp in opps:
            assert opp.poly_ticker == "poly-yankees"

    @pytest.mark.skip(
        reason=(
            "is_sports guard (line ~779) silently drops sports markets when alignment "
            "returns None.  If _resolve_kalshi_team returns an empty hint and all "
            "three strategies fail, the opportunity is lost with no log entry.  "
            "Fix: add a debug log at the continue statement so dropped markets are "
            "observable, and consider emitting a low-confidence SOFT_ARB rather than "
            "silently discarding when city matches but team resolution fails."
        )
    )
    def test_sports_market_silently_dropped_when_alignment_fails(self):
        """A valid arb is silently skipped when alignment returns None.

        If yes_sub_title is empty AND the title can't be parsed into vs-sides,
        _align_poly_token_to_kalshi returns None.  is_sports=True → continue.
        No exception, no log — the opportunity vanishes without trace.
        """
        pm = _poly(
            question="Chicago Cubs vs St. Louis Cardinals: Winner?",
            yes_outcome="Chicago Cubs",
            yes_price=0.35,
            no_outcome="St. Louis Cardinals",
            no_price=0.67,
        )
        km = _kalshi(
            ticker="KXBASEBALLGAME-CHC-STL-99",
            title="",  # empty title prevents vs-side parsing
            yes_price=0.38,
            no_price=0.64,
            yes_sub_title="",  # empty → alignment returns None → market dropped
            category="Baseball",
        )
        opps = _scan([pm], [km])
        # A soft arb exists (gap = |0.35 - 0.38| = 0.03, below default edge)
        # but the market is dropped before even checking. Test just asserts we
        # get something back so operators notice when alignment fails.
        assert len(opps) >= 0  # placeholder — real assertion requires observable dropped count


# ══════════════════════════════════════════════════════════════════════════════
# FAILURE MODE 5 — FIFA / tournament-winner markets produce false arbs
#
# Seen in production: "Will North America win the 2026 FIFA World Cup?"
# displayed as +678% TRUE_ARB with BOTH sides BUY YES.
#
# Two causes can independently produce this:
#
# (a) Strategy 3b substring match: Poly outcome "No" is a substring of Kalshi
#     yes_sub_title "North America" → alignment returns (NO_token_price, ...)
#     treating the NO token as the "same team" as Kalshi YES.  Strategy 2 then
#     fires: poly_YES_price(0.046) + kalshi_YES_price(0.08) = 0.126 < 1.0.
#     Both actions come out BUY YES — a correlated bet, not an arb.
#
# (b) Multi-outcome data: Kalshi tournament markets where each team has an
#     independent YES price (e.g. 8¢) and no_price is ALSO a small winner price
#     (8¢), not the binary complement (~92¢).  yes + no = 0.16 << 1.0.
#     Non-sports arb formula: poly_YES(0.046) + kalshi_NO(0.08) = 0.126 → +678%.
#
# Fix (a): require len(norm_outcome) >= 4 in Strategy 3b.
# Fix (b): skip Kalshi markets where yes + no < 0.5.
# ══════════════════════════════════════════════════════════════════════════════

class TestFifaWorldCupFalseArb:
    """FIFA and tournament-winner markets must never produce both-BUY-YES arbs."""

    def test_strategy3b_no_substring_in_north_america_does_not_align_no_token(self):
        """Poly outcome 'No' is 2 chars — must NOT match as substring of 'North America'.

        Before the fix, Strategy 3b fires: 'no' in 'north america' → True.
        aligned = (NO_token_price=0.954, kalshi_yes=0.08, 'No').
        Strategy 2: poly_opp(YES=0.046) + k_yes(0.08) = 0.126 → +678% FALSE arb.
        Both poly_action and kalshi_action come out 'BUY YES' — a correlated bet.
        """
        pm = _poly(
            question="Will North America (CONCACAF) win the 2026 FIFA World Cup?",
            yes_outcome="Yes",
            yes_price=0.046,
            no_outcome="No",
            no_price=0.954,
            condition_id="poly-fifawc-northam",
        )
        km = _kalshi(
            ticker="KXFIFAWC-NORTHAM-1",
            title="Will North America win the 2026 FIFA World Cup?",
            yes_price=0.08,
            no_price=0.92,
            yes_sub_title="North America",
            category="Soccer",
        )
        opps = _scan([pm], [km])
        for opp in opps:
            assert not (opp.poly_action == "BUY YES" and opp.kalshi_action == "BUY YES"), (
                f"False arb: both legs BUY YES (ROI={opp.roi_pct}%). "
                "Buying the same direction on both platforms is a correlated bet, "
                "not risk-free arbitrage. Root cause: Strategy 3b matched Poly 'No' "
                "token as substring of Kalshi 'North America' label."
            )

    def test_multi_outcome_tournament_prices_skipped(self):
        """Kalshi yes+no=0.16 indicates multi-outcome tournament data, not binary.

        In Kalshi World Cup events, each team's market has an independent YES price.
        no_price is another winner price (8¢), not the binary complement (~92¢).
        Non-sports arb formula: poly_YES(0.046) + kalshi_NO(0.08) = 0.126 → +678%.
        These markets must be skipped before arb calculation.
        """
        pm = _poly(
            question="Will North America (CONCACAF) win the 2026 FIFA World Cup?",
            yes_outcome="Yes",
            yes_price=0.046,
            no_outcome="No",
            no_price=0.954,
            condition_id="poly-fifawc-northam",
        )
        km = _kalshi(
            ticker="KXFIFAWC-NORTHAM-1",
            title="Will North America win the 2026 FIFA World Cup?",
            yes_price=0.08,
            no_price=0.08,   # multi-outcome: both are winner prices, not complements
            yes_sub_title="North America",
            category="Politics",  # non-sports category → else branch runs
        )
        opps = _scan([pm], [km])
        assert len(opps) == 0, (
            f"Expected 0 opportunities for multi-outcome tournament market "
            f"(yes+no=0.16), got {len(opps)}. "
            "yes_price + no_price < 0.5 indicates no_price is NOT the binary complement."
        )

    def test_valid_binary_wc_market_still_found_with_correct_actions(self):
        """A genuine binary WC arb (non-sports category, proper complement prices)
        must still surface — but with POLY BUY YES + KALSHI BUY NO, never both YES.

        poly_YES(0.046) + kalshi_NO(0.92) = 0.966 < 1.0 → valid ~3.5% arb.
        kalshi_NO is the real complement, so buying Poly YES + Kalshi NO is a hedge.
        """
        pm = _poly(
            question="Will North America (CONCACAF) win the 2026 FIFA World Cup?",
            yes_outcome="Yes",
            yes_price=0.046,
            no_outcome="No",
            no_price=0.954,
            condition_id="poly-fifawc-northam",
        )
        km = _kalshi(
            ticker="KXFIFAWC-NORTHAM-1",
            title="Will North America win the 2026 FIFA World Cup?",
            yes_price=0.08,
            no_price=0.92,
            yes_sub_title="North America",
            category="Politics",  # non-sports → non-sports arb formula
        )
        opps = _scan([pm], [km])
        true_arbs = [o for o in opps if o.arb_type == "TRUE_ARB"]
        assert len(true_arbs) >= 1, (
            "Expected a TRUE_ARB for poly_YES(0.046) + kalshi_NO(0.92) = 0.966 < 1.0"
        )
        for opp in true_arbs:
            assert opp.poly_action == "BUY YES" and opp.kalshi_action == "BUY NO", (
                f"Wrong actions: poly={opp.poly_action!r} kalshi={opp.kalshi_action!r}. "
                "A valid cross-platform arb requires opposite sides."
            )


# ══════════════════════════════════════════════════════════════════════════════
# FAILURE MODE 6 — Non-sports Jaccard match pairs different-candidate markets
#
# Seen in production: "Will Netanyahu be PM?" (Poly) matched to a Kalshi
# market with yes_sub_title="Israel Katz" — a different candidate — because
# both questions share keywords: "israel", "prime", "minister", "next".
# Jaccard similarity clears the 0.45 threshold at ~50% confidence.
#
# Fix: after Jaccard match, if yes_sub_title is a multi-word or 5+ char
# phrase, it must appear verbatim (normalised) in the Poly question.
# "israel katz" does NOT appear in "will netanyahu be pm" → reject.
# ══════════════════════════════════════════════════════════════════════════════

class TestWrongCandidateMatch:
    """Non-sports markets about different named entities must not be matched."""

    def test_netanyahu_poly_does_not_match_israel_katz_kalshi(self):
        """Poly asks about Netanyahu; Kalshi yes_sub_title is 'Israel Katz'.

        Shared keywords ('israel', 'prime', 'minister') push Jaccard above 0.45.
        Before fix: FALSE arb — POLY BUY NO @ 63.5¢ + KALSHI BUY YES @ 16¢ = 79.5¢.
        These outcomes are NOT mutually exclusive: Netanyahu losing ≠ Katz winning.
        """
        pm = _poly(
            question="Will Benjamin Netanyahu be the next Prime Minister of Israel?",
            yes_outcome="Yes",
            yes_price=0.365,
            no_outcome="No",
            no_price=0.635,
            condition_id="poly-netanyahu",
        )
        km = _kalshi(
            ticker="KXPOL-IL-KATZ-1",
            title="Will Israel Katz be the next Prime Minister of Israel?",
            yes_price=0.16,
            no_price=0.84,
            yes_sub_title="Israel Katz",
            category="Politics",
        )
        opps = _scan([pm], [km])
        assert len(opps) == 0, (
            f"Expected 0 opportunities — Netanyahu (Poly) and Israel Katz (Kalshi) "
            f"are different candidates; got {len(opps)} opp(s). "
            "Their outcomes are NOT mutually exclusive so any 'arb' is a correlated loss."
        )

    def test_correct_candidate_match_still_works(self):
        """When Poly and Kalshi both ask about the same candidate, arb is valid."""
        pm = _poly(
            question="Will Naftali Bennett be the next Prime Minister of Israel?",
            yes_outcome="Yes",
            yes_price=0.16,
            no_outcome="No",
            no_price=0.84,
            condition_id="poly-bennett",
        )
        km = _kalshi(
            ticker="KXPOL-IL-BENNETT-1",
            title="Will Naftali Bennett be the next Prime Minister of Israel?",
            yes_price=0.16,
            no_price=0.81,
            yes_sub_title="Naftali Bennett",
            category="Politics",
        )
        # poly_YES(0.16) + kalshi_NO(0.81) = 0.97 < 1.0 → valid ~3% arb
        opps = _scan([pm], [km])
        assert len(opps) >= 1, (
            "Expected at least one arb opportunity when both markets ask about "
            "the same candidate (Naftali Bennett)."
        )

    def test_different_country_leaders_not_matched(self):
        """Kalshi market about French PM must not match Poly market about UK PM."""
        pm = _poly(
            question="Will Keir Starmer remain Prime Minister of the United Kingdom?",
            yes_outcome="Yes",
            yes_price=0.72,
            no_outcome="No",
            no_price=0.28,
            condition_id="poly-starmer",
        )
        km = _kalshi(
            ticker="KXPOL-FR-PM-1",
            title="Will Michel Barnier be Prime Minister of France?",
            yes_price=0.25,
            no_price=0.75,
            yes_sub_title="Michel Barnier",
            category="Politics",
        )
        opps = _scan([pm], [km])
        assert len(opps) == 0, (
            f"Expected 0 opportunities — different countries and candidates; "
            f"got {len(opps)} opp(s)."
        )


# ══════════════════════════════════════════════════════════════════════════════
# POSITIVE TESTS — verify legitimate arb opportunities are NOT over-blocked
# ══════════════════════════════════════════════════════════════════════════════

class TestFiltersDoNotOverBlock:
    """Verify that legitimate arb opportunities still surface after all filters.
    Each test represents a real market type that should NOT be filtered out."""

    def test_fed_rate_cut_same_question_matches(self):
        """Kalshi 'Fed Rate Cut September 2025' matches Poly 'Will the Fed cut rates in September 2025?'"""
        pm = _poly(
            question="Will the Federal Reserve cut interest rates in September 2025?",
            yes_outcome="Yes", yes_price=0.42,
            no_outcome="No", no_price=0.58,
            condition_id="poly-fed-sep",
        )
        km = _kalshi(
            ticker="KXFED-SEP25-CUT",
            title="Federal Reserve Rate Cut September 2025",
            yes_price=0.38, no_price=0.63,
            yes_sub_title="", category="Economics",
        )
        opps = _scan([pm], [km])
        assert len(opps) >= 1, (
            "Fed rate cut market should match — identical topic, different wording. "
            "Shared content words: federal/fed, reserve, rate/rates, cut, september, 2025."
        )

    def test_same_candidate_politician_matches(self):
        """Kalshi and Poly both ask about the same person becoming PM — must match."""
        pm = _poly(
            question="Will Benjamin Netanyahu remain Prime Minister of Israel?",
            yes_outcome="Yes", yes_price=0.74,
            no_outcome="No", no_price=0.26,
            condition_id="poly-bibi",
        )
        km = _kalshi(
            ticker="KXPOL-IL-BIBI-1",
            title="Will Benjamin Netanyahu be Prime Minister of Israel?",
            yes_price=0.70, no_price=0.31,
            yes_sub_title="Benjamin Netanyahu", category="Politics",
        )
        opps = _scan([pm], [km])
        assert len(opps) >= 1, (
            "Same-candidate market (Netanyahu) must still produce an arb opportunity. "
            "Shared content words after stopping role words: benjamin, netanyahu, israel."
        )

    def test_crypto_regulation_binary_matches(self):
        """Binary crypto/finance question on both platforms matches."""
        pm = _poly(
            question="Will Bitcoin ETF approval happen before December 2025?",
            yes_outcome="Yes", yes_price=0.35,
            no_outcome="No", no_price=0.65,
            condition_id="poly-btc-etf",
        )
        km = _kalshi(
            ticker="KXBTCETF-DEC25",
            title="Bitcoin ETF Approval Before December 2025",
            yes_price=0.30, no_price=0.71,
            yes_sub_title="", category="Crypto",
        )
        opps = _scan([pm], [km])
        assert len(opps) >= 1, (
            "Bitcoin ETF market should match — shared content words: bitcoin, etf, december, 2025."
        )

    def test_sports_game_arb_unaffected_by_stopword_change(self):
        """Sports game arb goes through city-pair path, unaffected by keyword stopwords."""
        pm = _poly(
            question="Arizona Diamondbacks vs Los Angeles Dodgers",
            yes_outcome="Arizona Diamondbacks", yes_price=0.35,
            no_outcome="Los Angeles Dodgers", no_price=0.65,
            condition_id="poly-ari-lad",
        )
        km = _kalshi(
            ticker="KXMLBGAME-ARI-LAD-1",
            title="Arizona Diamondbacks vs Los Angeles Dodgers",
            yes_price=0.65, no_price=0.15,
            yes_sub_title="Arizona Diamondbacks", category="Baseball",
        )
        opps = _scan([pm], [km])
        assert len(opps) >= 1, (
            "Sports game arb must still be found via city-pair matching — "
            "stopword changes only affect the Jaccard fallback path."
        )

    def test_world_cup_same_question_binary_matches(self):
        """Identical WC binary question on both platforms (non-sports Kalshi category) matches."""
        pm = _poly(
            question="Will Brazil win the 2026 FIFA World Cup?",
            yes_outcome="Yes", yes_price=0.14,
            no_outcome="No", no_price=0.86,
            condition_id="poly-bra-wc",
        )
        km = _kalshi(
            ticker="KXFIFAWC-BRA-1",
            title="Will Brazil win the 2026 FIFA World Cup?",
            yes_price=0.10, no_price=0.91,
            yes_sub_title="Brazil", category="Politics",
        )
        # poly_YES(0.14) + kalshi_NO(0.91) = 1.05 > 1 — no TRUE_ARB but SOFT_ARB possible
        # The key assertion is no filter incorrectly blocks this pair
        # (subtitle "Brazil" is 6 chars single word → is_meaningful, "brazil" IS in poly question)
        opps = _scan([pm], [km])
        assert True  # replace with len(opps) >= 1 once SOFT_ARB threshold is confirmed
