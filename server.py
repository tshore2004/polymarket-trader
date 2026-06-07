from __future__ import annotations
import argparse
import dataclasses
from contextlib import asynccontextmanager
from datetime import datetime
from enum import Enum
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from config import Config
from core.executor import TradeExecutor
from core.scanner import BackgroundScanner
from core.signal import SignalEngine
from core.starred_traders import StarredTraderStore
from utils.models import Side

templates = Jinja2Templates(directory="templates")
scanner: BackgroundScanner | None = None
starred_store: StarredTraderStore | None = None


def _jsonify(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _jsonify(dataclasses.asdict(obj))
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonify(i) for i in obj]
    return obj


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scanner, starred_store
    config = Config.load()
    engine = SignalEngine(config)
    executor = TradeExecutor(config)
    starred_store = engine._lb.starred
    scanner = BackgroundScanner(engine, executor, config.scan_interval, scan_mode=config.scan_mode)
    scanner.start()
    yield
    scanner.stop()


app = FastAPI(title="Polymarket Trader", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/api/state")
async def api_state():
    snap = scanner.get_snapshot()
    return JSONResponse({
        "state": snap.state.value,
        "last_scan_at": snap.last_scan_at.isoformat() if snap.last_scan_at else None,
        "scan_duration_s": snap.scan_duration_s,
        "markets_loaded": snap.markets_loaded,
        "signal_count": len(snap.signals),
        "trader_count": len(snap.traders),
        "consensus_count": len(snap.consensuses),
        "news_signal_count": len(snap.news_signals),
        "volume_signal_count": len(snap.volume_signals),
        "scan_mode": snap.scan_mode,
        "balance": snap.balance,
        "error": snap.error,
        "scan_count": snap.scan_count,
        "scan_stage": snap.scan_stage,
        "scan_progress": snap.scan_progress,
    })


@app.get("/api/signals")
async def api_signals():
    snap = scanner.get_snapshot()
    signals_raw = _jsonify(snap.signals)
    for raw, sig in zip(signals_raw, snap.signals):
        tok = (
            sig.market.yes_token
            if sig.recommended_side == Side.YES
            else sig.market.no_token
        )
        raw["recommended_outcome"] = (
            tok.outcome
            if tok and tok.outcome.lower() not in ("yes", "no", "1", "0", "")
            else sig.recommended_side.value
        )
    return JSONResponse(signals_raw)


@app.get("/api/picks")
async def api_picks():
    snap = scanner.get_snapshot()
    active_picks = [p for p in snap.picks
                    if p.market.time_category != "past" and not p.market.closed]
    picks_raw = _jsonify(active_picks)
    # Build trader lookup for quality info
    trader_lookup = {t.address: t for t in snap.traders}
    for raw, pick in zip(picks_raw, active_picks):
        raw["dominant_side"] = pick.dominant_side.value
        raw["total_traders"] = pick.num_traders_yes + pick.num_traders_no
        raw["time_category"] = pick.market.time_category
        raw["daily_score"] = pick.daily_score
        raw["category"] = pick.category
        raw["subcategory"] = pick.subcategory
        raw["avg_dominant_win_rate"] = pick.avg_dominant_win_rate
        raw["copy_score"] = pick.copy_score
        raw["dominant_position_value"] = pick.dominant_position_value
        dominant_tok = (
            pick.market.yes_token
            if pick.dominant_side == Side.YES
            else pick.market.no_token
        )
        raw["recommended_price"] = (
            round(dominant_tok.price, 4)
            if dominant_tok and dominant_tok.price > 0
            else None
        )
        raw["recommended_outcome"] = (
            dominant_tok.outcome
            if dominant_tok and dominant_tok.outcome.lower() not in ("yes", "no", "1", "0", "")
            else pick.dominant_side.value
        )
        # Enrich stakes with trader quality data
        for side_key in ("yes_stakes", "no_stakes"):
            if side_key in raw:
                for stake in raw[side_key]:
                    t = trader_lookup.get(stake.get("address", ""))
                    if t:
                        stake["consistency_grade"] = t.consistency_grade
                        stake["num_trades"] = t.num_trades
                        stake["pct_positive"] = round(t.pct_positive, 4)
                        stake["profit"] = round(t.profit, 2)
                        stake["starred"] = t.starred
    return JSONResponse(picks_raw)


@app.get("/api/leaderboard")
async def api_leaderboard():
    snap = scanner.get_snapshot()
    traders = _jsonify(snap.traders)
    # Enrich with quality fields not in dataclass asdict
    for raw, t in zip(traders, snap.traders):
        raw["consistency_grade"] = t.consistency_grade
        raw["profit_per_trade"] = round(t.profit_per_trade, 2)
        raw["starred"] = t.starred
        raw["largest_win"] = round(t.largest_win, 2)
        raw["closed_positions"] = t.closed_positions
        raw["winning_positions"] = t.winning_positions
        raw["win_rate"] = round(t.pct_positive, 4)
        raw["join_date"] = t.join_date.isoformat() if t.join_date else None

    consensuses_raw