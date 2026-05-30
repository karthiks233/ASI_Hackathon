"""FastAPI application — all HTTP endpoints + static frontend serving."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import ANTHROPIC_API_KEY, CLAUDE_MODEL, DATA_BUNDLE_DIR, SIM_CACHE_DIR, SIM_STEP_MIN
from .copilot import suggest
from .data_loader import (
    load_cache,
    load_flights,
    load_sectors,
    save_cache,
    list_snapshots,
)
from .mitigations import apply_mitigation
from .schemas import (
    CopilotApplyRequest,
    CopilotApplyResponse,
    CopilotSuggestResponse,
    FrameResponse,
    HealthResponse,
    Mitigation,
    ResetRequest,
    ResetResponse,
    ScenarioLoadRequest,
    ScenarioLoadResponse,
    ScenariosResponse,
    TimelineResponse,
)
from .simulation import (
    ScenarioState,
    build_scenario,
    compute_frame,
    compute_timeline,
    freeze_baseline,
)

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(title="AI Flight Disruption War Room", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory scenario registry
_scenarios: dict[str, ScenarioState] = {}

# Cached sectors (loaded once)
_sectors = None
_sectors_geojson_raw = None


def _get_sectors():
    global _sectors, _sectors_geojson_raw
    if _sectors is None:
        _sectors = load_sectors(DATA_BUNDLE_DIR)
        # Cache raw GeoJSON for the sectors endpoint
        geojson_path = DATA_BUNDLE_DIR / "sectors.geojson"
        if not geojson_path.exists():
            geojson_path = DATA_BUNDLE_DIR / "sectors.geojson.gz"
        import gzip, json as _json
        if geojson_path.suffix == ".gz":
            import gzip
            with gzip.open(geojson_path, "rt") as f:
                raw = _json.load(f)
        else:
            with open(geojson_path) as f:
                raw = _json.load(f)
        # Annotate features with band + scaled capacity
        for feat in raw["features"]:
            props = feat["properties"]
            name = props["name"]
            sector = _sectors.get(name)
            if sector:
                props["band"] = sector.band
                props["alt_from_ft"] = sector.alt_from_ft
                props["alt_to_ft"] = sector.alt_to_ft
                props["capacity"] = sector.capacity
        _sectors_geojson_raw = raw
    return _sectors


def _get_scenario(scenario_id: str) -> ScenarioState:
    state = _scenarios.get(scenario_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Scenario {scenario_id} not found")
    return state


def _parse_t(t_str: str) -> datetime:
    try:
        dt = datetime.fromisoformat(t_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid timestamp: {t_str!r}")


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/api/health", response_model=HealthResponse)
def health():
    return HealthResponse(
        ok=True,
        has_claude=bool(ANTHROPIC_API_KEY),
        model=CLAUDE_MODEL,
    )


# ── Scenarios ──────────────────────────────────────────────────────────────────

@app.get("/api/scenarios", response_model=ScenariosResponse)
def get_scenarios():
    snapshots = list_snapshots(DATA_BUNDLE_DIR)
    # No injected disruptions in this build — we solve the dataset's own over-demand.
    return ScenariosResponse(snapshots=snapshots, presets=[])


# ── Scenario load ──────────────────────────────────────────────────────────────

@app.post("/api/scenario/load", response_model=ScenarioLoadResponse)
def scenario_load(body: ScenarioLoadRequest):
    snapshot_id = body.snapshot_id
    sectors = _get_sectors()

    # Check for cached visits
    cached = load_cache(snapshot_id)
    if cached is not None:
        cached_visits, t_start_iso, t_end_iso = cached
        log.info("Cache hit for snapshot %s", snapshot_id)
    else:
        cached_visits = None
        t_start_iso = None
        t_end_iso = None

    flights, window_start_iso, window_end_iso = load_flights(snapshot_id, DATA_BUNDLE_DIR)

    # Use a narrower window so the demo is fast: 4 hours around the snapshot
    t_start = _parse_t(window_start_iso) if t_start_iso is None else _parse_t(t_start_iso)
    # Cap to 6-hour window for demo speed
    from datetime import timedelta
    t_end_candidate = t_start + timedelta(hours=6)
    t_end = _parse_t(window_end_iso) if t_end_iso is None else _parse_t(t_end_iso)
    t_end = min(t_end, t_end_candidate)

    state = build_scenario(
        snapshot_id=snapshot_id,
        flights=flights,
        sectors=sectors,
        wx_strips=[],            # weather not used in the over-demand-only build
        t_start=t_start,
        t_end=t_end,
        cached_visits=cached_visits,
    )

    if cached_visits is None:
        log.info("Saving sector visit cache for snapshot %s", snapshot_id)
        save_cache(snapshot_id, (state.visits_by_flight, t_start.isoformat(), t_end.isoformat()))

    # The dataset's natural over-demand IS the problem set — freeze it now as the
    # "do nothing" baseline so the timeline can overlay baseline vs AI-managed.
    freeze_baseline(state)

    _scenarios[state.scenario_id] = state

    return ScenarioLoadResponse(
        scenario_id=state.scenario_id,
        snapshot_id=snapshot_id,
        t_start=t_start.isoformat(),
        t_end=t_end.isoformat(),
        step_min=SIM_STEP_MIN,
        flight_count=len(flights),
        has_disruption=False,
    )


# ── Sectors GeoJSON ────────────────────────────────────────────────────────────

@app.get("/api/scenario/{scenario_id}/sectors.geojson")
def get_sectors_geojson(scenario_id: str):
    _get_scenario(scenario_id)  # validate exists
    _get_sectors()
    return JSONResponse(content=_sectors_geojson_raw)


# ── Frame ──────────────────────────────────────────────────────────────────────

@app.get("/api/scenario/{scenario_id}/frame", response_model=FrameResponse)
def get_frame(scenario_id: str, t: str, band: str | None = None):
    state = _get_scenario(scenario_id)
    t_dt = _parse_t(t)
    return compute_frame(state, t_dt, band)


# ── Timeline ───────────────────────────────────────────────────────────────────

@app.get("/api/scenario/{scenario_id}/timeline", response_model=TimelineResponse)
def get_timeline(scenario_id: str, band: str | None = None):
    state = _get_scenario(scenario_id)
    return compute_timeline(state, band)


# ── Co-pilot suggest ───────────────────────────────────────────────────────────

@app.post("/api/scenario/{scenario_id}/copilot/suggest", response_model=CopilotSuggestResponse)
def post_copilot_suggest(scenario_id: str):
    state = _get_scenario(scenario_id)
    result = suggest(state)
    state.pending_mitigation = result.mitigation
    return result


# ── Co-pilot apply ─────────────────────────────────────────────────────────────

@app.post("/api/scenario/{scenario_id}/copilot/apply", response_model=CopilotApplyResponse)
def post_copilot_apply(scenario_id: str, body: CopilotApplyRequest, band: str | None = None):
    state = _get_scenario(scenario_id)

    # Resolve the mitigation
    mitigation: Mitigation | None = None
    if body.mitigation_id and state.pending_mitigation and state.pending_mitigation.id == body.mitigation_id:
        mitigation = state.pending_mitigation
    elif body.mitigation:
        mitigation = body.mitigation
    elif state.pending_mitigation:
        mitigation = state.pending_mitigation

    if mitigation is None:
        raise HTTPException(status_code=400, detail="No mitigation to apply")

    try:
        impact = apply_mitigation(state, mitigation)
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    mitigation.impact = impact
    state.applied.append(mitigation)
    state.pending_mitigation = None

    timeline = compute_timeline(state, band)

    return CopilotApplyResponse(
        applied=mitigation,
        applied_count=len(state.applied),
        timeline=timeline,
    )


# ── Reset ──────────────────────────────────────────────────────────────────────

@app.post("/api/scenario/{scenario_id}/reset", response_model=ResetResponse)
def post_reset(scenario_id: str, body: ResetRequest, band: str | None = None):
    """Drop all applied mitigations and restore the dataset's natural baseline."""
    state = _get_scenario(scenario_id)

    # Reload fresh flights (to undo all flight mutations from applied mitigations)
    sectors = _get_sectors()
    flights, _, _ = load_flights(state.snapshot_id, DATA_BUNDLE_DIR)

    cached = load_cache(state.snapshot_id)
    cached_visits = cached[0] if cached else None

    new_state = build_scenario(
        snapshot_id=state.snapshot_id,
        flights=flights,
        sectors=sectors,
        wx_strips=[],
        t_start=state.t_start,
        t_end=state.t_end,
        cached_visits=cached_visits,
    )
    new_state.scenario_id = state.scenario_id  # keep the same ID
    freeze_baseline(new_state)

    _scenarios[state.scenario_id] = new_state
    timeline = compute_timeline(new_state, band)

    return ResetResponse(timeline=timeline)


# ── Static frontend ────────────────────────────────────────────────────────────

_frontend_dir = Path(__file__).parent.parent / "frontend"
if _frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")
else:
    log.warning("frontend/ directory not found — UI will not be served")
