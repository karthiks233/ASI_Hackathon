# Frontend Design — AI Flight Disruption War Room

> Build target for the **frontend** engineer. You consume the API in §2 of `BACKEND_DESIGN.md`
> (mirrored below in §3). You can build against a **mock** of that contract before the backend
> is ready. See `SYSTEM_DESIGN.md` for the overall vision.

**Stack:** Vanilla HTML/CSS/JS + **Leaflet.js** (CDN). No build step. Served by the backend at
`/` from `frontend/`.
**Owns:** the war-room dashboard — map, timeline scrubber, conflict feed, AI co-pilot panel,
and all interaction/animation.

---

## 1. The experience (what judges see)

A dark "operations center" screen. A US map fills most of it. Flight dots drift along their
routes. The operator picks a scenario; the storm appears; **sectors bloom red** as they tip
over capacity and the conflict feed fills up. The operator hits **"Ask the co-pilot"** — a card
slides in: *"Reroute AAL1234 north around HIGH_142 — clears 3 conflicts, +7 min."* They click
**Apply**, the sector recolors green, the conflict curve drops. Repeat until the room is calm.

It must feel **live, legible, and dramatic**: motion, color, and a falling conflict count.

---

## 2. Layout

```
┌───────────────────────────────────────────────────────────────────────┐
│  HEADER:  ✈ AI FLIGHT DISRUPTION WAR ROOM   [scenario ▼] [disruption ▼]│
│           status chips: ● 2 over-demand  ● 5 in weather  ⏱ +52 min     │
├──────────────────────────────────────────────┬────────────────────────┤
│                                               │  CONFLICT FEED          │
│                                               │  ┌────────────────────┐ │
│                  LEAFLET MAP                   │  │ 🔴 HIGH_142  9/7    │ │
│   • flight dots (color by status)             │  │ 🟠 storm: 5 flights │ │
│   • sector polygons (heat by load ratio)      │  │ ⛔ HIGH_140 closed  │ │
│   • storm overlay (refc cells/polygon)         │  └────────────────────┘ │
│   • click flight/sector → detail popover       │                        │
│                                               │  AI CO-PILOT            │
│                                               │  [ Ask the co-pilot ]   │
│                                               │  ┌─ suggestion card ──┐ │
│                                               │  │ REROUTE AAL1234     │ │
│                                               │  │ +7 min · −3 conf.   │ │
│                                               │  │ "rationale…"        │ │
│                                               │  │ [Apply] [Skip]      │ │
│                                               │  └────────────────────┘ │
├──────────────────────────────────────────────┴────────────────────────┤
│  TIMELINE:  ▶  |————●———▲ peak————————|   22:40Z   [baseline ── current]│
│             little area chart of total conflicts over time              │
└───────────────────────────────────────────────────────────────────────┘
```

Three regions: **map** (center, dominant), **right rail** (conflict feed + AI panel),
**timeline** (bottom, full width).

---

## 3. API the frontend calls (mirror of the contract)

Base `/api`. See `BACKEND_DESIGN.md` §2 for full payloads. Summary of what the UI uses:

| UI action | Call |
|-----------|------|
| On load → populate scenario/disruption dropdowns | `GET /api/scenarios`, `GET /api/health` |
| Choose scenario → load sim | `POST /api/scenario/load` → `{scenario_id, t_start, t_end, step_min}` |
| Draw sectors (once) | `GET /api/scenario/{id}/sectors.geojson` |
| Each animation frame / scrub | `GET /api/scenario/{id}/frame?t=<ISO>` |
| Draw the timeline chart + peak marker | `GET /api/scenario/{id}/timeline` |
| Inject disruption | `POST /api/scenario/{id}/disrupt` → returns timeline |
| Ask co-pilot | `POST /api/scenario/{id}/copilot/suggest` → `{mitigation, world_summary, source}` |
| Apply a fix | `POST /api/scenario/{id}/copilot/apply` → returns timeline |
| Reset | `POST /api/scenario/{id}/reset` → returns timeline |

**Frame is the hot path** — fetch on scrub/play, render flights + sector colors + conflicts +
status chips from one `frame` response. Keep a small client cache of recently fetched frames.

### Mock-first
Ship `frontend/mock.js` that returns canned `frames/timeline/suggest` payloads matching §2 so
the UI is fully buildable before the backend lands. Toggle with `?mock=1`.

---

## 4. Map rendering (Leaflet)

- Base: a dark tile layer (e.g. CartoDB dark_nomatter) or a plain dark background + states
  GeoJSON outline. CONUS view, zoom locked to a sensible range.
