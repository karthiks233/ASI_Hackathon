"""Reroute / delay / altitude mitigations: evaluate (what-if) and apply."""
from __future__ import annotations

import copy
import logging
import uuid
from datetime import timedelta

import numpy as np

from .config import REROUTE_OFFSET_DEG
from .schemas import Mitigation, MitigationImpact
from .simulation import ScenarioState, compute_frame, rebuild_flight

log = logging.getLogger(__name__)


# ── Conflict snapshot helpers ───────────────────────────────────────────────────

def _conflict_key_set(state: ScenarioState) -> set[tuple]:
    """Snapshot all current conflicts as a set of (kind, sector_or_flight) tuples."""
    from .simulation import compute_frame
    from datetime import timedelta
    # Use peak-ish time for diffing
    mid_t = state.t_start + (state.t_end - state.t_start) / 2
    frame = compute_frame(state, mid_t)
    keys: set[tuple] = set()
    for c in frame.conflicts:
        if c.sector_name:
            keys.add((c.kind, c.sector_name))
        else:
            for fid in c.flight_ids:
                keys.add((c.kind, fid))
    return keys


def _diff_impacts(before: set[tuple], after: set[tuple]) -> tuple[int, int]:
    resolved = len(before - after)
    created = len(after - before)
    return resolved, created


# ── REROUTE ────────────────────────────────────────────────────────────────────

def _offset_waypoints(
    lats: np.ndarray,
    lons: np.ndarray,
    side: str,
    around: list[str],
    sectors: dict,
) -> tuple[np.ndarray, np.ndarray]:
    """Shift waypoints that fall inside the `around` sectors laterally."""
    from shapely.geometry import Point

    new_lats = lats.copy()
    new_lons = lons.copy()

    lat_off = 0.0
    lon_off = 0.0
    if side == "north":
        lat_off = REROUTE_OFFSET_DEG
    elif side == "south":
        lat_off = -REROUTE_OFFSET_DEG
    elif side == "east":
        lon_off = REROUTE_OFFSET_DEG
    elif side == "west":
        lon_off = -REROUTE_OFFSET_DEG

    around_geoms = [sectors[n].geom for n in around if n in sectors]
    if not around_geoms:
        # Offset all waypoints if sectors not found
        return lats + lat_off, lons + lon_off

    for idx, (lat, lon) in enumerate(zip(lats, lons)):
        pt = Point(lon, lat)
        for geom in around_geoms:
            if geom.contains(pt) or geom.distance(pt) < 0.5:
                new_lats[idx] = lat + lat_off
                new_lons[idx] = lon + lon_off
                break

    return new_lats, new_lons


def evaluate_reroute(
    state: ScenarioState,
    flight_id: str,
    side: str,
    around: list[str],
) -> tuple[MitigationImpact, dict]:
    """What-if reroute. Returns (impact, params_dict). Does NOT commit."""
    flight = state.flights.get(flight_id)
    if flight is None:
        raise ValueError(f"Flight {flight_id} not found")

    before = _conflict_key_set(state)

    # Clone and mutate
    new_flight = flight.clone()
    new_lats, new_lons = _offset_waypoints(flight.lats, flight.lons, side, around, state.sectors)
    new_flight.lats = new_lats
    new_flight.lons = new_lons

    # Compute new timing from extra distance
    from .geo import cum_dist_nm
    old_cum = cum_dist_nm(flight.lats, flight.lons)
    new_cum = cum_dist_nm(new_lats, new_lons)
    extra_nm = float(new_cum[-1] - old_cum[-1])
    if flight.cruise_speed_kt > 0:
        delay_min = (extra_nm / flight.cruise_speed_kt) * 60
    else:
        delay_min = 0.0
    new_flight.t1 = flight.t1 + timedelta(minutes=delay_min)

    # Temporarily rebuild to measure impact
    original_visits = copy.deepcopy(state.visits_by_flight.get(flight_id, []))
    rebuild_flight(state, new_flight)
    after = _conflict_key_set(state)
    resolved, created = _diff_impacts(before, after)

    # Restore original flight
    old_flight_copy = flight.clone()
    rebuild_flight(state, old_flight_copy)
    state.visits_by_flight[flight_id] = original_visits
    # Patch visits_by_sector back
    for visits in state.visits_by_sector.values():
        for v in visits:
            pass  # already handled by rebuild_flight restoring

    return (
        MitigationImpact(
            conflicts_resolved=resolved,
            conflicts_created=created,
            delay_min=round(delay_min, 1),
            extra_nm=round(extra_nm, 1),
        ),
        {"side": side, "around": around},
    )


