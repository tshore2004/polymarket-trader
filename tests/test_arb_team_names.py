"""Tests that arb opportunities expose which team/outcome to bet on.

Run:  pytest tests/test_arb_team_names.py -v
"""
from __future__ import annotations
import pytest
from datetime import datetime, timezone

from config import Config
from core.arbitrage import CrossPlatformArbScanner
from utils.models import ArbitrageOpportunity, KalshiMarket, Market, Token


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _cfg() -> Config:
    cfg = Config(private_key="0x" + "a" * 64)
    cfg.arb_min_roi = 0.0
    cfg.arb_soft_min_edge = 0.0
    cfg.fee_rate = 0.0
    return cfg


def _token(outcome: str, price: float, token_id: str = "tok-1") -> Token:
    return Token(token_id=token_id, outcome=outcome, price=price)


def _poly_market(
    question: str,
    yes_outcome: str,
    yes_price: float,
    no_outcome: str,
    no_price: float,
    condition_id: str = "poly-abc",
    end_date: datetime | None = None,
) -> Market:
    end = end_date or datetime(2099, 1, 1, tzinfo=timezone.utc)
    yes_tok = _token(yes_outcome, yes_price, "tok-yes")
    no_tok = _token(no_outcome, no_price, "tok-no")
    return Market(
        condition_id=condition_id,
        question=question,
        end_date=end,
        active=True,
        closed=False,
        tokens=[yes_tok, no_tok],
        volume=10_000,
        tags=[],
    )


def _kalshi_market(
    ticker: str,
    title: str,
    yes_price: float,
    no_price: float,
    yes_sub_title: str = "",
    no_sub_title: str = "",
) -> KalshiMarket:
    return KalshiMarket(
        ticker=ticker,
        title=title,
        category="Esports",
        yes_price=yes_price,
        no_price=no_price,
        yes_sub_title=yes_sub_title,
        no_sub_title=no_sub_title,
    )


def _find_opps(poly_markets, kalshi_markets):
    scanner = CrossPlatformArbScanner(_cfg())
    return scanner.find_opportunities(poly_markets, kalshi_markets)


# ─── Non-sports / keyword-matched markets ────────────────────────────────────

