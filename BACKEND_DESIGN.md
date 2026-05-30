# Backend Design — AI Flight Disruption War Room

> Build target for the **backend** engineer. The contract in §2 is the boundary with the
> frontend — once it's agreed, both sides build in parallel. See `FRONTEND_DESIGN.md` for the
> consumer side and `SYSTEM_DESIGN.md` for the overall vision.

**Stack:** Python 3.13 · FastAPI · Uvicorn · NumPy · Shapely 2 · Anthropic SDK
**Owns:** data loading, the simulation engine, disruptions, mitigations, the Claude co-pilot,
and the HTTP API. Serves the static `frontend/` too.

---

## 1. Responsibilities

1. Load the bundle: `routes.json` (flights), `sectors.geojson` (sectors), `wx/*.npz` (weather).
2. Simulate flight positions over a time window (constant cruise model).
3. Compute sector occupancy and detect conflicts (over-demand / weather / closed-sector).
4. Inject disruptions and recompute.
5. Evaluate & apply mitigations (reroute / delay / altitude).
6. Run the Claude tool-use co-pilot (with a deterministic fallback).
7. Expose everything via the JSON API in §2.

---

## 2. API CONTRACT  ⟵ the frontend/backend boundary

All responses JSON. Times are UTC ISO-8601 strings. `t` query params are ISO-8601.
Base path: `/api`. Errors: `{ "error": "<message>" }` with an appropriate 4xx/5xx status.

### 2.1 `GET /api/health`
```json
{ "ok": true, "has_claude": true, "model": "claude-opus-4-8" }
```

### 2.2 `GET /api/scenarios`
List the snapshots in the bundle and the prebuilt disruption presets.
```json
{
  "snapshots": [
    { "id": "2025-05-29T21:00:00Z", "asked_at": "2025-05-29T21:00:00+00:00",
      "window_start": "2025-05-29T19:00:00+00:00", "window_end": "2025-05-30T13:00:00+00:00",
      "flight_count": 16687 }
  ],
  "presets": [
    { "id": "midwest_storm", "label": "Convective line over the Midwest",
      "kind": "STORM", "description": "Close the HIGH cells under the storm core." },
    { "id": "ord_ground_stop", "label": "ORD ground stop",
      "kind": "GROUND_STOP", "description": "Hold KORD departures 45 min." }
  ]
}
```

### 2.3 `POST /api/scenario/load`
Body: `{ "snapshot_id": "2025-05-29T21:00:00Z" }`
Builds (or loads from cache) the baseline sim. Returns:
```json
{
  "scenario_id": "sc_ab12cd",
  "snapshot_id": "2025-05-29T21:00:00Z",
  "t_start": "2025-05-29T21:00:00+00:00",
  "t_end":   "2025-05-30T01:00:00+00:00",
  "step_min": 5,
  "flight_count": 16687,
  "has_disruption": false
}
```
> The first load of a snapshot may take longer (builds sector timelines); it's cached after.

### 2.4 `GET /api/scenario/{id}/sectors.geojson`
The sector polygons, sent **once** for the map. Standard GeoJSON `FeatureCollection`;
each feature's `properties`: `{ name, band, alt_from_ft, alt_to_ft, capacity }`.
(Capacity here is already the **scaled** capacity the sim uses.)

### 2.5 `GET /api/scenario/{id}/frame?t=<ISO>`
The world state at time `t` (snaps to the nearest frame step). The animation engine.
```json
{
  "t": "2025-05-29T22:40:00+00:00",
  "flights": [
    { "id": "AAL1234|2025-05-29T21:10:00+00:00|KORD",
      "fn": "AAL1234", "lat": 41.9, "lon": -87.6, "band": "HIGH",
      "alt_ft": 37000, "status": "ok" }
    // status ∈ "ok" | "weather" | "closed"
  ],
  "sectors": [
    { "name": "HIGH_142", "count": 9, "capacity": 7, "ratio": 1.29, "closed": false }
    // only sectors with count>0 or closed=true are returned (keep payload small)
  ],
  "conflicts": [
    { "id": "c1", "kind": "OVER_DEMAND", "severity": 1.29, "sector_name": "HIGH_142",
      "flight_ids": ["..."], "label": "HIGH_142 over capacity (9/7)" }
  ],
  "metrics": { "over_demand_sectors": 2, "weather_flights": 5, "closed_flights": 3,
               "total_delay_min": 0, "airborne": 812 }
}
```

