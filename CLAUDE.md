# ASI Hackathon — System Design

**Living Document:**  - 
Backend - BACKEND_DESIGN.md
Frontend  - FRONTEND_DESIGN.md
System Design - SYSTEM_DESIGN.md
---

## 1. Project Overview

> _What is this project? What problem does it solve? One paragraph._

**Project Name:**  ASI
**Team:**   Dudley
**Goal:** Win the Air Space Intelligence Hackathon

---

## 2. Requirements

### 2.1 Functional Requirements (FR)

| ID   | Requirement | Priority |
|------|-------------|----------|
| FR-1 | <!-- e.g. User can sign up and log in --> | Must Have |
| FR-2 | <!-- --> | Must Have |
| FR-3 | <!-- --> | Should Have |
| FR-4 | <!-- --> | Nice to Have |

### 2.2 Non-Functional Requirements (NFR)

| ID    | Requirement | Target |
|-------|-------------|--------|
| NFR-1 | Latency | <!-- e.g. p95 < 500ms --> |
| NFR-2 | Availability | <!-- e.g. 99.9% uptime --> |
| NFR-3 | Scalability | <!-- e.g. supports N concurrent users --> |
| NFR-4 | Security | <!-- e.g. auth, data encryption --> |
| NFR-5 | Observability | <!-- logging, tracing, alerting --> |

### 2.3 Out of Scope (MVP)

- <!-- Feature deferred to v2 -->
- <!-- Feature deferred to v2 -->

---

## 3. Architecture Overview

> _High-level diagram description or ASCII diagram._

```
[ Client ] ──► [ API Gateway ] ──► [ Service A ]
                                 ──► [ Service B ]
                                          │
                                     [ Database ]
```

**Architecture style:** <!-- e.g. Monolith / Microservices / Serverless -->

---

## 4. Tech Stack

| Layer | Technology | Reason |
|-------|-----------|--------|
| Frontend | <!-- e.g. React, Next.js --> | <!-- --> |
| Backend | <!-- e.g. FastAPI, Node.js --> | <!-- --> |
| Database | <!-- e.g. PostgreSQL, MongoDB --> | <!-- --> |
| AI/ML | <!-- e.g. Claude API, OpenAI --> | <!-- --> |
| Infrastructure | <!-- e.g. AWS, GCP, Vercel --> | <!-- --> |
| Auth | <!-- e.g. Clerk, Auth0, JWT --> | <!-- --> |

---

## 5. Data Model

> _Key entities and their relationships._

### Entity: `<!-- Name -->`
```
id          UUID    PK
<!-- field --> <!-- type -->
created_at  TIMESTAMP
```

### Entity: `<!-- Name -->`
```
id          UUID    PK
<!-- field --> <!-- type -->
```

**Relationships:**
- <!-- e.g. User has many Projects -->

---

## 6. API Design

### Endpoints

| Method | Path | Description | Auth Required |
|--------|------|-------------|---------------|
| POST | `/api/<!-- -->` | <!-- --> | Yes/No |
| GET  | `/api/<!-- -->` | <!-- --> | Yes/No |
| PUT  | `/api/<!-- -->` | <!-- --> | Yes/No |
| DELETE | `/api/<!-- -->` | <!-- --> | Yes/No |

---

## 7. Key Flows

### Flow 1: <!-- e.g. User Onboarding -->

1. <!-- Step 1 -->
2. <!-- Step 2 -->
3. <!-- Step 3 -->

### Flow 2: <!-- e.g. Core Feature Flow -->

1. <!-- Step 1 -->
2. <!-- Step 2 -->
3. <!-- Step 3 -->

---

## 8. AI / Agent Design

> _Fill in if the project uses LLMs or agents._

**Model(s):** <!-- e.g. claude-sonnet-4-6 -->  
**Prompting strategy:** <!-- e.g. system prompt + tool use -->  
**Tools / Function calls:**
- `<!-- tool_name -->` — <!-- what it does -->

**Context window considerations:** <!-- -->  
**Prompt caching strategy:** <!-- -->

---

## 9. Infrastructure & Deployment

**Environments:** `dev` | `staging` | `prod`  
**CI/CD:** <!-- e.g. GitHub Actions -->  
**Hosting:** <!-- -->  
**Environment variables needed:**
- `<!-- VAR_NAME -->` — <!-- purpose -->

---

## 10. Security Considerations

- [ ] <!-- e.g. All secrets in env vars, not in code -->
- [ ] <!-- e.g. Input validation on all API endpoints -->
- [ ] <!-- e.g. Rate limiting -->
- [ ] <!-- e.g. HTTPS only -->

---

## 11. Open Questions

| # | Question | Owner | Status |
|---|----------|-------|--------|
| 1 | <!-- --> | <!-- --> | Open |
| 2 | <!-- --> | <!-- --> | Open |

---

## 12. Milestones

| Milestone | Description | Target Date |
|-----------|-------------|-------------|
| M1 | <!-- e.g. Core backend + DB schema --> | <!-- --> |
| M2 | <!-- e.g. Frontend scaffold + auth --> | <!-- --> |
| M3 | <!-- e.g. AI feature integrated --> | <!-- --> |
| M4 | <!-- e.g. MVP demo-ready --> | <!-- --> |