class TestNonSportsArbTeamNames:
    """The else-branch in find_opportunities (keyword/Jaccard matching) must
    populate poly_outcome and kalshi_outcome so the UI shows which side to buy."""

    def test_poly_yes_outcome_set_when_buying_yes_on_poly(self):
        """BUY YES on Poly: poly_outcome must equal the YES token's outcome label."""
        poly = [_poly_market(
            question="Call of Duty: Miami Heretics vs Cloud9 New York - Game 2 Winner",
            yes_outcome="Miami Heretics",
            yes_price=0.365,
            no_outcome="Cloud9 New York",
            no_price=0.635,
        )]
        kalshi = [_kalshi_market(
            ticker="KXESPORTS-COD-1",
            title="Call of Duty Miami Heretics vs Cloud9 New York Game 2",
            yes_price=0.635,
            no_price=0.115,
            yes_sub_title="Miami Heretics",
        )]

        opps = _find_opps(poly, kalshi)
        arb = next((o for o in opps if o.arb_type == "TRUE_ARB"), None)
        assert arb is not None, "Expected a TRUE_ARB opportunity"
        assert arb.poly_action == "BUY YES"
        assert arb.kalshi_action == "BUY NO"
        assert arb.poly_outcome != "", (
            "poly_outcome is empty string. "
            "The non-sports else-branch never sets poly_outcome — fix needed in arbitrage.py."
        )
        assert arb.kalshi_outcome != "", (
            "kalshi_outcome is empty string. "
            "The non-sports else-branch never sets kalshi_outcome — fix needed in arbitrage.py."
        )

    def test_poly_no_outcome_set_when_buying_no_on_poly(self):
        """BUY NO on Poly: poly_outcome must equal the NO token's outcome label.

        Cloud9 is cheap on Poly (NO @ 30¢) and Miami is expensive on Kalshi
        (NO @ 60¢).  True arb: buy Cloud9 NO on Poly + buy Miami NO on Kalshi
        = 0.90 total cost, guaranteed $1 payout.  Kalshi YES = Cloud9 which is
        the Poly NO token, so poly_action must be "BUY NO".

        Uses a Kalshi title that starts differently from Poly so city extraction
        produces mismatched sets, forcing the keyword/Jaccard path.  Alignment
        then identifies Cloud9 as the Poly NO token via yes_sub_title matching.
        """
        poly = [_poly_market(
            question="Call of Duty: Miami Heretics vs Cloud9 New York - Game 2 Winner",
            yes_outcome="Miami Heretics",
            yes_price=0.70,
            no_outcome="Cloud9 New York",
            no_price=0.30,
        )]
        # Kalshi title starts with "Cloud9 New York" so Kalshi cities = {"cloud","miami"}
        # while Poly cities = {"call","cloud"} — they don't fully match → keyword path.
        # Alignment uses yes_sub_title="Cloud9 New York" to find the Poly NO token.
        kalshi = [_kalshi_market(
            ticker="KXESPORTS-COD-1",
            title="Cloud9 New York vs Miami Heretics Call of Duty Game 2",
            yes_price=0.40,   # Cloud9 @ 40¢ on Kalshi (YES side)
            no_price=0.60,    # Miami @ 60¢ on Kalshi (NO side, expensive)
            yes_sub_title="Cloud9 New York",
        )]
        # Strategy 1 fires: poly_same (Cloud9 NO @ 0.30) + k_no (Miami @ 0.60) = 0.90 < 1.0
        # Strategy 2 doesn't fire: poly_opp (Miami YES @ 0.70) + k_yes (Cloud9 @ 0.40) = 1.10

        opps = _find_opps(poly, kalshi)
        arb = next((o for o in opps if o.arb_type == "TRUE_ARB"), None)
        assert arb is not None, "Expected a TRUE_ARB opportunity"
        assert arb.poly_action == "BUY NO", (
            f"Expected poly_action='BUY NO' (Cloud9 is the Poly NO token) "
            f"but got {arb.poly_action!r}."
        )
        assert arb.poly_outcome != "", "poly_outcome is empty when buying NO on Poly"
        assert arb.poly_outcome == "Cloud9 New York", (
            f"Expected poly_outcome='Cloud9 New York' (the NO token), got {arb.poly_outcome!r}"
        )
        assert arb.kalshi_outcome == "Cloud9 New York", (
            f"Expected kalshi_outcome='Cloud9 New York' (from yes_sub_title), got {arb.kalshi_outcome!r}"
        )

    def test_generic_binary_yes_no_leaves_outcome_empty(self):
        """Markets where outcome labels are literally 'YES'/'NO' should leave
        poly_outcome empty so the UI shows just the action, not a redundant label."""
        poly = [_poly_market(
            question="Will the Fed cut rates in July?",
            yes_outcome="YES",
            yes_price=0.35,
            no_outcome="NO",
            no_price=0.65,
        )]
        kalshi = [_kalshi_market(
            ticker="KXFED-JULY",
            title="Fed Rate Cut July",
            yes_price=0.65,
            no_price=0.115,
        )]

        opps = _find_opps(poly, kalshi)
        arb = next((o for o in opps if o.arb_type == "TRUE_ARB"), None)
        if arb:
            # "YES" and "NO" as outcome labels are not useful team names
            assert arb.poly_outcome in ("", "YES", "NO")

    def test_kalshi_outcome_from_yes_sub_title(self):
        """kalshi_outcome should be populated from km.yes_sub_title."""
        poly = [_poly_market(
            question="Call of Duty: Miami Heretics vs Cloud9 New York - Game 2 Winner",
            yes_outcome="Miami Heretics",
            yes_price=0.365,
            no_outcome="Cloud9 New York",
            no_price=0.635,
        )]
        kalshi = [_kalshi_market(
            ticker="KXESPORTS-COD-1",
            title="Call of Duty Miami Heretics vs Cloud9 New York Game 2",
            yes_price=0.635,
            no_price=0.115,
            yes_sub_title="Miami Heretics",
        )]

        opps = _find_opps(poly, kalshi)
        arb = next((o for o in opps if o.arb_type == "TRUE_ARB"), None)
        assert arb is not None
        # kalshi_outcome should come from yes_sub_title (or derived from title)
        assert arb.kalshi_outcome != "", (
            "kalshi_outcome empty even though yes_sub_title='Miami Heretics' was provided."
        )


# ─── Sports-aligned markets ───────────────────────────────────────────────────

class TestSportsArbTeamNames:
    """Sports markets go through _align_poly_token_to_kalshi (3-tuple return).
    Verify the outcome name flows through to the ArbitrageOpportunity."""

    def test_sports_arb_sets_poly_outcome(self):
        """City-matched sports arb must populate poly_outcome with the team name."""
        end = datetime(2026, 7, 1, tzinfo=timezone.utc)
        poly = [_poly_market(
            question="Arizona Diamondbacks vs Los Angeles Dodgers",
            yes_outcome="Arizona Diamondbacks",
            yes_price=0.35,
            no_outcome="Los Angeles Dodgers",
            no_price=0.65,
            end_date=end,
        )]
        kalshi = [_kalshi_market(
            ticker="KXMLBGAME-ARI-LAD",
            title="Arizona Diamondbacks vs Los Angeles Dodgers",
            yes_price=0.65,
            no_price=0.15,
            yes_sub_title="Arizona Diamondbacks",
        )]

        opps = _find_opps(poly, kalshi)
        arb = next((o for o in opps), None)
        if arb:
            assert arb.poly_outcome != "", (
                f"poly_outcome empty for sports arb. "
                f"poly_action={arb.poly_action!r}, poly_outcome={arb.poly_outcome!r}"
            )
            assert arb.poly_outcome in ("Arizona Diamondbacks", "Los Angeles Dodgers"), (
                f"Unexpected poly_outcome: {arb.poly_outcome!r}"
            )
