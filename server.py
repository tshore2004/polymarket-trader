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
from core.arbitrage import CrossPlatformArbScanner
from core.backtest import SignalLogger, ResolutionPoller
from core.executor import TradeExecutor
from core.kalshi_scanner import KalshiScanner
from core.scanner import BackgroundScanner
from core.signal import SignalEngine
from core.starred_traders import StarredTraderStore
from utils.models import Side

templates = Jinja2Templates(directory="templates")
scanner: BackgroundScanner | None = None
kalshi_scanner: KalshiScanner | None = None
arb_scanner: CrossPlatformArbScanner | None = None
starred_store: StarredTraderStore | None = None
signal_logger: SignalLogger | None = None
resolution_poller: ResolutionPoller | None = None


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
    global scanner, kalshi_scanner, arb_scanner, starred_store, signal_logger, resolution_poller
    config = Config.load()
    engine = SignalEngine(config)
    executor = TradeExecutor(config)
    starred_store = engine._lb.starred

    signal_logger = SignalLogger()
    resolution_poller = ResolutionPoller(
        signal_logger, engine._client, poll_interval=1800
    )
    resolution_poller.start()

    scanner = BackgroundScanner(
        engine, executor, config.scan_interval,
        scan_mode=config.scan_mode, signal_logger=signal_logger,
        log_min_score=config.backtest_min_score,
    )
    scanner.start()

    kalshi_scanner = KalshiScanner(config)
    kalshi_scanner.start()

    arb_scanner = CrossPlatformArbScanner(config)

    yield
    scanner.stop()
    kalshi_scanner.stop()
    resolution_poller.stop()


app = FastAPI(title="OddsEdge", lifespan=lifespan)


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


@app.get("/api/starred")
async def api_starred():
    """List all starred traders with their leaderboard stats if available."""
    snap = scanner.get_snapshot()
    trader_lookup = {t.address.lower(): t for t in snap.traders}
    starred = starred_store.get_all()
    result = []
    for st in starred:
        t = trader_lookup.get(st.address.lower())
        entry = {
            "address": st.address,
            "name": st.name,
            "note": st.note,
            "starred_at": st.starred_at,
        }
        if t:
            entry.update({
                "profit": round(t.profit, 2),
                "volume": round(t.volume, 2),
                "num_trades": t.num_trades,
                "pct_positive": round(t.pct_positive, 4),
                "score": round(t.score, 4),
                "consistency_grade": t.consistency_grade,
                "profit_per_trade": round(t.profit_per_trade, 2),
                "on_leaderboard": True,
            })
        else:
            entry.update({
                "profit": 0, "volume": 0, "num_trades": 0,
                "pct_positive": 0, "score": 0,
                "consistency_grade": "?", "profit_per_trade": 0,
                "on_leaderboard": False,
            })
        result.append(entry)
    return JSONResponse(result)


@app.post("/api/star")
async def api_star(address: str = Query(...), name: str = Query(default="")):
    """Star a trader by address."""
    st = starred_store.star(address, name)
    return JSONResponse({
        "starred": True,
        "address": st.address,
        "name": st.name,
        "count": starred_store.count(),
    })


@app.get("/api/trader/{address}")
async def api_trader_profile(address: str):
    """Trader profile: stats, current positions, and activity history for charting."""
    snap = scanner.get_snapshot()
    trader_lookup = {t.address.lower(): t for t in snap.traders}
    t = trader_lookup.get(address.lower())

    # Basic stats
    profile: dict = {}
    if t:
        profile = {
            "address": t.address,
            "name": t.name,
            "profit": round(t.profit, 2),
            "volume": round(t.volume, 2),
            "num_trades": t.num_trades,
            "pct_positive": round(t.pct_positive, 4),
            "score": round(t.score, 4),
            "consistency_grade": t.consistency_grade,
            "profit_per_trade": round(t.profit_per_trade, 2),
            "starred": t.starred,
            "largest_win": round(t.largest_win, 2),
            "closed_positions": t.closed_positions,
            "winning_positions": t.winning_positions,
            "join_date": t.join_date.isoformat() if t.join_date else None,
        }
    else:
        profile = {"address": address, "name": "", "profit": 0, "score": 0}

    # Current positions from cached data
    engine = scanner._engine
    positions_raw = engine._lb._positions.get(address, [])
    positions = []
    for pos in positions_raw:
        if pos.cur_price in (0.0, 1.0):
            continue  # skip resolved
        positions.append({
            "market_id": pos.market_id,
            "title": pos.title,
            "outcome": pos.outcome,
            "size": round(pos.size, 2),
            "avg_price": round(pos.avg_price, 4),
            "cur_price": round(pos.cur_price, 4),
            "current_value": round(pos.current_value, 2),
            "pnl": round((pos.cur_price - pos.avg_price) * pos.size, 2) if pos.avg_price > 0 else 0,
        })

    # Activity history for chart — use cached data from scan enrichment
    activity = engine._lb._activity_cache.get(address, [])

    return JSONResponse({
        "profile": profile,
        "positions": positions,
        "activity": activity,
    })


