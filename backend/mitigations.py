"""Reroute / delay / altitude mitigations: evaluate (what-if) and apply.

Each mitigation moves one or more flights. `evaluate_*` builds candidate flights,
computes their new sector visits WITHOUT touching shared state, and measures impact
incrementally (only the sectors involved are re-checked). `apply_*` commits via
`rebuild_flight`. The deterministic solver builds a "ground delay program" — it holds
just enough contributing flights to bring an over-demand sector back under capacity —
so it can reliably clear a sector even when one flight is not enough.
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

import numpy as np

from .config import REROUTE_OFFSET_DEG
from .data_loader import Flight
from .geo import cum_dist_nm
from .schemas import Mitigation, MitigationImpact
from .simulation import (
    ScenarioState,
    build_occupancy,
    build_sector_visits,
    build_trajectory,
    impact_of_change,
    impact_of_changes,
    rebuild_flight,
)

log = logging.getLogger(__name__)


# ── Candidate builders (no state mutation) ──────────────────────────────────────

def _offset_waypoints(
    lats: np.ndarray,
    lons: np.ndarray,
    side: str,
    around: list[str],
    sectors: dict,
) -> tuple[np.ndarray, np.ndarray]:
    """Shift the waypoints that fall in/near the `around` sectors laterally."""
    from shapely.geometry import Point

    new_lats = lats.copy()
    new_lons = lons.copy()

    lat_off = lon_off = 0.0
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
        return lats + lat_off, lons + lon_off

    for idx, (lat, lon) in enumerate(zip(lats, lons)):
        pt = Point(lon, lat)
        for geom in around_geoms:
            if geom.contains(pt) or geom.distance(pt) < 0.5:
                new_lats[idx] = lat + lat_off
                new_lons[idx] = lon + lon_off
                break
    return new_lats, new_lons


def _reroute_candidate(state: ScenarioState, flight: Flight, side: str, around: list[str]) -> tuple[Flight, float, float]:
    new_flight = flight.clone()
    new_lats, new_lons = _offset_waypoints(flight.lats, flight.lons, side, around, state.sectors)
    new_flight.lats = new_lats
    new_flight.lons = new_lons
    extra_nm = float(cum_dist_nm(new_lats, new_lons)[-1] - cum_dist_nm(flight.lats, flight.lons)[-1])
    delay_min = (extra_nm / flight.cruise_speed_kt) * 60 if flight.cruise_speed_kt > 0 else 0.0
    new_flight.t1 = flight.t1 + timedelta(minutes=delay_min)
    return new_flight, round(extra_nm, 1), round(delay_min, 1)


def _delay_candidate(flight: Flight, minutes: float) -> tuple[Flight, float, float]:
    new_flight = flight.clone()
    new_flight.t0 = flight.t0 + timedelta(minutes=minutes)
    new_flight.t1 = flight.t1 + timedelta(minutes=minutes)
    return new_flight, 0.0, round(minutes, 1)


def _altitude_candidate(flight: Flight, new_alt_ft: int) -> tuple[Flight, float, float]:
    new_flight = flight.clone()
    new_flight.cruise_alt_ft = new_alt_ft
    new_flight.band = "HIGH" if new_alt_ft >= 35000 else "LOW"
    return new_flight, 0.0, 0.0


def _candidate_visits(state: ScenarioState, candidate: Flight) -> list:
    traj = build_trajectory(candidate)
    return build_sector_visits(candidate, traj, state.strtrees, state.sectors)


def _measure(state: ScenarioState, flight_id: str, candidate: Flight, extra_nm: float, delay_min: float) -> MitigationImpact:
    """Impact of switching `flight_id` to `candidate`, without mutating state."""
    new_visits = _candidate_visits(state, candidate)
    resolved, created = impact_of_change(state, flight_id, new_visits)
    return MitigationImpact(
        conflicts_resolved=resolved,
        conflicts_created=created,
        delay_min=delay_min,
        extra_nm=extra_nm,
    )


# ── REROUTE ────────────────────────────────────────────────────────────────────

def evaluate_reroute(state: ScenarioState, flight_id: str, side: str, around: list[str]) -> tuple[MitigationImpact, dict]:
    flight = state.flights.get(flight_id)
    if flight is None:
        raise ValueError(f"Flight {flight_id} not found")
    cand, extra_nm, delay_min = _reroute_candidate(state, flight, side, around)
    return _measure(state, flight_id, cand, extra_nm, delay_min), {"side": side, "around": around}


def apply_reroute(state: ScenarioState, flight_id: str, side: str, around: list[str]) -> MitigationImpact:
    flight = state.flights[flight_id]
    cand, extra_nm, delay_min = _reroute_candidate(state, flight, side, around)
    impact = _measure(state, flight_id, cand, extra_nm, delay_min)
    rebuild_flight(state, cand)
    state.total_delay_min += delay_min
    return impact


# ── DELAY ──────────────────────────────────────────────────────────────────────

def evaluate_delay(state: ScenarioState, flight_id: str, minutes: float) -> tuple[MitigationImpact, dict]:
    flight = state.flights.get(flight_id)
    if flight is None:
        raise ValueError(f"Flight {flight_id} not found")
    cand, extra_nm, delay_min = _delay_candidate(flight, minutes)
    return _measure(state, flight_id, cand, extra_nm, delay_min), {"minutes": minutes}


def apply_delay(state: ScenarioState, flight_id: str, minutes: float) -> MitigationImpact:
    flight = state.flights[flight_id]
    cand, extra_nm, delay_min = _delay_candidate(flight, minutes)
    impact = _measure(state, flight_id, cand, extra_nm, delay_min)
    rebuild_flight(state, cand)
    state.total_delay_min += delay_min
    return impact


# ── ALTITUDE ───────────────────────────────────────────────────────────────────

def evaluate_altitude(state: ScenarioState, flight_id: str, new_alt_ft: int) -> tuple[MitigationImpact, dict]:
    flight = state.flights.get(flight_id)
    if flight is None:
        raise ValueError(f"Flight {flight_id} not found")
    cand, extra_nm, delay_min = _altitude_candidate(flight, new_alt_ft)
    return _measure(state, flight_id, cand, extra_nm, delay_min), {"new_alt_ft": new_alt_ft}


def apply_altitude(state: ScenarioState, flight_id: str, new_alt_ft: int) -> MitigationImpact:
    flight = state.flights[flight_id]
    cand, extra_nm, delay_min = _altitude_candidate(flight, new_alt_ft)
    impact = _measure(state, flight_id, cand, extra_nm, delay_min)
    rebuild_flight(state, cand)
    return impact


# ── GROUND DELAY PROGRAM (multi-flight) ─────────────────────────────────────────

PROGRAM_DELAY_MIN = 60.0


def _program_changes(state: ScenarioState, flight_ids: list[str], minutes: float):
    """Build (flight_id, new_visits) for delaying each flight by `minutes`."""
    changes = []
    for fid in flight_ids:
        flight = state.flights.get(fid)
        if flight is None:
            continue
        cand, _, _ = _delay_candidate(flight, minutes)
        changes.append((fid, _candidate_visits(state, cand)))
    return changes


def apply_ground_program(state: ScenarioState, flight_ids: list[str], minutes: float) -> MitigationImpact:
    changes = _program_changes(state, flight_ids, minutes)
    resolved, created = impact_of_changes(state, changes)
    for fid in flight_ids:
        flight = state.flights.get(fid)
        if flight is None:
            continue
        cand, _, _ = _delay_candidate(flight, minutes)
        rebuild_flight(state, cand)
        state.total_delay_min += minutes
    return MitigationImpact(
        conflicts_resolved=resolved,
        conflicts_created=created,
        delay_min=round(minutes, 1),
        extra_nm=0.0,
    )


# ── Dispatch ────────────────────────────────────────────────────────────────────

def apply_mitigation(state: ScenarioState, mit: Mitigation) -> MitigationImpact:
    fid = mit.flight_id
    if mit.action == "REROUTE":
        p = mit.params
        return apply_reroute(state, fid, p["side"], p.get("around", []))
    elif mit.action == "DELAY":
        # Multi-flight ground delay program if flight_ids present, else single flight.
        flight_ids = mit.params.get("flight_ids")
        minutes = float(mit.params.get("minutes", PROGRAM_DELAY_MIN))
        if flight_ids:
            return apply_ground_program(state, flight_ids, minutes)
        return apply_delay(state, fid, minutes)
    elif mit.action == "ALTITUDE":
        return apply_altitude(state, fid, int(mit.params["new_alt_ft"]))
    else:
        raise ValueError(f"Unknown action: {mit.action}")


# ── Deterministic solver ────────────────────────────────────────────────────────

def deterministic_best(state: ScenarioState) -> Mitigation | None:
    """Pick the most "winnable" over-demand sector (closest to capacity) and build a
    ground delay program that holds just enough of its contributing flights to bring
    it back under capacity — without tipping any other sector over. Reliable: it can
    clear sectors that are several flights over, not just one."""
    grid = build_occupancy(state)
    targets: list[tuple[int, str]] = []   # (overage, sector)
    for sname, occ in grid.items():
        cap = state.sectors[sname].capacity
        overage = max(occ) - cap
        if overage > 0:
            targets.append((overage, sname))
    if not targets:
        return None
    targets.sort(key=lambda x: x[0])   # smallest overage first (most winnable)

    for overage, sname in targets[:15]:
        contributing = list(dict.fromkeys(v.flight_id for v in state.visits_by_sector.get(sname, [])))
        if not contributing:
            continue
        # Hold progressively more of the contributing flights until the sector clears.
        for k in range(overage, min(len(contributing), overage + 4) + 1):
            flight_ids = contributing[:k]
            changes = _program_changes(state, flight_ids, PROGRAM_DELAY_MIN)
            resolved, created = impact_of_changes(state, changes)
            if resolved >= 1 and created == 0:
                primary = state.flights[flight_ids[0]]
                fn = primary.flight_number
                label = f"{fn}" if k == 1 else f"{fn} +{k - 1} more"
                return Mitigation(
                    id="m_" + uuid.uuid4().hex[:6],
                    action="DELAY",
                    flight_id=flight_ids[0],
                    flight_number=label,
                    params={"minutes": PROGRAM_DELAY_MIN, "flight_ids": flight_ids, "around": [sname]},
                    impact=MitigationImpact(
                        conflicts_resolved=resolved,
                        conflicts_created=created,
                        delay_min=PROGRAM_DELAY_MIN,
                        extra_nm=0.0,
                    ),
                    rationale=(
                        f"Hold {label} by {int(PROGRAM_DELAY_MIN)} min to bring {sname} back under "
                        f"capacity — clears {resolved} over-demand sector(s) with no new hotspots."
                    ),
                )
    return None
