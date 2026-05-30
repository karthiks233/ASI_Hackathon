# Airway Superintelligence — System Design

**Hackathon:** ASI Hackathon 2026  
**Team:** Karthik Raja + teammate  
**Date:** 2026-05-30  
**Living Doc:** [Google Doc](https://docs.google.com/document/d/1MlgbDywEloj09Aqc2POS3nQjshg0tGSJZAd6pdLOtlY/edit?tab=t.0)

---

## 1. Project Overview

**Airway Superintelligence** is an AI-powered flight route optimizer that recommends the safest and most efficient airways between two airports. Given a departure airport, destination, aircraft type, and departure time, the system fetches real-time and forecast weather data (METARs, TAFs, SIGMETs, PIREPs, winds-aloft) and uses Claude to reason over candidate routes — scoring each for turbulence risk, icing, convective activity, headwind penalty, and airspace constraints — then returns an optimized flight plan with a plain-language hazard briefing.

**Target users:** Student pilots, GA (General Aviation) pilots, flight dispatchers, and flight planning tools.

---

## 2. Requirements

### 2.1 Functional Requirements

| ID   | Requirement | Priority |
|------|-------------|----------|
| FR-1 | User inputs departure ICAO, destination ICAO, departure time, cruise altitude, aircraft type | Must Have |
| FR-2 | System fetches live METARs, TAFs, SIGMETs, PIREPs, and winds-aloft along candidate routes | Must Have |
| FR-3 | Claude reasons over weather data and returns an optimized route as ordered waypoints | Must Have |
| FR-4 | Each route is scored: turbulence risk, icing risk, convective risk, wind efficiency | Must Have |
| FR-5 | System returns a plain-language pilot briefing explaining the recommendation | Must Have |
| FR-6 | User can request 1–3 alternative routes if the primary is unacceptable | Should Have |
| FR-7 | Routes are rendered on an interactive map (Leaflet.js) | Should Have |
| FR-8 | System checks active NOTAMs and restricted airspace along the route | Should Have |
| FR-9 | Historical route + weather data is stored for ML training and analytics | Nice to Have |
| FR-10 | Estimated fuel burn delta between route options | Nice to Have |

### 2.2 Non-Functional Requirements

| ID    | Requirement | Target |
|-------|-------------|--------|
| NFR-1 | Latency | Route recommendation returned in < 8 seconds (including weather fetch + Claude call) |
| NFR-2 | Weather data freshness | METARs refreshed every 30 min, SIGMETs every 5 min |
| NFR-3 | Availability | 99% uptime for demo; weather API failures degrade gracefully |
| NFR-4 | Security | API keys in env vars; no auth required for MVP demo |
| NFR-5 | Observability | Structured JSON logs for every route request + Claude call |

### 2.3 Out of Scope (MVP)

- Full ETOPS / oceanic route planning
- ATC clearance simulation
- Paid tier / user accounts
- Mobile app
- FAA / EASA regulatory compliance (advisory use only)

---

## 3. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         Browser / Client                        │
│  Route Form  ──►  Map View (Leaflet)  ──►  Hazard Briefing UI  │
└────────────────────────────┬────────────────────────────────────┘
                             │  REST / JSON
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                    FastAPI Backend (Python)                      │
│                                                                 │
│  POST /optimize  ──►  RouteOptimizer Service                    │
│                              │                                  │
│              ┌───────────────┼────────────────┐                 │
│              ▼               ▼                ▼                 │
│      WeatherFetcher    RouteGenerator    AirspaceChecker        │
│      (async calls)    (waypoint graph)  (NOTAM + TFR)          │
│              │               │                                  │
│              └───────────────┘                                  │
│                              │  Assembled context               │
│                              ▼                                  │
│                    Claude API (tool use)                        │
│                    claude-sonnet-4-6                            │
│                              │  Structured route + briefing     │
│                              ▼                                  │
│                      Response Builder                           │
└──────────────────────────────┬──────────────────────────────────┘
                               │
            ┌──────────────────┼──────────────────┐
            ▼                  ▼                  ▼
   PostgreSQL DB       Aviation Weather      FAA NOTAM API
   (route history)     API (NOAA/AWC)        (or alternatives)
```

**Architecture style:** Monolith (single FastAPI app) — keeps the demo deployable in one command.

---

## 4. Tech Stack

| Layer | Technology | Reason |
|-------|-----------|--------|
| Frontend | Vanilla HTML + Leaflet.js (or minimal React) | Fast to ship; map rendering built-in |
| Backend | Python 3.12 + FastAPI | Async-friendly, great for concurrent weather fetches |
| AI | Claude `claude-sonnet-4-6` via Anthropic SDK | Best reasoning over semi-structured weather data |
| Database | PostgreSQL + SQLAlchemy (async) | Store route requests + results for demo replay |
| Weather APIs | NOAA Aviation Weather Center (aviationweather.gov) | Free, official, real-time METARs/TAFs/SIGMETs/PIREPs |
| Airspace | FAA DroneZone / AIP API or static GeoJSON | NOTAM and restricted airspace lookup |
| HTTP Client | `httpx` (async) | Parallel weather fetches |
| Config | `pydantic-settings` + `.env` | Clean env var management |
| Deployment | Docker Compose (demo) | One command startup |

---

## 5. Data Model

### Entity: `RouteRequest`
```
id              UUID        PK
departure_icao  VARCHAR(4)
destination_icao VARCHAR(4)
departure_time  TIMESTAMP
cruise_altitude_ft INT
aircraft_type   VARCHAR(10)  -- e.g. "C172", "B738"
created_at      TIMESTAMP
```

### Entity: `RouteResult`
```
id              UUID        PK
request_id      UUID        FK → RouteRequest
waypoints       JSONB       -- [{ icao, lat, lon, alt_ft }]
risk_scores     JSONB       -- { turbulence: 0-10, icing: 0-10, convective: 0-10, wind_penalty: 0-10 }
overall_score   FLOAT
briefing_text   TEXT        -- Claude's plain-language explanation
raw_weather     JSONB       -- snapshot of weather data used
claude_tokens   INT
created_at      TIMESTAMP
```

### Entity: `WeatherSnapshot`
```
id              UUID        PK
station_icao    VARCHAR(4)
type            VARCHAR(10)  -- METAR, TAF, SIGMET, PIREP
raw_text        TEXT
parsed_json     JSONB
fetched_at      TIMESTAMP
```

**Relationships:**
- RouteRequest has one RouteResult
- RouteResult references many WeatherSnapshots

---

## 6. API Design

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| POST | `/api/optimize` | Submit a route optimization request | No |
| GET | `/api/optimize/{id}` | Fetch a previous result by ID | No |
| GET | `/api/weather/metar/{icao}` | Live METAR for a single station | No |
| GET | `/api/weather/sigmet` | Active SIGMETs in a bounding box | No |
| GET | `/api/health` | Health check | No |

### POST `/api/optimize` — Request Body
```json
{
  "departure_icao": "KBOS",
  "destination_icao": "KLAX",
  "departure_time": "2026-05-30T18:00:00Z",
  "cruise_altitude_ft": 35000,
  "aircraft_type": "B738"
}
```

### POST `/api/optimize` — Response
```json
{
  "request_id": "uuid",
  "recommended_route": {
    "waypoints": [
      { "id": "KBOS", "lat": 42.36, "lon": -71.01, "type": "airport" },
      { "id": "ALB",  "lat": 42.74, "lon": -73.80, "type": "vor" },
      { "id": "KLAX", "lat": 33.94, "lon": -118.40, "type": "airport" }
    ],
    "risk_scores": {
      "turbulence": 3,
      "icing": 1,
      "convective": 0,
      "wind_penalty": 4
    },
    "overall_score": 2.8,
    "estimated_flight_time_min": 318
  },
  "briefing": "The recommended route via ALB and J80 avoids a SIGMET for moderate turbulence over the Rockies. Expect light icing between FL240-FL280 near DEN; recommend FL350. Tailwind component of ~40kts over the Plains improves block time by approximately 22 minutes versus direct routing.",
  "alternatives": []
}
```

---

## 7. Key Flows

### Flow 1: Route Optimization Request

1. User fills in the form (departure, destination, time, altitude, aircraft) and hits "Optimize"
2. Frontend POSTs to `/api/optimize`
3. FastAPI spins up `RouteOptimizer`:
   - `RouteGenerator` computes 2–3 candidate route corridors (direct + northern/southern deviations)
   - `WeatherFetcher` fires parallel `httpx` requests to AWC for METARs, TAFs, SIGMETs, PIREPs, winds-aloft along each corridor
   - `AirspaceChecker` queries active NOTAMs and TFRs
4. All data is assembled into a structured context payload
5. Claude is called with tool use — it can call `get_weather_at_waypoint`, `score_turbulence`, `check_sigmet_overlap` tools if it needs more detail
6. Claude returns a structured JSON route recommendation + plain-language briefing
7. Result is saved to PostgreSQL and returned to the client
8. Frontend renders waypoints on the Leaflet map, overlays SIGMETs, and displays the briefing

### Flow 2: Alternative Route Request

1. User clicks "Show alternatives" on the result
2. Frontend re-POSTs with `{"request_id": "...", "exclude_route": [...primary waypoints...]}`
3. System reruns Claude with an instruction to avoid the primary route corridor
4. Up to 2 alternatives are returned and rendered as secondary polylines on the map

---

## 8. AI / Agent Design

**Model:** `claude-sonnet-4-6`  
**Prompting strategy:** System prompt establishes Claude as an expert aviation weather analyst + dispatcher. User turn provides structured weather context. Claude uses tool calls to fetch additional detail, then produces a JSON-structured route recommendation.

### System Prompt (skeleton)
```
You are an expert aviation weather analyst and flight dispatcher with 20+ years of experience.
Your job is to analyze weather data along candidate flight routes and recommend the safest,
most efficient airway. Always prioritize safety over efficiency.
Output a structured JSON route recommendation followed by a plain-language pilot briefing.
```

### Tools Claude Can Call

| Tool | Description |
|------|-------------|
| `get_metar(icao)` | Fetch live METAR for a station |
| `get_sigmet_in_area(lat, lon, radius_nm)` | Get active SIGMETs near a point |
| `get_winds_aloft(lat, lon, altitude_ft)` | Winds and temp at altitude |
| `get_pireps_near(lat, lon, radius_nm)` | Recent pilot reports near a point |
| `score_route_segment(waypoint_a, waypoint_b, altitude_ft)` | Aggregate hazard score for a segment |

### Prompt caching strategy
- System prompt is cached (it's large and static per session)
- Weather context is passed per-request (dynamic, not cached)
- Estimated cache hit rate: ~60% on system prompt across a demo session

### Context window considerations
- Weather data for a transcontinental route: ~4,000–8,000 tokens
- Full context including system prompt: < 20,000 tokens — well within limits
- If weather data exceeds 15k tokens, truncate PIREPs oldest-first

---

## 9. Infrastructure & Deployment

**Environments:** `local` | `demo` (single server)  
**CI/CD:** GitHub Actions — lint + test on PR  
**Hosting:** Fly.io or Railway (free tier for demo)

### Docker Compose (local)
```yaml
services:
  api:    # FastAPI app
  db:     # PostgreSQL 16
```

### Environment Variables
| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Claude API access |
| `DATABASE_URL` | PostgreSQL connection string |
| `AWC_API_KEY` | Aviation Weather Center (if required) |
| `NOTAM_API_KEY` | FAA NOTAM API key |
| `LOG_LEVEL` | `INFO` / `DEBUG` |

---

## 10. Security Considerations

- [ ] All secrets in `.env`, never committed — `.env` in `.gitignore`
- [ ] Input validation: ICAO codes validated against a known airport list
- [ ] Claude responses validated against a Pydantic schema before use
- [ ] Rate limit `/api/optimize` to 10 req/min per IP (to control Claude costs during demo)
- [ ] No PII collected — no user accounts in MVP
- [ ] CORS locked to demo frontend origin in production

---

## 11. Open Questions

| # | Question | Owner | Status |
|---|----------|-------|--------|
| 1 | Which waypoint graph to use for candidate routes? (FAA NAVAID database vs. OpenAIP) | Karthik | Open |
| 2 | Does AWC free tier support the request volume we need during the demo? | teammate | Open |
| 3 | Do we want a frontend framework (React) or keep it vanilla JS + Leaflet? | Both | Open |
| 4 | Include oceanic waypoints (NAT tracks) for transatlantic demo routes? | Both | Open |
| 5 | How do we handle airspace that requires ATC clearance (Class B/C)? Advisory disclaimer? | Karthik | Open |

---

## 12. Milestones

| Milestone | Description | Target |
|-----------|-------------|--------|
| M1 | FastAPI skeleton + PostgreSQL schema + `/health` endpoint | Day 1 AM |
| M2 | Weather fetcher working (METAR + SIGMET + winds-aloft) | Day 1 PM |
| M3 | Claude integration — basic route scoring + briefing | Day 1 PM |
| M4 | Frontend map (Leaflet) rendering waypoints + SIGMETs | Day 2 AM |
| M5 | End-to-end demo: BOS→LAX optimized route on the map | Day 2 PM |
| M6 | Polish: alternative routes, risk score UI, briefing panel | Day 2 PM |

---

## 13. File Structure (Planned)

```
ASI_Hackathon/
├── backend/
│   ├── main.py                  # FastAPI app entrypoint
│   ├── routers/
│   │   ├── optimize.py          # POST /api/optimize
│   │   └── weather.py           # GET /api/weather/*
│   ├── services/
│   │   ├── route_optimizer.py   # Orchestrates the full flow
│   │   ├── weather_fetcher.py   # Async AWC calls
│   │   ├── route_generator.py   # Candidate waypoint corridors
│   │   ├── airspace_checker.py  # NOTAM / TFR lookup
│   │   └── claude_client.py     # Anthropic SDK wrapper + tools
│   ├── models/
│   │   ├── db.py                # SQLAlchemy models
│   │   └── schemas.py           # Pydantic request/response schemas
│   └── config.py                # pydantic-settings env config
├── frontend/
│   ├── index.html
│   ├── map.js                   # Leaflet map + route rendering
│   └── style.css
├── docker-compose.yml
├── .env.example
├── requirements.txt
└── SYSTEM_DESIGN.md
```
