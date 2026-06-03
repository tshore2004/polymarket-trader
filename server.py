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
from utils.models import Side

templates = Jinja2Templates(directory="templates")
scanner: BackgroundScanner | None = None


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
    global scanner
    config = Config.load()
    engine = SignalEngine(config)
    executor = TradeExecutor(config)
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
    for raw, pick in zip(picks_raw, active_picks):
        raw["dominant_side"] = pick.dominant_side.value
        raw["total_traders"] = pick.num_traders_yes + pick.num_traders_no
        raw["time_category"] = pick.market.time_category
        raw["daily_score"] = pick.daily_score
        raw["category"] = pick.category
        raw["subcategory"] = pick.subcategory
        raw["avg_dominant_win_rate"] = pick.avg_dominant_win_rate
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
    return JSONResponse(picks_raw)


@app.get("/api/leaderboard")
async def api_leaderboard():
    snap = scanner.get_snapshot()
    traders = _jsonify(snap.traders)

    consensuses_raw = _jsonify(snap.consensuses)
    consensuses = []
    for raw, con in zip(consensuses_raw, snap.consensuses):
        raw["confidence"] = round(con.confidence, 4)
        raw["dominant_side"] = con.dominant_side.value
        raw["dominant_weight"] = round(con.dominant_weight, 2)
        consensuses.append(raw)

    return JSONResponse({"traders": traders, "consensuses": consensuses})


@app.get("/api/news")
async def api_news():
    snap = scanner.get_snapshot()
    return JSONResponse(_jsonify(snap.news_signals))


@app.get("/api/volume")
async def api_volume():
    snap = scanner.get_snapshot()
    return JSONResponse(_jsonify(snap.volume_signals))


@app.post("/api/scan")
async def api_scan(mode: str = Query(default=None)):
    triggered = scanner.trigger_scan(mode=mode)
    return JSONResponse({
        "triggered": triggered,
        "mode": mode or scanner.get_snapshot().scan_mode,
        "message": "Scan started." if triggered else "Scan already in progress.",
    })


@app.post("/api/mode")
async def api_set_mode(mode: str = Query(...)):
    scanner.set_mode(mode)
    return JSONResponse({"mode": mode, "message": f"Mode set to {mode!r}."})


if __name__ == "__main__":
    import uvicorn

    p = argparse.ArgumentParser(description="Polymarket Trader web dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()

    uvicorn.run("server:app", host=args.host, port=args.port, reload=False)
