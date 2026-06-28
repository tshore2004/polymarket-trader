"""Unit + live-diagnostic tests for Kalshi price parsing.

Run all (offline only):
    pytest tests/test_kalshi_prices.py -v

Run the live diagnostic (hits real Kalshi API, prints raw fields):
    pytest tests/test_kalshi_prices.py -v -k live
"""
from __future__ import annotations
import pytest
from unittest.mock import patch
from core.kalshi_client import KalshiClient


def _client() -> KalshiClient:
    return KalshiClient(api_key="", api_secret="")


def _raw(yes_bid, yes_ask, no_bid="0", no_ask="0",
         ticker="KXTEST-1", sub="", title="A vs B") -> dict:
    return {
        "ticker": ticker,
        "title": title,
        "yes_sub_title": sub,
        "yes_bid_dollars": yes_bid,
        "yes_ask_dollars": yes_ask,
        "no_bid_dollars": no_bid,
        "no_ask_dollars": no_ask,
    }


# ─── Price parsing unit tests ────────────────────────────────────────────────

class TestKalshiPriceParsing:
    """Verify that _parse_market extracts ask prices, not midpoints."""

    def test_yes_price_is_ask_not_midpoint(self):
        """YES price = yes_ask (what you actually pay to buy YES)."""
        client = _client()
        km = client._parse_market(_raw("0.50", "0.54"), {})
        # midpoint would be 0.52; ask is 0.54
        assert km is not None
        assert km.yes_price == pytest.approx(0.54, abs=0.001), (
            f"Expected yes_price=0.54 (ask), got {km.yes_price}. "
            "Likely still using midpoint (0.52)."
        )

    def test_no_price_is_ask_not_midpoint(self):
        """NO price = no_ask when both bid and ask are returned."""
        client = _client()
        km = client._parse_market(_raw("0.50", "0.54", no_bid="0.44", no_ask="0.48"), {})
        assert km is not None
        assert km.no_price == pytest.approx(0.48, abs=0.001), (
            f"Expected no_price=0.48 (ask), got {km.no_price}. "
            "Likely using midpoint (0.46)."
        )

    def test_no_price_falls_back_to_complement_of_yes_bid(self):
        """When Kalshi doesn't return no_ask, NO price = 1 - yes_bid.

        This is correct: buying NO is equivalent to selling YES at the bid.
        """
        client = _client()
        km = client._parse_market(_raw("0.87", "0.89", no_bid="0", no_ask="0"), {})
        assert km is not None
        expected_no = 1.0 - 0.87  # = 0.13
        assert km.no_price == pytest.approx(expected_no, abs=0.001), (
            f"Expected no_price={expected_no:.4f} (1 - yes_bid), got {km.no_price}. "
            "Likely using 1 - yes_midpoint or 1 - yes_ask instead."
        )

    def test_yes_price_only_ask_available(self):
        """YES ask > 0 but bid = 0: use ask."""
        client = _client()
        km = client._parse_market(_raw("0", "0.55"), {})
        assert km is not None
        assert km.yes_price == pytest.approx(0.55, abs=0.001)

    def test_yes_price_only_bid_available(self):
        """YES ask = 0 but bid > 0: use bid as fallback."""
        client = _client()
        km = client._parse_market(_raw("0.48", "0"), {})
        assert km is not None
        assert km.yes_price == pytest.approx(0.48, abs=0.001)

    def test_prices_sum_to_more_than_one_reflecting_spread(self):
        """YES_ask + NO_ask > 1 is normal — it reflects the bid-ask spread.
        This is correct for an arb check: cost = poly_price + kalshi_price,
        and we only arb when that total < 1.
        """
        client = _client()
        km = client._parse_market(_raw("0.87", "0.89"), {})  # no_ask not returned
        assert km is not None
        total = km.yes_price + km.no_price  # 0.89 + 0.13 = 1.02
        assert total > 1.0, (
            f"YES_ask + NO_ask should be >= 1 when there's a spread. Got {total}"
        )

    def test_skips_market_with_no_prices(self):
        client = _client()
        km = client._parse_market(_raw("0", "0", "0", "0"), {})
        assert km is None


