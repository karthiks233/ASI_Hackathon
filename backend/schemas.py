"""Pydantic request/response models — the API contract with the frontend."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel


# ── Health ─────────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    ok: bool
    has_claude: bool
    model: str


# ── Scenarios ──────────────────────────────────────────────────────────────────

class SnapshotInfo(BaseModel):
    id: str
    asked_at: str
    window_start: str
    window_end: str
    flight_count: int


class DisruptionPreset(BaseModel):
    id: str
    label: str
    kind: str
    description: str


class ScenariosResponse(BaseModel):
    snapshots: list[SnapshotInfo]
    presets: list[DisruptionPreset]


# ── Scenario load ──────────────────────────────────────────────────────────────

class ScenarioLoadRequest(BaseModel):
    snapshot_id: str


class ScenarioLoadResponse(BaseModel):
    scenario_id: str
    snapshot_id: str
    t_start: str
    t_end: str
    step_min: int
    flight_count: int
    has_disruption: bool


# ── Frame ──────────────────────────────────────────────────────────────────────

class FlightFrame(BaseModel):
    id: str
    fn: str
    lat: float
    lon: float
    band: str
    alt_ft: int
    status: Literal["ok", "weather", "closed"]


class SectorFrame(BaseModel):
    name: str
    count: int
    capacity: int
    ratio: float
    closed: bool


class ConflictFrame(BaseModel):
    id: str
    kind: Literal["OVER_DEMAND", "WEATHER", "CLOSED_SECTOR"]
    severity: float
    sector_name: str | None = None
    flight_ids: list[str]
    label: str


class MetricsFrame(BaseModel):
    over_demand_sectors: int
    weather_flights: int
    closed_flights: int
    total_delay_min: float
    airborne: int


class FrameResponse(BaseModel):
    t: str
    flights: list[FlightFrame]
    sectors: list[SectorFrame]
    conflicts: list[ConflictFrame]
    metrics: MetricsFrame


# ── Timeline ───────────────────────────────────────────────────────────────────

class TimelineFrameCount(BaseModel):
    over_demand: int
    weather: int
    closed: int
    total: int


class TimelineResponse(BaseModel):
    step_min: int
    frames: list[str]
    baseline: list[TimelineFrameCount]
    current: list[TimelineFrameCount]
    peak_t: str | None
    peak_total: int


# ── Disruption ─────────────────────────────────────────────────────────────────

class DisruptRequest(BaseModel):
    preset_id: str | None = None
    kind: str | None = None
    params: dict[str, Any] | None = None


class DisruptResponse(BaseModel):
    disruption: dict[str, Any]
    timeline: TimelineResponse
    storm_polygon: list[list[float]] | None = None   # [[lon,lat], ...] for the UI overlay


# ── Mitigations ────────────────────────────────────────────────────────────────

class MitigationImpact(BaseModel):
    conflicts_resolved: int
    conflicts_created: int
    delay_min: float
    extra_nm: float


class Mitigation(BaseModel):
    id: str
    action: Literal["REROUTE", "DELAY", "ALTITUDE"]
    flight_id: str
    flight_number: str
    params: dict[str, Any]
    impact: MitigationImpact
    rationale: str


class CopilotSuggestResponse(BaseModel):
    mitigation: Mitigation | None
    world_summary: str
    source: Literal["claude", "fallback"]


class CopilotApplyRequest(BaseModel):
    mitigation_id: str | None = None
    mitigation: Mitigation | None = None


class CopilotApplyResponse(BaseModel):
    applied: Mitigation
    applied_count: int
    timeline: TimelineResponse


class ResetRequest(BaseModel):
    keep_disruption: bool = True


class ResetResponse(BaseModel):
    timeline: TimelineResponse
