"""Trajectory interpolation, sector occupancy, conflict detection, scenario state."""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
from shapely.geometry import Point
from shapely.strtree import STRtree

from .config import REFC_THRESHOLD_DBZ, SIM_STEP_MIN, VISIT_SAMPLE_MIN
from .data_loader import Flight, Sector, WxStrip, build_strtrees
from .geo import cum_dist_nm, interpolate_position, latlon_to_pixel
from .schemas import (
    ConflictFrame,
    FlightFrame,
    FrameResponse,
    MetricsFrame,
    SectorFrame,
    TimelineFrameCount,
    TimelineResponse,
)

log = logging.getLogger(__name__)


# ── Trajectory ─────────────────────────────────────────────────────────────────

@dataclass
class Trajectory:
    flight_id: str
    cum_nm: np.ndarray
    total_nm: float
    t0: datetime
    t1: datetime
    lats: np.ndarray
    lons: np.ndarray

    def position(self, t: datetime) -> tuple[float, float] | None:
        if t < self.t0 or t > self.t1:
            return None
        duration = (self.t1 - self.t0).total_seconds()
        if duration < 1:
            return float(self.lats[0]), float(self.lons[0])
        f = (t - self.t0).total_seconds() / duration
        f = max(0.0, min(1.0, f))
        target_nm = f * self.total_nm
        return interpolate_position(self.lats, self.lons, self.cum_nm, target_nm)


def build_trajectory(flight: Flight) -> Trajectory:
    c = cum_dist_nm(flight.lats, flight.lons)
    return Trajectory(
        flight_id=flight.flight_id,
        cum_nm=c,
        total_nm=float(c[-1]),
        t0=flight.t0,
        t1=flight.t1,
        lats=flight.lats,
        lons=flight.lons,
    )


# ── Sector visit precomputation ─────────────────────────────────────────────────

@dataclass
class SectorVisit:
    flight_id: str
    sector_name: str
    enter_t: datetime
    exit_t: datetime


def build_sector_visits(
    flight: Flight,
    traj: Trajectory,
    strtrees: dict[str, tuple[STRtree, list[str]]],
    sectors: dict[str, Sector],
) -> list[SectorVisit]:
    """Sample a flight's trajectory and collapse into sector visit intervals."""
    tree, names = strtrees.get(flight.band, (None, None))
    if tree is None:
        return []

    duration_min = (flight.t1 - flight.t0).total_seconds() / 60
    n_samples = max(2, int(duration_min / VISIT_SAMPLE_MIN) + 1)
    sample_times = [
        flight.t0 + timedelta(minutes=i * VISIT_SAMPLE_MIN)
        for i in range(n_samples)
    ]
    if sample_times[-1] < flight.t1:
        sample_times.append(flight.t1)

    visits: list[SectorVisit] = []
    current_sector: str | None = None
    current_enter: datetime | None = None

    for t in sample_times:
        pos = traj.position(t)
        if pos is None:
            continue
        lat, lon = pos
        pt = Point(lon, lat)

        # Query the STRtree for candidate sectors, then check containment
        candidates = tree.query(pt)
        found: str | None = None
        for idx in candidates:
            if sectors[names[idx]].geom.contains(pt):
                found = names[idx]
                break

        if found != current_sector:
            if current_sector is not None and current_enter is not None:
                visits.append(SectorVisit(
                    flight_id=flight.flight_id,
                    sector_name=current_sector,
                    enter_t=current_enter,
                    exit_t=t,
                ))
            current_sector = found
            current_enter = t

    # Close the last open visit
    if current_sector is not None and current_enter is not None:
        visits.append(SectorVisit(
            flight_id=flight.flight_id,
            sector_name=current_sector,
            enter_t=current_enter,
            exit_t=flight.t1,
        ))

    return visits


# ── Scenario state ─────────────────────────────────────────────────────────────

@dataclass
class ScenarioState:
    scenario_id: str
    snapshot_id: str
    flights: dict[str, Flight]
    trajectories: dict[str, Trajectory]
    visits_by_sector: dict[str, list[SectorVisit]]
    visits_by_flight: dict[str, list[SectorVisit]]
    sectors: dict[str, Sector]
    strtrees: dict[str, tuple[STRtree, list[str]]]
    wx_strips: list[WxStrip]
    t_start: datetime
    t_end: datetime
    disruption: dict | None = None
    closed_sectors: set[str] = field(default_factory=set)
    applied: list = field(default_factory=list)          # list[schemas.Mitigation]
    pending_mitigation: object = None                    # schemas.Mitigation | None
    baseline_timeline: dict[str, list[TimelineFrameCount]] = field(default_factory=dict)
    baseline_frames: list[str] = field(default_factory=list)
    total_delay_min: float = 0.0