def apply_reroute(state: ScenarioState, flight_id: str, side: str, around: list[str]) -> MitigationImpact:
    flight = state.flights[flight_id]
    before = _conflict_key_set(state)

    new_flight = flight.clone()
    new_lats, new_lons = _offset_waypoints(flight.lats, flight.lons, side, around, state.sectors)
    new_flight.lats = new_lats
    new_flight.lons = new_lons

    from .geo import cum_dist_nm
    old_cum = cum_dist_nm(flight.lats, flight.lons)
    new_cum = cum_dist_nm(new_lats, new_lons)
    extra_nm = float(new_cum[-1] - old_cum[-1])
    delay_min = (extra_nm / flight.cruise_speed_kt) * 60 if flight.cruise_speed_kt > 0 else 0.0
    new_flight.t1 = flight.t1 + timedelta(minutes=delay_min)

    rebuild_flight(state, new_flight)
    after = _conflict_key_set(state)
    resolved, created = _diff_impacts(before, after)
    state.total_delay_min += delay_min

    return MitigationImpact(
        conflicts_resolved=resolved,
        conflicts_created=created,
        delay_min=round(delay_min, 1),
        extra_nm=round(extra_nm, 1),
    )


# ── DELAY ──────────────────────────────────────────────────────────────────────

def evaluate_delay(
    state: ScenarioState,
    flight_id: str,
    minutes: float,
) -> tuple[MitigationImpact, dict]:
    flight = state.flights.get(flight_id)
    if flight is None:
        raise ValueError(f"Flight {flight_id} not found")

    before = _conflict_key_set(state)

    new_flight = flight.clone()
    new_flight.t0 = flight.t0 + timedelta(minutes=minutes)
    new_flight.t1 = flight.t1 + timedelta(minutes=minutes)

    original_visits = copy.deepcopy(state.visits_by_flight.get(flight_id, []))
    rebuild_flight(state, new_flight)
    after = _conflict_key_set(state)
    resolved, created = _diff_impacts(before, after)

    # Restore
    old_flight_copy = flight.clone()
    rebuild_flight(state, old_flight_copy)
    state.visits_by_flight[flight_id] = original_visits

    return (
        MitigationImpact(
            conflicts_resolved=resolved,
            conflicts_created=created,
            delay_min=round(minutes, 1),
            extra_nm=0.0,
        ),
        {"minutes": minutes},
    )


def apply_delay(state: ScenarioState, flight_id: str, minutes: float) -> MitigationImpact:
    flight = state.flights[flight_id]
    before = _conflict_key_set(state)

    new_flight = flight.clone()
    new_flight.t0 = flight.t0 + timedelta(minutes=minutes)
    new_flight.t1 = flight.t1 + timedelta(minutes=minutes)

    rebuild_flight(state, new_flight)
    after = _conflict_key_set(state)
    resolved, created = _diff_impacts(before, after)
    state.total_delay_min += minutes

    return MitigationImpact(
        conflicts_resolved=resolved,
        conflicts_created=created,
        delay_min=round(minutes, 1),
        extra_nm=0.0,
    )


# ── ALTITUDE ───────────────────────────────────────────────────────────────────

def evaluate_altitude(
    state: ScenarioState,
    flight_id: str,
    new_alt_ft: int,
) -> tuple[MitigationImpact, dict]:
    flight = state.flights.get(flight_id)
    if flight is None:
        raise ValueError(f"Flight {flight_id} not found")

    before = _conflict_key_set(state)

    new_flight = flight.clone()
    new_flight.cruise_alt_ft = new_alt_ft
    new_flight.band = "HIGH" if new_alt_ft >= 35000 else "LOW"

    original_visits = copy.deepcopy(state.visits_by_flight.get(flight_id, []))
    rebuild_flight(state, new_flight)
    after = _conflict_key_set(state)
    resolved, created = _diff_impacts(before, after)

    # Restore
    old_flight_copy = flight.clone()
    rebuild_flight(state, old_flight_copy)
    state.visits_by_flight[flight_id] = original_visits

    return (
        MitigationImpact(
            conflicts_resolved=resolved,
            conflicts_created=created,
            delay_min=0.0,
            extra_nm=0.0,
        ),
        {"new_alt_ft": new_alt_ft},
    )


