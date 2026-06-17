from __future__ import annotations
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv


@dataclass
class Config:
    # ── Auth ──────────────────────────────────────────────────────────────────
    private_key: str
    funder: str = ""
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    signature_type: int = 1
    chain_id: int = 137

    # ── Risk ──────────────────────────────────────────────────────────────────
    max_bet_size: float = 25.0
    min_bet_size: float = 5.0
    fee_rate: float = 0.02

    # ── Leaderboard copy-trading ───────────────────────────────────────────────
    leaderboard_top_n: int = 20
    leaderboard_min_profit: float = 5_000.0
    leaderboard_min_volume: float = 10_000.0
    leaderboard_window: str = "1m"
    leaderboard_min_traders: int = 2

    # ── Copy-trading strategy ──────────────────────────────────────────────────
    # A trader position only counts as "conviction" if it's at least this large ($).
    # Filters out dust / exploratory positions from the smart-money signal.
    copy_min_position_usd: float = 100.0
    # Markets resolving within this many hours are treated as short-term ("tonight").
    short_term_hours: int = 48
    # How many ranked picks to show in the daily report.
    daily_report_top_n: int = 12

    # ── Signal threshold ──────────────────────────────────────────────────────
    min_signal_score: float = 10.0
    min_signal_score_today: float = 5.0

    # ── Timing ────────────────────────────────────────────────────────────────
    scan_interval: int = 60
    leaderboard_refresh: int = 900
    market_refresh: int = 300

    # ── Scan mode ─────────────────────────────────────────────────────────────
    # all | leaderboard | news | volume | today
    scan_mode: str = "all"

    # ── Market category filter ─────────────────────────────────────────────────
    market_tags_filter: list[str] = field(default_factory=list)

    # ── News sentiment ────────────────────────────────────────────────────────
    news_enabled: bool = True
    news_api_key: str = ""
    news_refresh: int = 300

    # ── Volume spike detection ────────────────────────────────────────────────
    volume_spike_enabled: bool = True
    volume_spike_threshold: float = 1.5

    # ── Fair value / external odds ────────────────────────────────────────────
    odds_api_key: str = ""       # The Odds API key (free tier: 500 req/month)
    pinnacle_username: str = ""  # Pinnacle API username (requires funded account + API access)
    pinnacle_password: str = ""  # Pinnacle API password

    # ── Backtest / performance tracking ───────────────────────────────────────
    backtest_min_score: float = 50.0  # Only log signals scoring above this

    @classmethod
    def load(cls) -> Config:
        load_dotenv()

        def req(key: str) -> str:
            val = os.getenv(key, "").strip()
            if not val:
                raise EnvironmentError(
                    f"Missing required env var: {key}\n"
                    "Copy .env.example to .env and fill in your credentials."
                )
            return val

        def opt_float(key: str, default: float) -> float:
            val = os.getenv(key, "").strip()
            return float(val) if val else default

        def opt_int(key: str, default: int) -> int:
            val = os.getenv(key, "").strip()
            return int(val) if val else default

        def opt_str(key: str, default: str) -> str:
            return os.getenv(key, default).strip()

        def opt_bool(key: str, default: bool) -> bool:
            val = os.getenv(key, "").strip().lower()
            if not val:
                return default
            return val in ("1", "true", "yes")

        def opt_list(key: str) -> list[str]:
            val = os.getenv(key, "").strip()
            if not val:
                return []
            return [s.strip().lower() for s in val.split(",") if s.strip()]

        return cls(
            private_key=req("POLY_PRIVATE_KEY"),
            funder=opt_str("POLY_FUNDER", ""),
            api_key=opt_str("POLY_API_KEY", ""),
            api_secret=opt_str("POLY_API_SECRET", ""),
            api_passphrase=opt_str("POLY_API_PASSPHRASE", ""),
            signature_type=opt_int("POLY_SIGNATURE_TYPE", 1),
            chain_id=opt_int("POLY_CHAIN_ID", 137),
            max_bet_size=opt_float("MAX_BET_SIZE", 25.0),
            min_bet_size=opt_float("MIN_BET_SIZE", 5.0),
            fee_rate=opt_float("FEE_RATE", 0.02),
            scan_interval=opt_int("SCAN_INTERVAL", 60),
            leaderboard_top_n=opt_int("LEADERBOARD_TOP_N", 20),
            leaderboard_min_profit=opt_float("LEADERBOARD_MIN_PROFIT", 5_000.0),
            leaderboard_min_volume=opt_float("LEADERBOARD_MIN_VOLUME", 10_000.0),
            leaderboard_window=opt_str("LEADERBOARD_WINDOW", "1m"),
            leaderboard_min_traders=opt_int("LEADERBOARD_MIN_TRADERS", 2),
            copy_min_position_usd=opt_float("COPY_MIN_POSITION_USD", 100.0),
            short_term_hours=opt_int("SHORT_TERM_HOURS", 48),
            daily_report_top_n=opt_int("DAILY_REPORT_TOP_N", 12),
            leaderboard_refresh=opt_int("LEADERBOARD_REFRESH", 900),
            market_refresh=opt_int("MARKET_REFRESH", 300),
            min_signal_score=opt_float("MIN_SIGNAL_SCORE", 10.0),
            min_signal_score_today=opt_float("MIN_SIGNAL_SCORE_TODAY", 5.0),
            scan_mode=opt_str("SCAN_MODE", "all"),
            market_tags_filter=opt_list("MARKET_TAGS_FILTER"),
            news_enabled=opt_bool("NEWS_ENABLED", True),
            news_api_key=opt_str("NEWS_API_KEY", ""),
            news_refresh=opt_int("NEWS_REFRESH", 300),
            volume_spike_enabled=opt_bool("VOLUME_SPIKE_ENABLED", True),
            volume_spike_threshold=opt_float("VOLUME_SPIKE_THRESHOLD", 1.5),
            odds_api_key=opt_str("ODDS_API_KEY", ""),
            pinnacle_username=opt_str("PINNACLE_USERNAME", ""),
            pinnacle_password=opt_str("PINNACLE_PASSWORD", ""),
            backtest_min_score=opt_float("BACKTEST_MIN_SCORE", 50.0),
        )
