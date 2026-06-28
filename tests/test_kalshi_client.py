"""Tests for KalshiClient market parsing and sports series fetching — no network required."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from core.kalshi_client import KalshiClient, _TIER1_GAME_SERIES


def _make_client() -> KalshiClient:
    return KalshiClient(api_key="", api_secret="")


def _raw_market(ticker="KXMLBGAME-TEST", yes_bid="0.50", yes_ask="0.52",
                no_bid="0.48", no_ask="0.50", close_time=None) -> dict:
    m = {
        "ticker": ticker,
        "title": "Test Market",
        "yes_sub_title": "",
        "yes_bid_dollars": yes_bid,
        "yes_ask_dollars": yes_ask,
        "no_bid_dollars": no_bid,
        "no_ask_dollars": no_ask,
    }
    if close_time is not None:
        m["close_time"] = close_time
    return m


def _raw_event(category="Sports", close_time=None) -> dict:
    ev = {"title": "Chicago Cubs vs St. Louis Cardinals", "category": category}
    if close_time is not None:
        ev["close_time"] = close_time
    return ev


class TestParseMarket:
    def test_skips_kxmve_ticker(self):
        client = _make_client()
        raw = _raw_market(ticker="KXMVE12345")
        assert client._parse_market(raw, {}) is None

    def test_skips_zero_price_market(self):
        client = _make_client()
        raw = _raw_market(yes_bid="0", yes_ask="0", no_bid="0", no_ask="0")
        assert client._parse_market(raw, {}) is None

    def test_parses_valid_market(self):
        client = _make_client()
        raw = _raw_market()
        km = client._parse_market(raw, _raw_event())
        assert km is not None
        assert km.ticker == "KXMLBGAME-TEST"
        assert km.category == "Sports"
        assert 0 < km.yes_price < 1

    def test_uses_market_level_close_time(self):
        client = _make_client()
        market_ts = "2026-06-20T02:00:00Z"
        event_ts = "2026-06-21T02:00:00Z"
        raw = _raw_market(close_time=market_ts)
        km = client._parse_market(raw, _raw_event(close_time=event_ts))
        assert km.close_time is not None
        assert km.close_time.day == 20  # market-level wins

    def test_inherits_event_level_close_time_when_market_has_none(self):
        client = _make_client()
        event_ts = "2026-06-20T02:00:00Z"
        raw = _raw_market()  # no close_time on market
        km = client._parse_market(raw, _raw_event(close_time=event_ts))
        assert km is not None
        assert km.close_time is not None
        assert km.close_time.year == 2026
        assert km.close_time.month == 6
        assert km.close_time.day == 20

    def test_close_time_none_when_neither_has_it(self):
        client = _make_client()
        km = client._parse_market(_raw_market(), _raw_event())
        assert km is not None
        assert km.close_time is None

    def test_event_level_integer_timestamp(self):
        client = _make_client()
        ts = int(datetime(2026, 6, 20, 2, 0, tzinfo=timezone.utc).timestamp())
        km = client._parse_market(_raw_market(), _raw_event(close_time=ts))
        assert km is not None
        assert km.close_time is not None
        assert km.close_time.day == 20

    def test_category_from_event(self):
        client = _make_client()
        km = client._parse_market(_raw_market(), _raw_event(category="Baseball"))
        assert km.category == "Baseball"


class TestGetSportsSeries:
    def test_filters_by_game_in_title(self):
        client = _make_client()
        series_data = {
            "series": [
                {"ticker": "KXMLBGAME", "title": "MLB Game"},
                {"ticker": "KXMLB", "title": "MLB Season Winner"},
                {"ticker": "KXNBAGAME", "title": "NBA Game"},
            ]
        }
        with patch.object(client, "_get", return_value=series_data):
            result = client.get_sports_series()
        assert "KXMLBGAME" in result
        assert "KXNBAGAME" in result
        assert "KXMLB" not in result  # no "game" in title

    def test_falls_back_to_tier1_when_no_game_series(self):
        client = _make_client()
        series_data = {
            "series": [
                {"ticker": "KXMLB", "title": "MLB Season Winner"},
            ]
        }
        with patch.object(client, "_get", return_value=series_data):
            result = client.get_sports_series()
        assert result == list(_TIER1_GAME_SERIES)

    def test_falls_back_to_tier1_on_api_error(self):
        client = _make_client()
        with patch.object(client, "_get", side_effect=Exception("timeout")):
            result = client.get_sports_series()
        assert result == list(_TIER1_GAME_SERIES)


class TestGetSportsGameMarkets:
    def _make_event(self, hours_until_close: float) -> dict:
        close_dt = datetime.now(timezone.utc) + timedelta(hours=hours_until_close)
        return {
            "title": "Cubs vs Cardinals",
            "category": "Baseball",
            "close_time": close_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "markets": [_raw_market()],
        }

    def test_cutoff_skips_far_future_events(self):
        client = _make_client()
        far_event = self._make_event(hours_until_close=10 * 24)  # 10 days out
        events_data = {"events": [far_event]}
        with patch.object(client, "get_sports_series", return_value=["KXMLBGAME"]):
            with patch.object(client, "_get", return_value=events_data):
                result = client.get_sports_game_markets(days_ahead=2.0)
        assert result == []

    def test_includes_near_term_events(self):
        client = _make_client()
        near_event = self._make_event(hours_until_close=18)  # tonight
        events_data = {"events": [near_event]}
        with patch.object(client, "get_sports_series", return_value=["KXMLBGAME"]):
            with patch.object(client, "_get", return_value=events_data):
                result = client.get_sports_game_markets(days_ahead=2.0)
        assert len(result) == 1
        assert result[0].ticker == "KXMLBGAME-TEST"

    def test_near_term_market_has_close_time_from_event(self):
        client = _make_client()
        near_event = self._make_event(hours_until_close=12)
        events_data = {"events": [near_event]}
        with patch.object(client, "get_sports_series", return_value=["KXMLBGAME"]):
            with patch.object(client, "_get", return_value=events_data):
                result = client.get_sports_game_markets(days_ahead=2.0)
        assert result[0].close_time is not None
        assert result[0].urgency_score == 1.0  # within 24h