def apply_altitude(state: ScenarioState, flight_id: str, new_alt_ft: int) -> MitigationImpact:
    flight = state.flights[flight_id]
    before = _conflict_key_set(state)

    new_flight = flight.clone()
    new_flight.cruise_alt_ft = new_alt_ft
    new_flight.band = "HIGH" if new_alt_ft >= 35000 else "LOW"

    rebuild_flight(state, new_flight)
    after = _conflict_key_set(state)
    resolved, created = _diff_impacts(before, after)

    return MitigationImpact(
        conflicts_resolved=resolved,
        conflicts_created=created,
        delay_min=0.0,
        extra_nm=0.0,
    )


# ── Dispatch ────────────────────────────────────────────────────────────────────

def apply_mitigation(state: ScenarioState, mit: Mitigation) -> MitigationImpact:
    fid = mit.flight_id
    if mit.action == "REROUTE":
        p = mit.params
        return apply_reroute(state, fid, p["side"], p.get("around", []))
    elif mit.action == "DELAY":
        return apply_delay(state, fid, float(mit.params["minutes"]))
    elif mit.action == "ALTITUDE":
        return apply_altitude(state, fid, int(mit.params["new_alt_ft"]))
    else:
        raise ValueError(f"Unknown action: {mit.action}")


# ── Deterministic solver (fallback) ────────────────────────────────────────────

def deterministic_best(state: ScenarioState) -> Mitigation | None:
    """Pick the worst conflict, try the three what-ifs, return the best."""
    from .simulation import compute_frame
    mid_t = state.t_start + (state.t_end - state.t_start) / 2
    frame = compute_frame(state, mid_t)
    if not frame.conflicts:
        return None

    # Find worst over-demand or closed-sector conflict
    worst = max(frame.conflicts, key=lambda c: c.severity)
    if not worst.flight_ids:
        return None

    # Pick the most frequently conflicted flight
    flight_id = worst.flight_ids[0]
    flight = state.flights.get(flight_id)
    if flight is None:
        return None

    candidates: list[tuple[MitigationImpact, str, dict]] = []

    around = [worst.sector_name] if worst.sector_name else []

    try:
        impact, params = evaluate_reroute(state, flight_id, "north", around)
        candidates.append((impact, "REROUTE", params))
    except Exception:
        pass

    try:
        impact, params = evaluate_delay(state, flight_id, 30.0)
        candidates.append((impact, "DELAY", params))
    except Exception:
        pass

    if flight.cruise_alt_ft < 40000:
        try:
            impact, params = evaluate_altitude(state, flight_id, flight.cruise_alt_ft + 2000)
            candidates.append((impact, "ALTITUDE", params))
        except Exception:
            pass

    if not candidates:
        # Default: 30-min delay
        return Mitigation(
            id="m_" + uuid.uuid4().hex[:6],
            action="DELAY",
            flight_id=flight_id,
            flight_number=flight.flight_number,
            params={"minutes": 30},
            impact=MitigationImpact(conflicts_resolved=0, conflicts_created=0, delay_min=30, extra_nm=0),
            rationale=f"Hold {flight.flight_number} 30 min to relieve {worst.label}.",
        )

    # Pick best by conflicts resolved, then fewest delay minutes
    best_impact, best_action, best_params = max(
        candidates,
        key=lambda x: (x[0].conflicts_resolved, -x[0].delay_min),
    )

    action_label = {"REROUTE": "Reroute", "DELAY": "Delay", "ALTITUDE": "Climb"}[best_action]
    rationale = (
        f"[Offline solver] {action_label} {flight.flight_number} to relieve {worst.label}. "
        f"Estimated: {best_impact.conflicts_resolved} conflict(s) resolved, "
        f"+{best_impact.delay_min:.0f} min delay."
    )

    return Mitigation(
        id="m_" + uuid.uuid4().hex[:6],
        action=best_action,
        flight_id=flight_id,
        flight_number=flight.flight_number,
        params=best_params,
        impact=best_impact,
        rationale=rationale,
    )