- **Sectors:** one GeoJSON layer; on each frame, restyle each polygon by its `ratio`:
  - `ratio < 0.7` → faint blue/green, low opacity
  - `0.7–1.0` → amber
  - `> 1.0` (over-demand) → red, higher opacity, subtle pulse
  - `closed: true` → hatched/dark red outline ⛔
  Only `sectors[]` in the frame are styled "hot"; others reset to faint. Use the band the
  user is viewing (HIGH/LOW toggle) — show one band at a time to avoid overlap.
- **Flights:** circle markers keyed by `id`; **reuse markers across frames** (move, don't
  recreate). Color by `status`: `ok` = cyan, `weather` = orange, `closed` = red. Optional
  small heading triangle. Click → popover with `fn`, alt, origin→dest, current conflicts.
- **Storm overlay:** from the disruption response (refc cells or a polygon) → a translucent
  orange/purple layer; gentle pulse so it reads as "live weather".
- Performance: with up to ~16.7k flights, only render those present in the frame (airborne),
  use `L.canvas()` renderer, and update positions in place. Target a smooth scrub.

---

## 5. Timeline scrubber (bottom)

- A horizontal track spanning `t_start…t_end`. Play/pause; scrubbing sets the current `t` and
  triggers a `frame` fetch (throttled).
- Behind the track, a small **area chart** of `total` conflicts per frame from `/timeline`:
  draw **baseline** (faint) and **current** (bright) so the improvement is visible.
- A **▲ peak marker** at `peak_t`; clicking it jumps the scrubber there ("show me the worst
  moment"). This is the money shot — make it one click.

---

## 6. Conflict feed (right rail, top)

- Render `frame.conflicts[]` as rows, sorted by severity. Icon + `label`.
  - `OVER_DEMAND` 🔴 `HIGH_142 9/7`, `WEATHER` 🟠 `5 flights in storm`, `CLOSED_SECTOR` ⛔.
- Click a row → highlight the sector/flights on the map (pan + flash).
- Header **status chips** summarize `frame.metrics` (over-demand count, weather flights, total
  delay min). These should visibly tick down as fixes are applied.

---

## 7. AI co-pilot panel (right rail, bottom)

- **"Ask the co-pilot"** button → `POST copilot/suggest`. Show a thinking state (animated
  "co-pilot is analyzing the airspace…"). When `source==="fallback"`, badge it "offline solver".
- Render the returned `mitigation` as a **card**:
  - Title: `{action} {flight_number}` with an icon (reroute ↪ / delay ⏱ / altitude ⤴).
  - Impact pills: `−{conflicts_resolved} conflicts`, `+{delay_min} min`, `+{extra_nm} nm`,
    and a warning pill if `conflicts_created > 0`.
  - `rationale` text (the human-readable why).
  - **[Apply]** → `POST copilot/apply`, then refresh timeline + current frame, animate the
    affected flight's path change. **[Skip]** → ask for the next suggestion.
- Keep an **applied-fixes log** beneath the card (running list of what the co-pilot did) so the
  narrative of "the room calming down" is visible.

---

## 8. State & data flow (client)

```
appState = {
  scenarioId, tStart, tEnd, stepMin, band: "HIGH",
  currentT, playing,
  sectorsGeo,            // drawn once
  frameCache: Map<t, frame>,
  timeline,              // baseline + current series, peak
  suggestion,            // current co-pilot card
  appliedLog: []
}
```
- `setT(t)` → fetch/lookup frame → `renderFrame(frame)` (positions, sector styles, conflicts,
  chips). `play()` advances `t` by `stepMin` on an interval (e.g. 1 frame / 400 ms).
- After `disrupt` / `apply` / `reset`: refetch `/timeline`, invalidate `frameCache`, refetch
  the current frame.

---

## 9. Visual language

- Dark ops-center theme; monospace for IDs/metrics, clean sans for prose.
- Status palette: `ok` cyan `#38bdf8`, `weather` orange `#fb923c`, `closed/over-demand` red
  `#ef4444`, resolved/good green `#22c55e`. Storm purple-orange.
- Motion with restraint: flights glide, hot sectors pulse, the conflict number animates when it
  changes. Nothing gratuitous — it should read as a serious console.

---

## 10. File layout

```
frontend/
  index.html     # layout skeleton + Leaflet CDN + entry
  app.js         # state, API client, render loop, interactions
  mock.js        # canned contract responses for offline/parallel dev (?mock=1)
  style.css      # ops-center theme
```

## 11. Definition of done (frontend)

- [ ] Scenario loads; sectors drawn; scrubbing animates flight dots smoothly.
- [ ] Disruption injection makes sectors bloom red + storm overlay appears + conflict feed fills.
- [ ] Timeline shows baseline vs current with a clickable peak marker.
- [ ] "Ask the co-pilot" shows a suggestion card; Apply visibly drops the conflict curve and
      recolors the map; Skip fetches the next.
- [ ] Works against `?mock=1` with no backend, and against the live backend.
