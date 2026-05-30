"""Reroute / delay / altitude mitigations: evaluate (what-if) and apply.

Each mitigation moves a single flight. `evaluate_*` builds a candidate flight,
computes its new sector visits WITHOUT touching shared state, and measures impact
via the incremental `impact_of_change` (only the sectors the flight enters/leaves are
re-checked). `apply_*` commits the change with `rebuild_flight`.
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
    build_sector_visits,
    build_trajectory,
    impact_of_change,
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


def _measure(state: ScenarioState, flight_id: str, candidate: Flight, extra_nm: float, delay_min: float) -> MitigationImpact:
    """Impact of switching `flight_id` to `candidate`, without mutating state."""
    new_traj = build_trajectory(candidate)
    new_visits = build_sector_visits(candidate, new_traj, state.strtrees, state.sectors)
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
    """Target the most "winnable" over-demand sector (closest to capacity) and try
    fixes on its contributing flights until one fully clears it. This mirrors the
    strategy the Claude prompt uses, so the fallback is just as effective."""
    from .simulation import build_occupancy

    # Rank over-demand sectors across the WHOLE timeline by overage (closest to
    # capacity first — those are the ones a single fix can fully clear).
    grid = build_occupancy(state)
    targets: list[tuple[str, int]] = []   # (sector_name, peak_overage)
    for sname, occ in grid.items():
        cap = state.sectors[sname].capacity
        overage = max(occ) - cap
        if overage > 0:
            targets.append((sname, overage))
    if not targets:
        return None
    targets.sort(key=lambda x: x[1])

    best: tuple[MitigationImpact, str, dict, str] | None = None  # (impact, action, params, flight_id)

    for sname, _overage in targets[:12]:
        around = [sname]
        # Flights that transit this sector are the contributors
        contributing = list(dict.fromkeys(v.flight_id for v in state.visits_by_sector.get(sname, [])))
        for flight_id in contributing[:6]:
            flight = state.flights.get(flight_id)
            if flight is None:
                continue
            trials: list[tuple[MitigationImpact, str, dict]] = []
            for side in ("north", "south", "east", "west"):
                try:
                    imp, p = evaluate_reroute(state, flight_id, side, around)
                    trials.append((imp, "REROUTE", p))
                except Exception:
                    pass
            try:
                imp, p = evaluate_delay(state, flight_id, 30.0)
                trials.append((imp, "DELAY", p))
            except Exception:
                pass
            new_alt = flight.cruise_alt_ft + (2000 if flight.cruise_alt_ft < 35000 else 4000)
            try:
                imp, p = evaluate_altitude(state, flight_id, new_alt)
                trials.append((imp, "ALTITUDE", p))
            except Exception:
                pass

            # Best trial for this flight: maximize cleared, no new over-demand, least delay
            for imp, action, params in trials:
                if imp.conflicts_created > 0:
                    continue
                key = (imp.conflicts_resolved, -imp.delay_min, -imp.extra_nm)
                if imp.conflicts_resolved >= 1 and (
                    best is None or key > (best[0].conflicts_resolved, -best[0].delay_min, -best[0].extra_nm)
                ):
                    best = (imp, action, params, flight_id)
            # A clean win on this sector — take it
            if best is not None and best[0].conflicts_resolved >= 1:
                break
        if best is not None and best[0].conflicts_resolved >= 1:
            break

    if best is None:
        return None

    impact, action, params, flight_id = best
    flight = state.flights[flight_id]
    target = (params.get("around") or ["an over-demand sector"])[0]
    action_label = {"REROUTE": "Reroute", "DELAY": "Delay", "ALTITUDE": "Move"}[action]
    rationale = (
        f"[Offline solver] {action_label} {flight.flight_number} to clear {target}. "
        f"Resolves {impact.conflicts_resolved} sector(s), +{impact.delay_min:.0f} min, "
        f"+{impact.extra_nm:.0f} nm, no new over-demand."
    )

    return Mitigation(
        id="m_" + uuid.uuid4().hex[:6],
        action=action,
        flight_id=flight_id,
        flight_number=flight.flight_number,
        params=params,
        impact=impact,
        rationale=rationale,
    )