### 2.6 `GET /api/scenario/{id}/timeline`
Per-frame conflict counts for the scrubber + peak marker. Includes a baseline ("do nothing")
series and the current (post-mitigation) series so the UI can overlay them.
```json
{
  "step_min": 5,
  "frames": ["2025-05-29T21:00:00+00:00", "..."],
  "baseline":  [{ "over_demand": 0, "weather": 0, "closed": 0, "total": 0 }, ...],
  "current":   [{ "over_demand": 2, "weather": 5, "closed": 3, "total": 10 }, ...],
  "peak_t": "2025-05-29T22:40:00+00:00",
  "peak_total": 14
}
```

### 2.7 `POST /api/scenario/{id}/disrupt`
Body (one of):
```json
{ "preset_id": "midwest_storm" }
// or a custom disruption:
{ "kind": "SECTOR_CLOSURE", "params": { "sectors": ["HIGH_142","HIGH_143"] } }
{ "kind": "GROUND_STOP",    "params": { "airport": "KORD", "hold_min": 45,
                                        "from": "2025-05-29T21:00:00Z", "to": "2025-05-29T23:00:00Z" } }
{ "kind": "STORM",          "params": { "refc_dbz": 40, "close_impacted_sectors": true } }
```
Returns the refreshed `timeline` payload (§2.6) plus `{ "disruption": {...} }`.

### 2.8 `POST /api/scenario/{id}/copilot/suggest`
Body: `{}` (operates on current conflict state). Asks Claude for the **single** next fix.
```json
{
  "mitigation": {
    "id": "m_7f3a",
    "action": "REROUTE",            // REROUTE | DELAY | ALTITUDE
    "flight_id": "AAL1234|...|KORD",
    "flight_number": "AAL1234",
    "params": { "side": "north", "around": ["HIGH_142"] },
    "impact": { "conflicts_resolved": 3, "conflicts_created": 0,
                "delay_min": 7, "extra_nm": 48 },
    "rationale": "AAL1234 is the biggest contributor to the HIGH_142 over-demand at 22:40Z..."
  },
  "world_summary": "2 sectors over capacity (peak 22:40Z); 5 flights in the storm core.",
  "source": "claude"               // "claude" | "fallback"
}
```
If there are no conflicts: `{ "mitigation": null, "world_summary": "All clear." }`.

### 2.9 `POST /api/scenario/{id}/copilot/apply`
Body: `{ "mitigation_id": "m_7f3a" }` (or the full `mitigation` object).
Commits the fix, recomputes the affected flights, returns the refreshed `timeline` (§2.6) plus:
```json
{ "applied": { ...mitigation... }, "applied_count": 1 }
```

### 2.10 `POST /api/scenario/{id}/reset`
Body: `{ "keep_disruption": true }`. Drops applied mitigations (and the disruption if
`keep_disruption=false`). Returns the refreshed `timeline`.

---

## 3. Module layout

```
backend/
  config.py        # env, paths, model id, tuning constants
  geo.py           # haversine, great-circle interpolation, wx grid pixel lookup
  data_loader.py   # parse routes/sectors/wx; build STRtree; pickle cache
  simulation.py    # Trajectory, SectorVisit, FrameState, conflict detection, ScenarioState
  disruptions.py   # storm / sector closure / ground stop + presets
  mitigations.py   # reroute / delay / altitude: evaluate (what-if) + apply
  copilot.py       # Claude tool-use loop + deterministic fallback
  schemas.py       # Pydantic request/response models matching §2
  main.py          # FastAPI app, routes, static mount, in-memory scenario registry
```

---

## 4. Internal data structures

```python
@dataclass
class Flight:
    flight_id: str          # f"{flight_number}|{take_off_time}|{origin}"
    flight_number: str
    origin: str; dest: str
    t0: datetime; t1: datetime          # take_off, landing (mutated by DELAY)
    cruise_alt_ft: int; cruise_speed_kt: int
    band: str                            # "HIGH" | "LOW"
    lats: np.ndarray; lons: np.ndarray   # waypoints (mutated by REROUTE)
    is_airborne: bool

@dataclass
class Trajectory:                        # derived from a Flight
    cum_nm: np.ndarray; total_nm: float
    def position(self, t) -> (lat, lon) | None

@dataclass
class SectorVisit:
    flight_id: str; sector_name: str; enter_t: datetime; exit_t: datetime

@dataclass
class ScenarioState:                     # the mutable per-scenario world
    snapshot_id: str
    flights: dict[str, Flight]
    trajectories: dict[str, Trajectory]
    visits_by_sector: dict[str, list[SectorVisit]]   # for fast occupancy
    sectors: dict[str, Sector]
    wx: WeatherSet
    disruption: Disruption | None
    applied: list[Mitigation]
```

---

## 5. Simulation engine (the technical core)

