# AI Flight Disruption War Room — System Design

**Hackathon:** ASI Hackathon 2026
**Date:** 2026-05-30
**Living Doc:** [Google Doc](https://docs.google.com/document/d/1MlgbDywEloj09Aqc2POS3nQjshg0tGSJZAd6pdLOtlY/edit?tab=t.0)

---

## 1. Project Overview

**AI Flight Disruption War Room** is a real-time decision-support dashboard for air-traffic  
flow management. A simulated disruption — a storm, a closed sector, or an airport ground  
stop — hits the US National Airspace, and the operator watches **live as flights cascade into
conflict**: sectors tip over their capacity, aircraft penetrate severe weather, traffic backs
up. An **AI co-pilot** (Claude) then reasons over the live conflict picture and proposes fixes
**one at a time** — reroute this flight, hold that departure, climb this one above the echo
tops — each with a plain-language justification and a measured before/after impact. The
operator applies a fix, the simulation recomputes, and the room calms down.

It is built entirely on the provided `hackathon_data_bundle`: real US flight routes
(16,687 flights with full waypoint paths), 712 synthetic ATC sectors with capacities, and
time-stepped HRRR weather forecasts (composite reflectivity + echo tops).

**Why it scores on every criterion**


| Criterion             | How the War Room delivers                                                                                                                                                              |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Technical merit       | Time-stepped trajectory simulation, point-in-polygon sector occupancy via spatial index, capacity/weather/closure conflict detection, constraint-aware rerouting, Claude tool-use loop |
| Problem understanding | Models the *cascading* nature of ATC disruptions — one closed sector pushes demand into neighbors, which tip over in turn                                                              |
| Communication         | Visceral, watchable demo: judges see red sectors bloom and the AI cool them down live on a map                                                                                         |
| Creativity            | The "war room + AI co-pilot suggesting fixes one by one" framing is memorable and dramatic                                                                                             |


**Team:** 

---

## 2. Requirements

### 2.1 Functional Requirements


| ID    | Requirement                                                                                              | Priority     |
| ----- | -------------------------------------------------------------------------------------------------------- | ------------ |
| FR-1  | Load a flight-route snapshot, the sector geometry, and the matching weather forecast strips              | Must Have    |
| FR-2  | Simulate every flight's position over a time window (constant cruise speed/altitude model)               | Must Have    |
| FR-3  | Assign each flight to its sector (correct altitude band) at each timestep via spatial index              | Must Have    |
| FR-4  | Detect conflicts: sector **over-demand**, **weather penetration**, **closed-sector** intrusion           | Must Have    |
| FR-5  | Inject a disruption scenario: **storm** (from weather data), **sector closure**, **airport ground stop** | Must Have    |
| FR-6  | Animate the cascade on a map: flight dots, sector load heatmap, storm overlay, timeline scrubber         | Must Have    |
| FR-7  | AI co-pilot (Claude, tool use) inspects the live conflict set and proposes mitigations one at a time     | Must Have    |
| FR-8  | Each mitigation has a before/after impact (conflicts resolved, delay minutes, extra distance)            | Must Have    |
| FR-9  | Operator applies/rejects a suggestion; simulation recomputes only the affected flights                   | Must Have    |
|       |                                                                                                          |              |
| FR-11 | Reset / replay the scenario to compare "do nothing" vs "AI-managed" outcomes                             | Should Have  |
| FR-12 | Multiple prebuilt scenarios selectable from the bundle's 11 snapshots                                    | Nice to Have |


### 2.2 Non-Functional Requirements


| ID    | Requirement           | Target                                                                                    |
| ----- | --------------------- | ----------------------------------------------------------------------------------------- |
| NFR-1 | Scrub/animate latency | Timestep frame served from precomputed state in < 100 ms                                  |
| NFR-2 | Scenario load         | First load builds + caches the sim; subsequent loads from cache in < 3 s                  |
| NFR-3 | Co-pilot turnaround   | A mitigation suggestion returned in < 10 s (Claude call + tool round-trips)               |
| NFR-4 | Resilience            | Missing `ANTHROPIC_API_KEY` degrades co-pilot to the deterministic solver, app still runs |
| NFR-5 | Scale                 | Handle the full ~16.7k-flight snapshot without pre-filtering                              |
| NFR-6 | Observability         | Structured logs for sim builds, conflict counts per frame, and every co-pilot action      |


### 2.3 Out of Scope (MVP)

- Climb/descent and en-route speed changes (constant cruise model per the data spec)
- Real-time live ADS-B ingestion (we replay the provided snapshots)
- Multi-user / authentication / persistence beyond an in-process scenario cache
- Provably optimal flow program (we do greedy, AI-guided, locally-valid mitigations)

---

## 3. Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│                          Browser — War Room UI                         │
│  Leaflet map        Timeline scrubber       Conflict feed     AI panel │
│  • flight dots      • play / scrub          • over-demand     • Claude │
│  • sector heatmap   • peak-conflict marker  • wx penetration    fixes  │
│  • storm overlay                            • closed sector   • apply  │
└───────────────┬──────────────────────────────────────────┬────────────┘
                │  GET frame state / POST disruption        │  POST copilot/suggest
                ▼                                           ▼
┌──────────────────────────────────────────────────────────────────────┐
│                        FastAPI backend (Python)                        │
│                                                                        │
│   /scenarios          DataLoader        ── routes.json (gz)            │
│   /scenario/load   ──► • flights        ── sectors.geojson            │
│   /frame              • sectors (STRtree) ── wx refc/retop .npz        │
│   /disrupt         ──► SimulationEngine                                │
│   /copilot/suggest    • trajectory interpolation (numpy)              │
│   /copilot/apply      • per-flight sector timelines                   │
│   /reset              • conflict detection (over-demand/wx/closure)   │
│                              │                                         │
│                       DisruptionEngine  (storm / closure / ground stop)│
│                              │                                         │
│                       Mitigation tools  (reroute / delay / altitude)   │
│                              │                                         │
│                       Copilot (Claude tool-use)  claude-opus-4-8       │
└──────────────────────────────────────────────────────────────────────┘
```

**Architecture style:** Single FastAPI monolith serving a static SPA. All simulation state
lives in memory keyed by `scenario_id`; the heavy sector-timeline build is cached to disk
(pickle) per snapshot so only the first run pays the cost.

---

## 4. Tech Stack


| Layer     | Technology                                                                                         | Reason                                                                                                             |
| --------- | -------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| Frontend  | Vanilla HTML/CSS/JS + **Leaflet.js**                                                               | No build step, instant load, map + overlays built-in — ideal for a live demo                                       |
| Backend   | Python 3.13 + **FastAPI** + Uvicorn                                                                | Async API, trivial static hosting, ecosystem for geo/data                                                          |
| Geo / sim | **NumPy** (vectorized trajectory math) + **Shapely 2** (`STRtree` spatial index, point-in-polygon) | Fast occupancy computation over 712 sectors × 16.7k flights                                                        |
| Weather   | **NumPy** `.npz` grids (HRRR refc/retop)                                                           | Provided format; equirectangular grid → O(1) pixel lookup                                                          |
| AI        | **Claude `claude-opus-4-8`** via Anthropic SDK, **tool use**                                       | Reasons over the conflict set and drives deterministic mitigation tools; prompt caching on the system/world prompt |
| Config    | `python-dotenv` + env vars                                                                         | `ANTHROPIC_API_KEY`, data bundle path                                                                              |
| Cache     | Local pickle of per-snapshot sector timelines                                                      | Sub-3s warm scenario loads                                                                                         |


---

## 5. Data Model

The bundle is the source of truth; these are the **in-memory** structures the engine builds.

### Loaded inputs (from the bundle)

```
Flight                         # from routes.json
  flight_id      str           # f"{flight_number}|{take_off_time}|{origin}"  (unique)
  flight_number  str
  origin / dest  ICAO
  take_off_time  datetime (UTC)
  landing_time   datetime (UTC)
  cruise_alt_ft  int
  cruise_speed_kt int
  band           "HIGH"|"LOW"  # HIGH iff cruise_alt_ft >= 35000
  lats, lons     float[]       # planned waypoints, origin → dest
  is_airborne    bool

Sector                         # from sectors.geojson
  name           str           # HIGH_NNN / LOW_NNN
  band           "HIGH"|"LOW"
  alt_from/to_ft int
  capacity       int
  geom           shapely Polygon

WxFrame                        # from wx/refc & wx/retop .npz
  valid_from/to  datetime
  refc           float[256,358]  # dBZ
  retop          float[256,358]  # ft
```

### Derived / runtime structures

```
Trajectory (per flight, precomputed)
  cum_dist_nm    float[]        # cumulative great-circle distance along waypoints
  total_nm       float
  t0, t1         datetime       # take_off, landing (mutable: ground hold shifts both)
  -> position(t) = interpolate lat/lon by distance fraction

SectorVisit (per flight, precomputed timeline)
  flight_id, sector_name, enter_t, exit_t

FrameState (per timestep t, served to UI)
  t
  flights[]      {flight_id, lat, lon, band, status}      # status: ok|weather|closed
  sector_load[]  {sector_name, count, capacity, ratio}    # ratio>1 => over-demand
  conflicts[]    Conflict
  metrics        {over_demand_sectors, weather_flights, total_delay_min, ...}

Conflict
  kind           "OVER_DEMAND"|"WEATHER"|"CLOSED_SECTOR"
  severity       float
  sector_name?   str
  flight_ids[]   str
  t_window       [start, end]

Disruption
  kind           "STORM"|"SECTOR_CLOSURE"|"GROUND_STOP"
  params         {...}          # e.g. closed sector names, airport, refc threshold, hold window

Mitigation (proposed or applied)
  action         "REROUTE"|"DELAY"|"ALTITUDE"
  flight_id      str
  params         {...}
  impact         {conflicts_resolved, conflicts_created, delay_min, extra_nm}
  rationale      str            # Claude's explanation
```

**Relationships:** a Scenario = one snapshot + one Disruption + an ordered list of applied
Mitigations. Re-running a Mitigation only recomputes the trajectories/visits of the flights it
touches, then re-aggregates frame state.

---

## 6. API Design


| Method | Path                                 | Description                                                                    |
| ------ | ------------------------------------ | ------------------------------------------------------------------------------ |
| GET    | `/api/scenarios`                     | List available snapshots + prebuilt disruption presets                         |
| POST   | `/api/scenario/load`                 | Build (or load from cache) the sim for a snapshot → `scenario_id`, time bounds |
| GET    | `/api/scenario/{id}/frame?t=...`     | Frame state at time `t` (flights, sector load, conflicts, metrics)             |
| GET    | `/api/scenario/{id}/timeline`        | Per-timestep conflict counts (for the scrubber + peak marker)                  |
| POST   | `/api/scenario/{id}/disrupt`         | Inject a disruption; recompute; returns new timeline summary                   |
| POST   | `/api/scenario/{id}/copilot/suggest` | Ask Claude for the next single mitigation given current conflicts              |
| POST   | `/api/scenario/{id}/copilot/apply`   | Apply a mitigation (reroute/delay/altitude); recompute affected flights        |
| POST   | `/api/scenario/{id}/reset`           | Drop applied mitigations / disruption; back to baseline                        |
| GET    | `/api/scenario/{id}/sectors.geojson` | Sector polygons (for the map, sent once)                                       |
| GET    | `/api/health`                        | Health + whether the Claude key is configured                                  |


### POST `/api/scenario/{id}/copilot/suggest` — Response (shape)

```json
{
  "mitigation": {
    "action": "REROUTE",
    "flight_id": "AAL1234|2025-05-29T21:10:00+00:00|KORD",
    "params": { "around": ["HIGH_142", "HIGH_143"], "detour_side": "north" },
    "impact": { "conflicts_resolved": 3, "conflicts_created": 0, "delay_min": 7, "extra_nm": 48 },
    "rationale": "AAL1234 is the largest single contributor to the HIGH_142 over-demand at 22:40Z. A northern detour around the closed cells clears it with only ~7 min added and creates no new hotspots."
  },
  "world_summary": "2 sectors over capacity, peak 22:40Z; 5 flights penetrating the storm core."
}
```

---

## 7. Key Flows

### Flow 1: Watch the cascade

1. Operator picks a snapshot + disruption preset (e.g. *"Convective line over the Midwest, close HIGH_140–145"*).
2. Backend builds trajectories + per-flight sector timelines (cached), aggregates a `FrameState` per timestep.
3. UI loads sector polygons once, then scrubs/plays the timeline: flight dots move, sectors fill toward red, the storm overlay pulses.
4. Disruption is injected → the timeline's conflict curve jumps; the **peak-conflict marker** shows the worst minute. The operator scrubs to it.

### Flow 2: AI co-pilot resolves it, one fix at a time

1. Operator clicks **"Ask the co-pilot."**
2. Backend builds a compact world summary (top conflicts, the worst sectors and their biggest contributing flights) and calls **Claude with tool use**.
3. Claude calls read tools (`list_conflicts`, `get_sector`, `get_flight`) then a what-if tool (`evaluate_reroute` / `evaluate_delay` / `evaluate_altitude`) to test a fix and see its measured impact **before** recommending it.
4. Claude returns **one** mitigation with a rationale + impact numbers. UI shows it as a card with **Apply / Skip**.
5. Operator clicks **Apply** → backend recomputes the affected flights, re-aggregates, the conflict curve drops, the sector recolors green. Loop back to step 1 for the next fix until the room is clear.

### Flow 3: Compare outcomes

1. **Reset** restores baseline-with-disruption.
2. UI overlays the "do nothing" conflict curve vs. the "AI-managed" curve so judges see the delta in resolved conflicts and total added delay.

---

## 8. AI / Agent Design

**Model:** `claude-opus-4-8` (most capable for multi-constraint reasoning).
**Pattern:** a bounded **tool-use loop** — Claude is the strategist, the backend owns ground
truth. Claude never edits flights directly; it *evaluates* candidate fixes through tools and
then recommends the single best one. This keeps every number on screen real and verifiable.

### System prompt (skeleton)

```
You are the AI co-pilot in an air-traffic flow-management war room. A disruption has put
sectors over capacity and pushed flights into weather. Your job: propose the SINGLE most
valuable next mitigation to relieve the worst conflict, with the least delay and distance
added, creating no new conflicts. Prefer rerouting/altitude for weather; ground holds for
demand. Always evaluate a candidate with the what-if tools before recommending it. Return one
mitigation with a crisp operational rationale.
```

### Tools Claude can call


| Tool                                        | Kind    | Description                                                     |
| ------------------------------------------- | ------- | --------------------------------------------------------------- |
| `list_conflicts(kind?, top_n?)`             | read    | Current conflicts ranked by severity                            |
| `get_sector(name)`                          | read    | Capacity, current load, biggest contributing flights, neighbors |
| `get_flight(flight_id)`                     | read    | Route, altitude, timing, which conflicts it's in                |
| `evaluate_reroute(flight_id, side, around)` | what-if | Simulated impact of a detour (no commit)                        |
| `evaluate_delay(flight_id, minutes)`        | what-if | Simulated impact of a ground hold (no commit)                   |
| `evaluate_altitude(flight_id, new_alt_ft)`  | what-if | Simulated impact of an altitude change (no commit)              |
| `recommend(mitigation, rationale)`          | final   | Emit the chosen mitigation back to the UI                       |


### Prompt caching

- The system prompt + static world description (sector layout summary, rules) are sent as a
cached prefix (`cache_control`), so repeated "suggest" calls within a session hit the cache.
- The dynamic conflict snapshot is appended uncached per call.
- Expected: large cache-read fraction across a demo session of many suggestions.

### Context-window notes

- The world summary is deliberately compact (top-N conflicts + worst sectors), a few thousand
tokens — full sector/flight detail is fetched on demand via tools, not dumped into context.

### Degraded mode

- If `ANTHROPIC_API_KEY` is absent, `/copilot/suggest` falls back to the **deterministic
solver** (pick worst conflict → best-scoring what-if among reroute/delay/altitude) and a
templated rationale, so the demo never hard-fails.

---

## 9. Simulation Engine (technical core)

**Trajectory model** (per the data spec — constant cruise, no climb/descent):

- Great-circle distance between consecutive waypoints → `cum_dist_nm`, `total_nm`.
- At time `t`, fraction `f = (t - t0) / (t1 - t0)`; find the segment whose cumulative distance
brackets `f * total_nm`, linearly interpolate lat/lon within it. Outside `[t0, t1]` the
flight is on the ground / landed and excluded.

**Sector occupancy** (precomputed once per flight, the key optimization):

- Build a Shapely `STRtree` over each band's sector polygons.
- Sample each flight's route at a fixed time step (e.g. 2 min), map each sample to its sector
via the tree, and collapse consecutive identical sectors into `SectorVisit(enter_t, exit_t)`.
- Sector occupancy at time `t` = count of visits whose `[enter_t, exit_t]` contains `t`.
Aggregating all visits into per-sector interval lists makes any frame O(visits).

**Conflict detection** per frame:

- `OVER_DEMAND`: `count > capacity` for a sector; severity = `count / capacity`.
- `WEATHER`: flight position pixel has `refc >= 40 dBZ` **and** `retop >= cruise_alt_ft`
(per the weather spec) in the forecast valid at `t`.
- `CLOSED_SECTOR`: flight is inside a sector marked closed by the disruption.

**Mitigations** (what-if = recompute one flight, diff the conflict set):

- `REROUTE`: insert detour waypoints offsetting the route laterally (N/S/E/W) around the
closed/storm cells; recompute distance → new timing (extra_nm, delay_min).
- `DELAY`: shift `t0`/`t1` by the hold; the flight enters busy sectors later.
- `ALTITUDE`: change `cruise_alt_ft` (and possibly band) to clear echo tops / move to a
less-loaded band.

**Disruptions**:

- `STORM`: threshold the refc grid (`>= 40 dBZ`) over the window → impacted region; flights at
or below local echo tops crossing it conflict.
- `SECTOR_CLOSURE`: mark a set of sectors closed (often derived from the storm footprint).
- `GROUND_STOP`: hold departures from an airport for a window.

---

## 10. Infrastructure & Deployment

**Environments:** `local` (demo).
**Run:** `./run.sh` → creates venv, installs `requirements.txt`, launches Uvicorn serving the
API + the static `frontend/`. Open `http://localhost:8000`.

### Environment variables


| Variable            | Purpose                                                            |
| ------------------- | ------------------------------------------------------------------ |
| `ANTHROPIC_API_KEY` | Claude co-pilot (optional — absent ⇒ deterministic fallback)       |
| `DATA_BUNDLE_DIR`   | Path to `hackathon_data_bundle` (default: the Desktop bundle path) |
| `CLAUDE_MODEL`      | Override model id (default `claude-opus-4-8`)                      |
| `LOG_LEVEL`         | `INFO` / `DEBUG`                                                   |


---

## 11. Security Considerations

- `ANTHROPIC_API_KEY` only from env / `.env`; `.env` git-ignored, never committed.
- Claude tool calls are validated against Pydantic schemas; unknown flight ids / out-of-range params rejected before touching the sim.
- Claude can only call whitelisted, side-effect-free *evaluate* tools; the only state change is an explicit operator **Apply**.
- Input validation on all API params (scenario id, timestamp bounds, flight id existence).
- CORS limited to the local demo origin.
- No PII — flight numbers and public route geometry only.

---

## 12. Open Questions


| #   | Question                                                                                                                                           | Status |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| 1   | Capacities are uniform-ish (min 20); do we scale them down so over-demand is visible at this traffic density, or pick the busiest snapshot/region? | Open   |
| 2   | Reroute waypoint synthesis: simple lateral offset vs. routing around the actual closed-sector polygons?                                            | Open   |
| 3   | How many forecast strips to preload per scenario (memory vs. fidelity)?                                                                            | Open   |
| 4   | Animate all ~16.7k dots or only airborne/en-route within the window for frame-rate?                                                                | Open   |


---

## 13. Milestones

Each milestone ends with **both engineers pushing to `main`** so the other can `git pull` and immediately run `./run.sh` to see the working state. Commit convention: `M<N>: <short description>`.

---

### M1 — "Hello Map" · Day 1 AM

**App shows:** Dark war-room layout renders in the browser. Sector polygons are drawn on the Leaflet map (styled faint blue). The scenario dropdown is populated. The health chip in the header says the backend is up.


| Engineer     | Delivers                                                                                                                                                  |
| ------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Backend**  | `GET /health`, `GET /scenarios`, `POST /scenario/load`, `GET /sectors.geojson` · Data loader (routes + sectors) · STRtree built · Pickle cache scaffolded |
| **Frontend** | War-room layout skeleton (`index.html` + `style.css`) · Leaflet map · Sector polygon layer · Scenario/disruption dropdowns populated from `/scenarios`    |


**Git:** each pushes to `main`; other pulls and opens `http://localhost:8000`.

---

### M2 — "Flights Moving" · Day 1 AM → PM

**App shows:** ~800 airborne flight dots drift along their routes as the scrubber plays. Each dot is cyan (ok). The timeline area chart draws total conflict counts (all zeros at baseline). Play/pause works.


| Engineer     | Delivers                                                                                                                                              |
| ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Backend**  | Trajectory engine (great-circle interpolation) · `GET /frame` · `GET /timeline` · Conflict detection (over-demand / weather / closed — 0 at baseline) |
| **Frontend** | Flight dot layer (canvas renderer, reuse markers) · Timeline scrubber + play/pause · Frame-fetch loop (throttled) · Small area chart from `/timeline` |


**Git:** push; pull; confirm dots are moving.

---

### M3 — "The Cascade" · Day 1 PM

**App shows:** Inject *"Convective line over the Midwest"* → sectors bloom red on the map → storm overlay appears → conflict feed fills with 🔴/🟠/⛔ rows → timeline curve jumps → peak marker ▲ appears. Click the peak marker → scrubber jumps to the worst minute.


| Engineer     | Delivers                                                                                                                                                                                                           |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Backend**  | Disruption engine (STORM, SECTOR_CLOSURE, GROUND_STOP) + 2–3 presets · `POST /disrupt` returns refreshed timeline · Frame state includes conflict status per flight and per sector                                 |
| **Frontend** | Sector heatmap (ratio → color) · Storm overlay (translucent polygon) · Conflict feed (severity-sorted rows, click-to-highlight) · Status chips (over-demand count, weather flights) · Peak marker + click-to-scrub |


**Git:** push; pull; run the preset disruption and watch the cascade.

---

### M4 — "AI Co-pilot Live" · Day 2 AM

**App shows:** Click **"Ask the co-pilot"** → "analysing airspace…" spinner → suggestion card slides in (*"REROUTE AAL1234 north around HIGH_142 — clears 3 conflicts, +7 min"*). Click **Apply** → affected flight path shifts on map → sector recolors green → conflict curve drops → applied-fixes log updates.


| Engineer     | Delivers                                                                                                                                                                                                           |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Backend**  | Claude tool-use loop (list_conflicts, get_sector, get_flight, evaluate_*, recommend) · `POST /copilot/suggest` · `POST /copilot/apply` (recomputes affected flights only) · Deterministic fallback when no API key |
| **Frontend** | AI panel · Thinking state animation · Suggestion card (action icon, impact pills, rationale) · Apply / Skip buttons · Applied-fixes log                                                                            |


**Git:** push; pull; run a full suggest → apply cycle; confirm conflict count drops.

---

### M5 — "War Room Complete" · Day 2 PM

**App shows:** Reset button restores baseline-with-disruption. Timeline overlays **baseline** (faint) vs **AI-managed** (bright) curves so the delta is visible. Multiple prebuilt scenarios selectable. Works end-to-end without a Claude key (fallback solver + "offline solver" badge).


| Engineer     | Delivers                                                                                                                                                                              |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Backend**  | `POST /reset` (keep/drop disruption) · Baseline timeline frozen at load · Scenario presets tuned for a dramatic cascade · Full API contract verified                                  |
| **Frontend** | Dual timeline overlay (baseline vs current) · Reset flow · Scenario preset selector · `?mock=1` mock mode passes all paths · Visual polish (ops-center dark theme, smooth animations) |


**Git:** push; pull; run the full demo flow from scratch; record a screen capture for judges.

---

### M6 — "Demo Polish" · Day 2 PM (stretch)

**App shows:** Perfectly smooth demo: sector pulse animation, conflict number ticks down with each fix, clean typography, no flicker, fast warm-load (< 3 s), co-pilot suggestion in < 10 s.

- Bug fixes and frame-rate tuning (canvas renderer, marker reuse)
- Capacity scaling if over-demand is not dramatic enough (`CAPACITY_SCALE` env var)
- README with one-command launch instructions

---

## 14. File Structure (planned)

```
ASI_Hackathon/
├── backend/
│   ├── main.py            # FastAPI app + static serving
│   ├── config.py          # env / paths / model id
│   ├── geo.py             # haversine, great-circle interpolation, grid pixel lookup
│   ├── data_loader.py     # routes.json, sectors.geojson, wx .npz  (+ pickle cache)
│   ├── simulation.py      # Trajectory, SectorVisit, FrameState, conflict detection
│   ├── disruptions.py     # storm / sector closure / ground stop
│   ├── mitigations.py     # reroute / delay / altitude what-if + apply
│   ├── copilot.py         # Claude tool-use loop + deterministic fallback
│   └── schemas.py         # Pydantic request/response models
├── frontend/
│   ├── index.html         # war-room layout
│   ├── app.js             # Leaflet map, scrubber, conflict feed, AI panel
│   └── style.css
├── .env.example
├── requirements.txt
├── run.sh
└── SYSTEM_DESIGN.md
```

