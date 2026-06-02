"""Tests for Config loading — no network required."""
from __future__ import annotations
import os
import pytest
from unittest.mock import patch


@pytest.fixture(autouse=True)
def no_dotenv():
    """Prevent load_dotenv from reading the real .env during tests."""
    with patch("config.load_dotenv"):
        yield


def _load(env: dict) -> "Config":
    from config import Config
    with patch.dict(os.environ, env, clear=True):
        return Config.load()


def test_load_defaults():
    from config import Config
    cfg = _load({"POLY_PRIVATE_KEY": "0x" + "a" * 64})
    assert cfg.private_key == "0x" + "a" * 64
    assert cfg.max_bet_size == 25.0
    assert cfg.min_bet_size == 5.0
    assert cfg.scan_interval == 60
    assert cfg.fee_rate == 0.02
    assert cfg.min_signal_score == 10.0
    assert cfg.leaderboard_refresh == 900
    assert cfg.market_refresh == 300
    assert cfg.leaderboard_min_traders == 2
    assert cfg.chain_id == 137
    assert cfg.signature_type == 1


def test_load_copy_strategy_defaults():
    """New smart-money copy-trade knobs."""
    cfg = _load({"POLY_PRIVATE_KEY": "0x" + "a" * 64})
    assert cfg.copy_min_position_usd == 100.0
    assert cfg.short_term_hours == 48
    assert cfg.daily_report_top_n == 12


def test_load_missing_private_key_raises():
    from config import Config
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(EnvironmentError, match="POLY_PRIVATE_KEY"):
            Config.load()


def test_load_optional_overrides():
    cfg = _load({
        "POLY_PRIVATE_KEY": "0x" + "b" * 64,
        "MAX_BET_SIZE": "50.0",
        "MIN_BET_SIZE": "10.0",
        "MIN_SIGNAL_SCORE": "30.0",
        "FEE_RATE": "0.03",
        "SCAN_INTERVAL": "120",
        "LEADERBOARD_REFRESH": "1800",
        "MARKET_REFRESH": "600",
        "LEADERBOARD_MIN_TRADERS": "5",
        "COPY_MIN_POSITION_USD": "250",
        "SHORT_TERM_HOURS": "24",
        "DAILY_REPORT_TOP_N": "20",
        "POLY_CHAIN_ID": "80001",
        "POLY_SIGNATURE_TYPE": "3",
    })
    assert cfg.max_bet_size == 50.0
    assert cfg.min_bet_size == 10.0
    assert cfg.min_signal_score == 30.0
    assert cfg.fee_rate == 0.03
    assert cfg.scan_interval == 120
    assert cfg.leaderboard_refresh == 1800
    assert cfg.market_refresh == 600
    assert cfg.leaderboard_min_traders == 5
    assert cfg.copy_min_position_usd == 250.0
    assert cfg.short_term_hours == 24
    assert cfg.daily_report_top_n == 20
    assert cfg.chain_id == 80001
    assert cfg.signature_type == 3


def test_load_leaderboard_params():
    cfg = _load({
        "POLY_PRIVATE_KEY": "0x" + "c" * 64,
        "LEADERBOARD_TOP_N": "10",
        "LEADERBOARD_MIN_PROFIT": "1000.0",
        "LEADERBOARD_MIN_VOLUME": "5000.0",
        "LEADERBOARD_WINDOW": "1w",
    })
    assert cfg.leaderboard_top_n == 10
    assert cfg.leaderboard_min_profit == 1000.0
    assert cfg.leaderboard_min_volume == 5000.0
    assert cfg.leaderboard_window == "1w"


def test_load_with_api_credentials():
    cfg = _load({
        "POLY_PRIVATE_KEY": "0x" + "d" * 64,
        "POLY_API_KEY": "test-key",
        "POLY_API_SECRET": "test-secret",
        "POLY_API_PASSPHRASE": "test-pass",
        "POLY_FUNDER": "0xdeadbeef",
    })
    assert cfg.api_key == "test-key"
    assert cfg.api_secret == "test-secret"
    assert cfg.api_passphrase == "test-pass"
    assert cfg.funder == "0xdeadbeef"