@app.post("/api/unstar")
async def api_unstar(address: str = Query(...)):
    """Unstar a trader."""
    removed = starred_store.unstar(address)
    return JSONResponse({
        "removed": removed,
        "address": address,
        "count": starred_store.count(),
    })


# ── Backtest / Performance Tracking ──────────────────────────────────────────


@app.get("/api/backtest/performance")
async def api_backtest_performance():
    """Aggregate performance stats: win rate, P&L, factor analysis."""
    return JSONResponse(signal_logger.get_performance())


@app.get("/api/backtest/picks")
async def api_backtest_picks(limit: int = Query(default=50)):
    """Recent tracked picks with resolution status."""
    return JSONResponse(signal_logger.get_recent_picks(limit=limit))


@app.get("/api/backtest/unresolved")
async def api_backtest_unresolved():
    """All picks still awaiting resolution."""
    return JSONResponse(signal_logger.get_unresolved())


@app.post("/api/backtest/poll")
async def api_backtest_poll():
    """Manually trigger a resolution poll cycle."""
    resolved = resolution_poller.poll_once()
    return JSONResponse({
        "resolved_this_cycle": resolved,
        "message": f"Resolved {resolved} pick(s).",
    })


# ── Kalshi endpoints ──────────────────────────────────────────────────────────


@app.get("/api/kalshi/state")
async def api_kalshi_state():
    snap = kalshi_scanner.get_snapshot()
    return JSONResponse({
        "state": snap.state.value,
        "enabled": snap.enabled,
        "markets_loaded": snap.markets_loaded,
        "signal_count": len(snap.signals),
        "last_scan_at": snap.last_scan_at.isoformat() if snap.last_scan_at else None,
        "scan_duration_s": snap.scan_duration_s,
        "error": snap.error,
        "has_api_key": kalshi_scanner._client.enabled,
    })


@app.get("/api/kalshi/signals")
async def api_kalshi_signals():
    snap = kalshi_scanner.get_snapshot()
    return JSONResponse(_jsonify(snap.signals))


@app.post("/api/kalshi/scan")
async def api_kalshi_scan():
    triggered = kalshi_scanner.trigger_scan()
    return JSONResponse({
        "triggered": triggered,
        "message": "Kalshi scan started." if triggered else "Kalshi scanner not available (missing API key).",
    })


# ── Cross-platform arbitrage endpoint ─────────────────────────────────────────


@app.get("/api/arbitrage")
async def api_arbitrage():
    poly_snap = scanner.get_snapshot()
    kalshi_snap = kalshi_scanner.get_snapshot()

    # Gather Polymarket markets from current signals + picks
    poly_markets = list({
        s.market.condition_id: s.market
        for s in poly_snap.signals
    }.values())
    # Also include markets from picks
    for p in poly_snap.picks:
        if p.market.condition_id not in {m.condition_id for m in poly_markets}:
            poly_markets.append(p.market)

    kalshi_markets = [sig.market for sig in kalshi_snap.signals]

    opportunities = arb_scanner.find_opportunities(poly_markets, kalshi_markets)
    return JSONResponse(_jsonify(opportunities))


if __name__ == "__main__":
    import uvicorn

    p = argparse.ArgumentParser(description="OddsEdge — prediction market scanner")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()

    uvicorn.run("server:app", host=args.host, port=args.port, reload=False)
