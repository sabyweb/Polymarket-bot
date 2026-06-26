"""Dashboard API for the Polymarket reward-farming bot.

Serves a React SPA and read-only JSON endpoints.  Binds to localhost only in
production; access is via SSH tunnel.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import models, queries

_DIR = Path(__file__).parent.resolve()
ROOT = _DIR.parent
DIST = ROOT / "frontend" / "dist"

app = FastAPI(title="Polymarket Reward Farmer Dashboard", version="0.1.0")

# CORS only for local dev; in prod static files are served directly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health", response_model=models.Health)
def health():
    return models.Health(**queries.get_health())


@app.get("/api/portfolio", response_model=models.Portfolio)
def portfolio():
    return models.Portfolio(**queries.get_portfolio())


@app.get("/api/pnl", response_model=models.PnlSummary)
def pnl():
    return models.PnlSummary(**queries.get_pnl_summary())


@app.get("/api/pnl/daily")
def daily_pnl(days: int = Query(14, ge=1, le=90)):
    return queries.get_daily_pnl(days)


@app.get("/api/fills/daily")
def daily_fills(days: int = Query(14, ge=1, le=90)):
    return queries.get_daily_fills(days)


@app.get("/api/fills", response_model=list[models.FillRow])
def fills(limit: int = Query(50, ge=1, le=500), hours: int = Query(24, ge=1, le=168)):
    since = time.time() - hours * 3600
    return [models.FillRow(**r) for r in queries.get_recent_fills(limit, since)]


@app.get("/api/unwinds", response_model=list[models.UnwindRow])
def unwinds(limit: int = Query(50, ge=1, le=500)):
    return [models.UnwindRow(**r) for r in queries.get_recent_unwinds(limit)]


@app.get("/api/orders", response_model=list[models.ActiveOrder])
def orders():
    return [models.ActiveOrder(**r) for r in queries.get_active_orders()]


@app.get("/api/positions", response_model=list[models.PositionRow])
def positions():
    return [models.PositionRow(**r) for r in queries.get_positions()]


@app.get("/api/ab-cohorts/history")
def cohort_history(days: int = Query(2, ge=1, le=7)):
    return queries.get_cohort_history(days)


@app.get("/api/ab-cohorts/latest", response_model=list[models.CohortLatest])
def cohort_latest():
    return [models.CohortLatest(**r) for r in queries.get_cohort_latest()]


@app.get("/api/allocations", response_model=models.Allocation)
def allocations():
    return models.Allocation(**queries.get_allocation())


@app.get("/api/config", response_model=list[models.ConfigEntry])
def config():
    return [models.ConfigEntry(**r) for r in queries.get_config()]


@app.get("/api/rewards/24h", response_model=models.RewardSummary)
def rewards_24h():
    return models.RewardSummary(**queries.get_rewards_24h())


@app.get("/api/rewards/daily")
def rewards_daily(days: int = Query(7, ge=1, le=90)):
    return [models.RewardDaily(**r) for r in queries.get_rewards_daily(days)]


@app.get("/api/logs")
def logs(service: str = Query("polymarket-farmer"), lines: int = Query(100, ge=1, le=500)):
    if service not in {"polymarket-farmer", "polymarket-oversight"}:
        raise HTTPException(status_code=400, detail="unknown service")
    return [models.LogLine(**r) for r in queries.get_logs(service, lines)]


# Static SPA serving
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        index = DIST / "index.html"
        if index.exists():
            return FileResponse(index)
        raise HTTPException(status_code=404, detail="frontend not built")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.main:app", host="127.0.0.1", port=8502, reload=True)
