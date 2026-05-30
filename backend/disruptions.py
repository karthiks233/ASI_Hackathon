"""Disruption injection: STORM, SECTOR_CLOSURE, GROUND_STOP."""
from __future__ import annotations

import logging
from datetime import timedelta

import numpy as np
from shapely.geometry import MultiPolygon, Point, Polygon, box
from shapely.ops import unary_union

from .config import REFC_THRESHOLD_DBZ
from .data_loader import PRESETS
from .geo import WX_COLS, WX_LAT_MAX, WX_LAT_MIN, WX_LON_MAX, WX_LON_MIN, WX_ROWS
from .simulation import ScenarioState, get_wx_strip, rebuild_flight

log = logging.getLogger(__name__)


# ── Storm disruption ────────────────────────────────────────────────────────────

def _build_storm_polygon(state: ScenarioState, refc_dbz: float) -> Polygon | MultiPolygon | None:
    """Union of wx grid pixels with refc >= threshold at the peak wx strip."""
    if not state.wx_strips:
        return None

    # Use the strip nearest the scenario midpoint
    mid_t = state.t_start + (state.t_end - state.t_start) / 2
    strip = get_wx_strip(state.wx_strips, mid_t)
    if strip is None:
        return None

    refc = strip.refc()
    cell_h = (WX_LAT_MAX - WX_LAT_MIN) / WX_ROWS
    cell_w = (WX_LON_MAX - WX_LON_MIN) / WX_COLS

    storm_cells: list[Polygon] = []
    rows, cols = np.where(refc >= refc_dbz)
    for i, j in zip(rows.tolist(), cols.tolist()):
        lat_n = WX_LAT_MAX - i * cell_h
        lat_s = lat_n - cell_h
        lon_w = WX_LON_MIN + j * cell_w
        lon_e = lon_w + cell_w
        storm_cells.append(box(lon_w, lat_s, lon_e, lat_n))

    if not storm_cells:
        return None

    return unary_union(storm_cells).simplify(0.1)


def _polygon_to_coords(geom) -> list[list[float]] | None:
    """Convert a Shapely polygon/multipolygon to a [[lon,lat],...] list for the API."""
    if geom is None:
        return None
    if geom.geom_type == "MultiPolygon":
        # Return the largest polygon
        geom = max(geom.geoms, key=lambda g: g.area)
    if geom.geom_type == "Polygon":
        return [[lon, lat] for lon, lat in geom.exterior.coords]
    return None


def _apply_storm(state: ScenarioState, params: dict) -> list[list[float]] | None:
    refc_dbz = float(params.get("refc_dbz", REFC_THRESHOLD_DBZ))
    close_impacted = bool(params.get("close_impacted_sectors", True))

    storm_geom = _build_storm_polygon(state, refc_dbz)
    if storm_geom is None:
        log.warning("No storm cells found at threshold %.0f dBZ", refc_dbz)
        return None

    if close_impacted:
        for sname, sector in state.sectors.items():
            if sector.band == "HIGH" and storm_geom.intersects(sector.geom):
                intersection = storm_geom.intersection(sector.geom)
                if intersection.area / sector.geom.area > 0.15:   # >15% overlap
                    state.closed_sectors.add(sname)
        log.info("Storm closed %d sectors", len(state.closed_sectors))

    return _polygon_to_coords(storm_geom)


# ── Sector closure ──────────────────────────────────────────────────────────────

def _apply_sector_closure(state: ScenarioState, params: dict) -> None:
    sectors_to_close: list[str] = params.get("sectors", [])
    for sname in sectors_to_close:
        if sname in state.sectors:
            state.closed_sectors.add(sname)
    log.info("Closed %d sectors: %s", len(sectors_to_close), sectors_to_close)


# ── Ground stop ─────────────────────────────────────────────────────────────────

def _parse_dt(s: str):
    from datetime import datetime, timezone
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _apply_ground_stop(state: ScenarioState, params: dict) -> None:
    airport: str = params.get("airport", "KORD")
    hold_min: int = int(params.get("hold_min", 45))
    from_t = _parse_dt(params["from"]) if "from" in params else state.t_start
    to_t = _parse_dt(params["to"]) if "to" in params else state.t_end

    held = 0
    for fid, flight in list(state.flights.items()):
        if flight.origin == airport and from_t <= flight.t0 < to_t:
            new_flight = flight.clone()
            new_flight.t0 = flight.t0 + timedelta(minutes=hold_min)
            new_flight.t1 = flight.t1 + timedelta(minutes=hold_min)
            rebuild_flight(state, new_flight)
            state.total_delay_min += hold_min
            held += 1
    log.info("Ground stop: held %d departures from %s by %d min", held, airport, hold_min)


# ── Presets ─────────────────────────────────────────────────────────────────────

_PRESET_CONFIGS: dict[str, dict] = {
    "midwest_storm": {
        "kind": "STORM",
        "params": {"refc_dbz": 40, "close_impacted_sectors": True},
    },
    "ord_ground_stop": {
        "kind": "GROUND_STOP",
        "params": {
            "airport": "KORD",
            "hold_min": 45,
        },
    },
    "northeast_closure": {
        "kind": "SECTOR_CLOSURE",
        "params": {"sectors": ["HIGH_001", "HIGH_002", "HIGH_003", "HIGH_004", "HIGH_005"]},
    },
}


def apply_disruption(state: ScenarioState, body: dict) -> tuple[dict, list[list[float]] | None]:
    """Apply a disruption to state. Returns (disruption_info_dict, storm_polygon_coords|None)."""
    preset_id = body.get("preset_id")
    if preset_id:
        config = _PRESET_CONFIGS.get(preset_id)
        if config is None:
            raise ValueError(f"Unknown preset: {preset_id}")
        kind = config["kind"]
        params = dict(config["params"])
        # Set dynamic time bounds for ground stop
        if kind == "GROUND_STOP" and "from" not in params:
            params["from"] = state.t_start.isoformat()
            params["to"] = state.t_end.isoformat()
    else:
        kind = body.get("kind")
        params = body.get("params", {})
        if not kind:
            raise ValueError("Must provide preset_id or kind")

    state.disruption = {"kind": kind, "params": params}
    state.closed_sectors = set()

    storm_polygon = None
    if kind == "STORM":
        storm_polygon = _apply_storm(state, params)
    elif kind == "SECTOR_CLOSURE":
        _apply_sector_closure(state, params)
    elif kind == "GROUND_STOP":
        _apply_ground_stop(state, params)
    else:
        raise ValueError(f"Unknown disruption kind: {kind}")

    return state.disruption, storm_polygon


def reset_disruption(state: ScenarioState) -> None:
    """Remove the disruption (requires rebuilding all ground-stop-affected flights from original)."""
    state.disruption = None
    state.closed_sectors = set()