def build_scenario(
    snapshot_id: str,
    flights: dict[str, Flight],
    sectors: dict[str, Sector],
    wx_strips: list[WxStrip],
    t_start: datetime,
    t_end: datetime,
    cached_visits: dict[str, list[SectorVisit]] | None = None,
) -> ScenarioState:
    strtrees = build_strtrees(sectors)
    trajectories = {fid: build_trajectory(f) for fid, f in flights.items()}

    if cached_visits is not None:
        visits_by_flight = cached_visits
        log.info("Loaded sector visits from cache (%d flights)", len(visits_by_flight))
    else:
        log.info("Building sector visit timelines for %d flights…", len(flights))
        visits_by_flight: dict[str, list[SectorVisit]] = {}
        for i, (fid, flight) in enumerate(flights.items()):
            traj = trajectories[fid]
            visits_by_flight[fid] = build_sector_visits(flight, traj, strtrees, sectors)
            if (i + 1) % 1000 == 0:
                log.info("  … %d / %d flights processed", i + 1, len(flights))

    visits_by_sector: dict[str, list[SectorVisit]] = {}
    for visit_list in visits_by_flight.values():
        for v in visit_list:
            visits_by_sector.setdefault(v.sector_name, []).append(v)

    scenario_id = "sc_" + uuid.uuid4().hex[:8]
    state = ScenarioState(
        scenario_id=scenario_id,
        snapshot_id=snapshot_id,
        flights=flights,
        trajectories=trajectories,
        visits_by_sector=visits_by_sector,
        visits_by_flight=visits_by_flight,
        sectors=sectors,
        strtrees=strtrees,
        wx_strips=wx_strips,
        t_start=t_start,
        t_end=t_end,
    )
    return state


def rebuild_flight(state: ScenarioState, flight: Flight) -> None:
    """Recompute trajectory and sector visits for a single flight, update state in place."""
    fid = flight.flight_id
    state.flights[fid] = flight
    traj = build_trajectory(flight)
    state.trajectories[fid] = traj

    # Remove old visits from visits_by_sector
    old_visits = state.visits_by_flight.get(fid, [])
    for v in old_visits:
        sector_list = state.visits_by_sector.get(v.sector_name, [])
        state.visits_by_sector[v.sector_name] = [x for x in sector_list if x.flight_id != fid]

    # Build new visits
    new_visits = build_sector_visits(flight, traj, state.strtrees, state.sectors)
    state.visits_by_flight[fid] = new_visits
    for v in new_visits:
        state.visits_by_sector.setdefault(v.sector_name, []).append(v)


# ── Weather lookup ──────────────────────────────────────────────────────────────

def get_wx_strip(wx_strips: list[WxStrip], t: datetime) -> WxStrip | None:
    """Return the wx strip whose valid window contains t."""
    for strip in wx_strips:
        if strip.valid_from <= t < strip.valid_to:
            return strip
    # Fall back to nearest strip
    if not wx_strips:
        return None
    closest = min(wx_strips, key=lambda s: abs((s.valid_from - t).total_seconds()))
    return closest


# ── Frame computation ───────────────────────────────────────────────────────────

def _snap_t(t: datetime, t_start: datetime, step_min: int = SIM_STEP_MIN) -> datetime:
    offset = round((t - t_start).total_seconds() / 60 / step_min) * step_min
    return t_start + timedelta(minutes=offset)