class TestKalshiPriceRefresh:
    """Verify _refresh_kalshi_prices picks the correct leg price."""

    def _make_opp(self, kalshi_action, poly_price=0.36, kalshi_price=0.12):
        from utils.models import ArbitrageOpportunity
        return ArbitrageOpportunity(
            question="Test market",
            poly_ticker="poly-abc",
            kalshi_ticker="KXTEST-1",
            poly_action="BUY YES",
            kalshi_action=kalshi_action,
            poly_price=poly_price,
            kalshi_price=kalshi_price,
            roi_pct=10.0,
            arb_type="TRUE_ARB",
            match_confidence=0.9,
        )

    def _make_km(self, yes_price=0.88, no_price=0.13):
        from utils.models import KalshiMarket
        return KalshiMarket(
            ticker="KXTEST-1",
            title="Test",
            category="Sports",
            yes_price=yes_price,
            no_price=no_price,
        )

    def test_refresh_uses_no_price_for_buy_no_action(self):
        """When kalshi_action='BUY NO', the refreshed price must be no_price."""
        from core.full_arb_scanner import FullArbScanner
        from config import Config

        cfg = Config(private_key="0x" + "a" * 64)
        scanner = FullArbScanner(cfg)
        opp = self._make_opp("BUY NO", kalshi_price=0.12)
        km = self._make_km(yes_price=0.88, no_price=0.14)

        with patch.object(scanner._kalshi_client, "get_market", return_value=km):
            refreshed = scanner._refresh_kalshi_prices([opp])

        assert len(refreshed) == 1
        assert refreshed[0].kalshi_price == pytest.approx(0.14, abs=0.001), (
            f"Expected refreshed kalshi_price=0.14 (no_price), got {refreshed[0].kalshi_price}"
        )

    def test_refresh_uses_yes_price_for_buy_yes_action(self):
        """When kalshi_action='BUY YES', the refreshed price must be yes_price."""
        from core.full_arb_scanner import FullArbScanner
        from config import Config

        cfg = Config(private_key="0x" + "a" * 64)
        scanner = FullArbScanner(cfg)
        opp = self._make_opp("BUY YES", kalshi_price=0.12)
        km = self._make_km(yes_price=0.15, no_price=0.86)

        with patch.object(scanner._kalshi_client, "get_market", return_value=km):
            refreshed = scanner._refresh_kalshi_prices([opp])

        assert len(refreshed) == 1
        assert refreshed[0].kalshi_price == pytest.approx(0.15, abs=0.001), (
            f"Expected refreshed kalshi_price=0.15 (yes_price), got {refreshed[0].kalshi_price}"
        )


# ─── Live diagnostic (requires network) ──────────────────────────────────────

@pytest.mark.live
def test_live_kalshi_price_fields():
    """Hit the real Kalshi API and print raw price fields.

    Run with:  pytest tests/test_kalshi_prices.py -v -k live
    This reveals which price fields the API actually returns.
    """
    client = _client()
    try:
        markets = client.get_markets(limit=5)
    except Exception as exc:
        pytest.skip(f"Kalshi API unreachable: {exc}")

    print("\n\n=== RAW KALSHI PRICE FIELDS (first 5 markets) ===")
    for km in markets[:5]:
        try:
            import json
            raw_resp = client._get(f"/markets/{km.ticker}")
            raw = raw_resp.get("market", {})
            price_fields = {k: v for k, v in raw.items()
                           if any(w in k.lower() for w in ("price", "bid", "ask"))}
            print(f"\nTicker: {km.ticker}")
            print(f"  title: {km.title}")
            print(f"  price-related fields from API: {json.dumps(price_fields, indent=4)}")
            print(f"  → parsed yes_price={km.yes_price:.4f}  no_price={km.no_price:.4f}")
        except Exception as exc:
            print(f"  [error fetching {km.ticker}]: {exc}")

    assert len(markets) > 0, "No markets returned from Kalshi API"