**Trajectory / position(t)** — constant cruise, no climb/descent (per data spec):
- `cum_nm` = cumulative haversine distance over waypoints; `total_nm` = last value.
- `f = (t - t0)/(t1 - t0)`, clamp to en-route; target = `f*total_nm`; binary-search the
  bracketing segment; linear-interpolate lat/lon. Outside `[t0,t1]` → `None` (not airborne).

**Sector occupancy** — precompute once per flight (key optimization):
- One `STRtree` per band over sector polygons.
- Sample the route every `VISIT_SAMPLE_MIN`; map each sample → sector (tree query +
  `prepared.contains`); collapse runs into `SectorVisit(enter_t, exit_t)`.
- Index visits by sector. Occupancy at `t` = count visits with `enter_t ≤ t < exit_t`.

**Conflicts per frame:**
- `OVER_DEMAND`: `count > capacity`; severity `count/capacity`.
- `WEATHER`: flight pixel `refc ≥ 40` **and** `retop ≥ cruise_alt_ft` in the forecast valid at `t`.
- `CLOSED_SECTOR`: flight inside a sector flagged closed.

**Performance:** build the per-flight visit timelines lazily/at load and **pickle-cache** them
keyed by `snapshot_id`. Frames are then O(active visits). Only recompute visits for flights a
mitigation touches.

---

## 6. Disruptions (`disruptions.py`)

- `STORM(refc_dbz, close_impacted_sectors)`: threshold the refc grid over the window; the union
  of impacted pixels is the storm footprint (sent to UI as cells/polygon). Optionally mark
  HIGH/LOW sectors whose footprint is heavily impacted as **closed**.
- `SECTOR_CLOSURE(sectors[])`: flag those sectors closed.
- `GROUND_STOP(airport, hold_min, from, to)`: for flights departing `airport` in `[from,to)`,
  shift `t0`/`t1` by `hold_min` (a DELAY applied as part of the disruption, not a mitigation).
- Ship 2-3 **presets** tuned on a real snapshot so the demo cascade is dramatic.

---

## 7. Mitigations (`mitigations.py`)

Each has `evaluate(state, ...)` → `(candidate_state_diff, impact)` **without committing**, and
`apply(state, ...)` which mutates and recomputes only affected flights.
- `REROUTE(flight_id, side, around)`: insert lateral detour waypoints offsetting the route
  around closed/storm cells → new distance → new `t1` (extra_nm, delay_min).
- `DELAY(flight_id, minutes)`: shift `t0/t1`.
- `ALTITUDE(flight_id, new_alt_ft)`: change cruise altitude / band.

`impact = { conflicts_resolved, conflicts_created, delay_min, extra_nm }` computed by diffing
the conflict set before vs. after on the candidate.

---

## 8. Co-pilot (`copilot.py`)

- Model `claude-opus-4-8`, **tool use** loop, max ~6 tool rounds.
- System prompt + static world description = **cached prefix** (`cache_control`); dynamic
  conflict snapshot appended uncached.
- Read tools: `list_conflicts`, `get_sector`, `get_flight`. What-if tools: `evaluate_reroute`,
  `evaluate_delay`, `evaluate_altitude` (call into `mitigations.evaluate`). Final tool:
  `recommend(mitigation, rationale)`.
- **Fallback** (no API key or error): deterministic solver — take the worst conflict, try the
  three what-ifs on its biggest contributing flight, pick the best impact, template a rationale.
  Set `source: "fallback"`.

---

## 9. Non-functional targets

- Frame served < 100 ms from precomputed state. Warm scenario load < 3 s. Suggest < 10 s.
- App must boot and animate **without** an API key (co-pilot degrades to fallback).
- Validate every inbound param (scenario id exists, flight id exists, timestamp in bounds,
  altitude/hold ranges) → 4xx with `{ "error": ... }`. Claude tool args validated before use.
- Structured logs: sim builds, per-frame conflict counts, every co-pilot action.

---

## 10. Local run

`./run.sh` → venv + `pip install -r requirements.txt` + `uvicorn backend.main:app`.
Env: `ANTHROPIC_API_KEY` (optional), `DATA_BUNDLE_DIR`, `CLAUDE_MODEL`, `CAPACITY_SCALE`.

## 11. Definition of done (backend)

- [ ] All §2 endpoints return contract-shaped JSON against a real snapshot.
- [ ] Baseline sim shows ~0 conflicts; a preset disruption produces a visible conflict curve.
- [ ] A mitigation measurably lowers the curve; reset restores baseline.
- [ ] Co-pilot returns a valid mitigation via Claude, and via fallback when the key is absent.