def compute_frame(state: ScenarioState, t: datetime, band: str | None = None) -> FrameResponse:
    t = _snap_t(t, state.t_start)
    wx_strip = get_wx_strip(state.wx_strips, t)

    # Sector occupancy (optionally restricted to one altitude band)
    sector_counts: dict[str, list[str]] = {}  # sector_name -> [flight_ids]
    for sname, visit_list in state.visits_by_sector.items():
        if band is not None:
            sector = state.sectors.get(sname)
            if sector is None or sector.band != band:
                continue
        fids = [v.flight_id for v in visit_list if v.enter_t <= t < v.exit_t]
        if fids:
            sector_counts[sname] = fids

    # Flight statuses
    flight_frames: list[FlightFrame] = []
    weather_flight_ids: set[str] = set()
    closed_flight_ids: set[str] = set()

    for fid, flight in state.flights.items():
        if band is not None and flight.band != band:
            continue
        pos = state.trajectories[fid].position(t)
        if pos is None:
            continue
        lat, lon = pos

        status = "ok"
        # Weather check
        if wx_strip is not None:
            i, j = latlon_to_pixel(lat, lon)
            refc_val = wx_strip.refc()[i, j]
            retop_val = wx_strip.retop()[i, j]
            if refc_val >= REFC_THRESHOLD_DBZ and retop_val >= flight.cruise_alt_ft:
                status = "weather"
                weather_flight_ids.add(fid)

        # Closed-sector check
        for visit in state.visits_by_flight.get(fid, []):
            if visit.enter_t <= t < visit.exit_t and visit.sector_name in state.closed_sectors:
                status = "closed"
                closed_flight_ids.add(fid)
                break

        flight_frames.append(FlightFrame(
            id=fid,
            fn=flight.flight_number,
            lat=round(lat, 5),
            lon=round(lon, 5),
            band=flight.band,
            alt_ft=flight.cruise_alt_ft,
            status=status,
        ))

    # Sector frames + over-demand conflicts
    sector_frames: list[SectorFrame] = []
    conflicts: list[ConflictFrame] = []
    conflict_idx = 0

    for sname, fids in sector_counts.items():
        sector = state.sectors.get(sname)
        if sector is None:
            continue
        count = len(fids)
        cap = sector.capacity
        ratio = count / cap if cap > 0 else 0.0
        closed = sname in state.closed_sectors
        sector_frames.append(SectorFrame(
            name=sname,
            count=count,
            capacity=cap,
            ratio=round(ratio, 3),
            closed=closed,
        ))
        if count > cap:
            conflicts.append(ConflictFrame(
                id=f"c{conflict_idx}",
                kind="OVER_DEMAND",
                severity=round(ratio, 3),
                sector_name=sname,
                flight_ids=fids,
                label=f"{sname} over capacity ({count}/{cap})",
            ))
            conflict_idx += 1

    # Closed-sector conflicts
    for sname in state.closed_sectors:
        if band is not None:
            sector = state.sectors.get(sname)
            if sector is None or sector.band != band:
                continue
        in_closed = [fid for fid in closed_flight_ids
                     if any(v.sector_name == sname and v.enter_t <= t < v.exit_t
                            for v in state.visits_by_flight.get(fid, []))]
        if in_closed:
            sector = state.sectors.get(sname)
            cap = sector.capacity if sector else 0
            # Add to sector frames if not already there
            existing = [sf for sf in sector_frames if sf.name == sname]
            if not existing:
                sector_frames.append(SectorFrame(
                    name=sname,
                    count=len(in_closed),
                    capacity=cap,
                    ratio=0.0,
                    closed=True,
                ))
            conflicts.append(ConflictFrame(
                id=f"c{conflict_idx}",
                kind="CLOSED_SECTOR",
                severity=1.0,
                sector_name=sname,
                flight_ids=in_closed,
                label=f"{sname} closed ({len(in_closed)} flights inside)",
            ))
            conflict_idx += 1

    # Weather conflicts
    if weather_flight_ids:
        conflicts.append(ConflictFrame(
            id=f"c{conflict_idx}",
            kind="WEATHER",
            severity=1.0,
            sector_name=None,
            flight_ids=list(weather_flight_ids),
            label=f"{len(weather_flight_ids)} flight(s) in storm",
        ))

    # Sort conflicts: over-demand first by severity, then closed, then weather
    conflicts.sort(key=lambda c: (
        {"OVER_DEMAND": 0, "CLOSED_SECTOR": 1, "WEATHER": 2}[c.kind],
        -c.severity,
    ))

    metrics = MetricsFrame(
        over_demand_sectors=sum(1 for c in conflicts if c.kind == "OVER_DEMAND"),
        weather_flights=len(weather_flight_ids),
        closed_flights=len(closed_flight_ids),
        total_delay_min=state.total_delay_min,
        airborne=len(flight_frames),
    )

    return FrameResponse(
        t=t.isoformat(),
        flights=flight_frames,
        sectors=sector_frames,
        conflicts=conflicts,
        metrics=metrics,
    )


# ── Timeline ────────────────────────────────────────────────────────────────────

def compute_timeline(state: ScenarioState, band: str | None = None) -> TimelineResponse:
    frame_times: list[datetime] = []
    t = state.t_start
    while t <= state.t_end:
        frame_times.append(t)
        t += timedelta(minutes=SIM_STEP_MIN)

    current_counts: list[TimelineFrameCount] = []
    for ft in frame_times:
        frame = compute_frame(state, ft, band)
        od = sum(1 for c in frame.conflicts if c.kind == "OVER_DEMAND")
        wx = sum(1 for c in frame.conflicts if c.kind == "WEATHER")
        cl = sum(1 for c in frame.conflicts if c.kind == "CLOSED_SECTOR")
        current_counts.append(TimelineFrameCount(
            over_demand=od, weather=wx, closed=cl, total=od + wx + cl
        ))

    # Determine peak
    peak_idx, peak_total = 0, 0
    for i, fc in enumerate(current_counts):
        if fc.total > peak_total:
            peak_total = fc.total
            peak_idx = i

    baseline = state.baseline_timeline.get(band or "ALL") or [
        TimelineFrameCount(over_demand=0, weather=0, closed=0, total=0)
        for _ in frame_times
    ]

    frames_iso = [ft.isoformat() for ft in frame_times]
    peak_t = frames_iso[peak_idx] if peak_total > 0 else None

    return TimelineResponse(
        step_min=SIM_STEP_MIN,
        frames=frames_iso,
        baseline=baseline,
        current=current_counts,
        peak_t=peak_t,
        peak_total=peak_total,
    )


def freeze_baseline(state: ScenarioState) -> None:
    """Snapshot the current timeline as the "do nothing" baseline, per band.

    Call once right after a disruption is applied (before any mitigations), so the
    UI can overlay baseline vs AI-managed for whichever band it is viewing.
    """
    for band in (None, "HIGH", "LOW"):
        tl = compute_timeline(state, band)
        state.baseline_timeline[band or "ALL"] = tl.current
        if band is None:
            state.baseline_frames = tl.frames
