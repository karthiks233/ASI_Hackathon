"""Claude tool-use co-pilot + deterministic fallback."""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from .config import ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_COPILOT_ROUNDS
from .mitigations import (
    deterministic_best,
    evaluate_altitude,
    evaluate_delay,
    evaluate_reroute,
)
from .schemas import CopilotSuggestResponse, Mitigation, MitigationImpact
from .simulation import ScenarioState, compute_frame

log = logging.getLogger(__name__)

# ── System prompt (cached prefix) ──────────────────────────────────────────────

_SYSTEM_PROMPT = """You are the AI co-pilot in an air-traffic flow-management war room.
Several sectors of the National Airspace are OVER CAPACITY — more flights are routed
through them at once than their capacity allows (over-demand).

Your job: propose the SINGLE most valuable next mitigation that FULLY CLEARS an
over-demand sector (brings it back to capacity), with the least delay and distance
added, creating no new over-demand.

Levers available (all evaluated against real sector occupancy):
- REROUTE a contributing flight laterally so it transits a less-loaded sector.
- DELAY a contributing flight so it enters the busy sector at a calmer time.
- ALTITUDE: move a flight to the other band (HIGH/LOW) to shift it out of the sector.

Strategy — this matters:
- A single fix moves ONE flight, so it can only clear a sector that is just 1–2 flights
  over capacity. PREFER these "winnable" sectors (e.g. 21/20) — one good fix clears them
  and visibly drops the conflict count.
- Use `list_conflicts` to see how far over each sector is. Pick a sector that is barely
  over, find a contributing flight, and evaluate fixes until one shows conflicts_resolved >= 1
  and conflicts_created == 0.
- Don't waste the turn on a sector that is many flights over (e.g. 26/20) — one fix won't
  clear it and the impact will read 0.

Rules:
- Always evaluate a candidate fix with the what-if tools before recommending it.
- Recommend the fix with conflicts_resolved >= 1 and the least added delay/distance.
- Return ONLY ONE mitigation — the best single fix.
- The operator will call you again for the next fix after applying yours.

Respond by calling the `recommend` tool with the chosen mitigation and a concise,
plain-language rationale that names the specific flight and the sector it relieves."""

# ── Tool definitions ────────────────────────────────────────────────────────────

_TOOLS = [
    {
        "name": "list_conflicts",
        "description": "List current conflicts ranked by severity. Returns top_n conflicts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["OVER_DEMAND", "WEATHER", "CLOSED_SECTOR"],
                    "description": "Filter by conflict kind (optional).",
                },
                "top_n": {"type": "integer", "description": "Max results (default 10)."},
            },
        },
    },
    {
        "name": "get_sector",
        "description": "Get a sector's capacity, current load, and biggest contributing flights.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Sector name e.g. HIGH_142"}},
            "required": ["name"],
        },
    },
    {
        "name": "get_flight",
        "description": "Get a flight's route, altitude, timing, and which conflicts it's in.",
        "input_schema": {
            "type": "object",
            "properties": {"flight_id": {"type": "string"}},
            "required": ["flight_id"],
        },
    },
    {
        "name": "evaluate_reroute",
        "description": "Simulate a lateral detour for a flight. Returns impact WITHOUT committing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "flight_id": {"type": "string"},
                "side": {"type": "string", "enum": ["north", "south", "east", "west"]},
                "around": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Sector names to route around.",
                },
            },
            "required": ["flight_id", "side", "around"],
        },
    },
    {
        "name": "evaluate_delay",
        "description": "Simulate a ground hold for a flight. Returns impact WITHOUT committing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "flight_id": {"type": "string"},
                "minutes": {"type": "number", "description": "Hold duration in minutes."},
            },
            "required": ["flight_id", "minutes"],
        },
    },
    {
        "name": "evaluate_altitude",
        "description": "Simulate an altitude change for a flight. Returns impact WITHOUT committing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "flight_id": {"type": "string"},
                "new_alt_ft": {"type": "integer", "description": "New cruise altitude in feet."},
            },
            "required": ["flight_id", "new_alt_ft"],
        },
    },
    {
        "name": "recommend",
        "description": "Emit the chosen mitigation back to the operator. Call this LAST.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["REROUTE", "DELAY", "ALTITUDE"]},
                "flight_id": {"type": "string"},
                "params": {
                    "type": "object",
                    "description": "Action params: {side, around} for REROUTE; {minutes} for DELAY; {new_alt_ft} for ALTITUDE.",
                },
                "rationale": {"type": "string", "description": "Plain-language explanation."},
            },
            "required": ["action", "flight_id", "params", "rationale"],
        },
    },
]


# ── Tool execution ──────────────────────────────────────────────────────────────

def _exec_tool(name: str, inp: dict, state: ScenarioState, mid_t) -> Any:
    if name == "list_conflicts":
        frame = compute_frame(state, mid_t)
        kind_filter = inp.get("kind")
        top_n = int(inp.get("top_n", 10))
        conflicts = frame.conflicts
        if kind_filter:
            conflicts = [c for c in conflicts if c.kind == kind_filter]
        return [c.model_dump() for c in conflicts[:top_n]]

    elif name == "get_sector":
        sname = inp["name"]
        sector = state.sectors.get(sname)
        if sector is None:
            return {"error": f"Sector {sname} not found"}
        frame = compute_frame(state, mid_t)
        sector_frame = next((s for s in frame.sectors if s.name == sname), None)
        count = sector_frame.count if sector_frame else 0
        flight_ids_in_sector = [
            v.flight_id for v in state.visits_by_sector.get(sname, [])
            if v.enter_t <= mid_t < v.exit_t
        ]
        return {
            "name": sname,
            "band": sector.band,
            "capacity": sector.capacity,
            "current_count": count,
            "ratio": round(count / sector.capacity, 2) if sector.capacity else 0,
            "closed": sname in state.closed_sectors,
            "flight_ids": flight_ids_in_sector[:20],
        }

    elif name == "get_flight":
        fid = inp["flight_id"]
        flight = state.flights.get(fid)
        if flight is None:
            return {"error": f"Flight {fid} not found"}
        frame = compute_frame(state, mid_t)
        in_conflicts = [c.label for c in frame.conflicts if fid in c.flight_ids]
        return {
            "flight_id": fid,
            "flight_number": flight.flight_number,
            "origin": flight.origin,
            "dest": flight.dest,
            "cruise_alt_ft": flight.cruise_alt_ft,
            "cruise_speed_kt": flight.cruise_speed_kt,
            "band": flight.band,
            "t0": flight.t0.isoformat(),
            "t1": flight.t1.isoformat(),
            "conflicts": in_conflicts,
            "waypoint_count": len(flight.lats),
        }

    elif name == "evaluate_reroute":
        impact, _ = evaluate_reroute(
            state, inp["flight_id"], inp["side"], inp.get("around", [])
        )
        return impact.model_dump()

    elif name == "evaluate_delay":
        impact, _ = evaluate_delay(state, inp["flight_id"], float(inp["minutes"]))
        return impact.model_dump()

    elif name == "evaluate_altitude":
        impact, _ = evaluate_altitude(state, inp["flight_id"], int(inp["new_alt_ft"]))
        return impact.model_dump()

    return {"error": f"Unknown tool: {name}"}


# ── World summary builder ───────────────────────────────────────────────────────

def _winnable_targets(state: ScenarioState, k: int = 5) -> list[dict]:
    """The k over-demand sectors closest to capacity (most likely clearable by one
    fix), each with peak occupancy, capacity, and a few contributing flights."""
    from .simulation import build_occupancy
    grid = build_occupancy(state)
    rows = []
    for sname, occ in grid.items():
        cap = state.sectors[sname].capacity
        peak = max(occ)
        if peak > cap:
            rows.append((peak - cap, sname, peak, cap))
    rows.sort(key=lambda r: r[0])  # smallest overage first
    out = []
    for overage, sname, peak, cap in rows[:k]:
        flights = list(dict.fromkeys(v.flight_id for v in state.visits_by_sector.get(sname, [])))[:4]
        out.append({"sector": sname, "peak": peak, "capacity": cap,
                    "over_by": overage, "contributing_flights": flights})
    return out


def _build_world_summary(state: ScenarioState) -> str:
    from .simulation import overdemand_keyset
    od = len({s for _, s in overdemand_keyset(state)})
    if od == 0:
        return "All clear — no sectors over capacity."
    targets = _winnable_targets(state, 3)
    summary = f"{od} sector(s) over capacity."
    if targets:
        summary += " Closest to capacity: " + ", ".join(
            f"{t['sector']} {t['peak']}/{t['capacity']}" for t in targets
        ) + "."
    return summary


# ── Claude suggest ─────────────────────────────────────────────────────────────

def _claude_suggest(state: ScenarioState) -> CopilotSuggestResponse:
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    mid_t = state.t_start + (state.t_end - state.t_start) / 2
    world_summary = _build_world_summary(state)

    if "All clear" in world_summary:
        return CopilotSuggestResponse(
            mitigation=None, world_summary=world_summary, source="claude"
        )

    system_blocks = [
        {
            "type": "text",
            "text": _SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    targets = _winnable_targets(state, 5)
    targets_json = json.dumps(targets, indent=2)
    user_content = (
        f"Current conflict snapshot:\n{world_summary}\n\n"
        f"The most winnable over-demand sectors (closest to capacity) and their contributing "
        f"flights are below. Start here — these can usually be cleared by moving ONE flight:\n"
        f"{targets_json}\n\n"
        f"Pick one target, evaluate at most 2-3 candidate fixes on its contributing flights "
        f"(use the evaluate_* tools), then call `recommend` AS SOON AS you find one with "
        f"conflicts_resolved >= 1 and conflicts_created == 0. Do not keep exploring — converge quickly."
    )

    messages = [{"role": "user", "content": user_content}]

    log.info("Calling Claude co-pilot (%s)…", CLAUDE_MODEL)
    recommended: dict | None = None
    # Track the best fix Claude actually evaluated, so its work isn't wasted if it
    # never gets around to calling `recommend`.
    best_eval: tuple[dict, str, dict] | None = None  # (impact_dict, action, tool_input)
    _ACTION_BY_TOOL = {"evaluate_reroute": "REROUTE", "evaluate_delay": "DELAY", "evaluate_altitude": "ALTITUDE"}

    for round_num in range(MAX_COPILOT_ROUNDS):
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            system=system_blocks,
            tools=_TOOLS,
            messages=messages,
        )

        # Add assistant response to history
        messages.append({"role": "assistant", "content": response.content})

        # Process tool calls
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            tool_name = block.name
            tool_input = block.input

            log.debug("Claude calls tool %s with %s", tool_name, tool_input)

            if tool_name == "recommend":
                recommended = tool_input
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps({"status": "mitigation recorded"}),
                })
                break

            try:
                result = _exec_tool(tool_name, tool_input, state, mid_t)
            except Exception as e:
                # Feed the error back to Claude so it can retry (e.g. with a valid
                # flight_id) instead of aborting the whole session.
                result = {"error": str(e)}

            # Remember the best winning evaluation seen
            if tool_name in _ACTION_BY_TOOL and isinstance(result, dict) and "conflicts_resolved" in result:
                if result["conflicts_resolved"] >= 1 and result["conflicts_created"] == 0:
                    if best_eval is None or (
                        result["conflicts_resolved"], -result["delay_min"]
                    ) > (best_eval[0]["conflicts_resolved"], -best_eval[0]["delay_min"]):
                        best_eval = (result, _ACTION_BY_TOOL[tool_name], tool_input)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result),
            })

        if recommended:
            break

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        if response.stop_reason == "end_turn":
            break

    if recommended is None:
        if best_eval is not None:
            # Claude never called recommend, but it found a valid winning fix —
            # promote that rather than discarding its work.
            impact_dict, action, tinput = best_eval
            log.info("Claude didn't call recommend; promoting its best evaluated fix (%s)", action)
            recommended = {
                "action": action,
                "flight_id": tinput["flight_id"],
                "params": {k: v for k, v in tinput.items() if k != "flight_id"},
                "rationale": f"{action.title()} {tinput['flight_id'].split('|')[0]} to relieve the "
                             f"targeted over-demand sector — clears "
                             f"{impact_dict['conflicts_resolved']} sector(s) with no new over-demand.",
            }
        else:
            log.warning("Claude did not call recommend and found no winning fix — falling back")
            return _fallback_suggest(state)

    # Build the Mitigation from Claude's recommend call
    action = recommended["action"]
    flight_id = recommended["flight_id"]
    params = recommended.get("params", {})
    rationale = recommended.get("rationale", "")
    flight = state.flights.get(flight_id)
    flight_number = flight.flight_number if flight else flight_id

    # Evaluate the actual impact
    try:
        if action == "REROUTE":
            impact, _ = evaluate_reroute(state, flight_id, params.get("side", "north"), params.get("around", []))
        elif action == "DELAY":
            impact, _ = evaluate_delay(state, flight_id, float(params.get("minutes", 30)))
        elif action == "ALTITUDE":
            impact, _ = evaluate_altitude(state, flight_id, int(params.get("new_alt_ft", 37000)))
        else:
            impact = MitigationImpact(conflicts_resolved=0, conflicts_created=0, delay_min=0, extra_nm=0)
    except Exception as e:
        log.warning("Impact evaluation failed: %s", e)
        impact = MitigationImpact(conflicts_resolved=0, conflicts_created=0, delay_min=0, extra_nm=0)

    mitigation = Mitigation(
        id="m_" + uuid.uuid4().hex[:6],
        action=action,
        flight_id=flight_id,
        flight_number=flight_number,
        params=params,
        impact=impact,
        rationale=rationale,
    )

    return CopilotSuggestResponse(
        mitigation=mitigation,
        world_summary=world_summary,
        source="claude",
    )


# ── Fallback ────────────────────────────────────────────────────────────────────

def _fallback_suggest(state: ScenarioState) -> CopilotSuggestResponse:
    world_summary = _build_world_summary(state)
    mitigation = deterministic_best(state)
    return CopilotSuggestResponse(
        mitigation=mitigation,
        world_summary=world_summary,
        source="fallback",
    )


# ── Claude rationale (hybrid path) ──────────────────────────────────────────────

def _claude_rationale(state: ScenarioState, mit: Mitigation, world_summary: str) -> str:
    """One fast, no-tool Claude call to write an operational rationale for a fix the
    solver already found and verified. Reliable and quick (the tool-use search is the
    slow, flaky part — this keeps Claude doing what it's best at: explaining)."""
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    target = (mit.params.get("around") or ["the targeted sector"])[0]
    user = (
        f"Airspace status: {world_summary}\n\n"
        f"Recommended fix (already verified against the simulation):\n"
        f"- Action: {mit.action}\n"
        f"- Flight: {mit.flight_number} ({mit.flight_id})\n"
        f"- Params: {json.dumps(mit.params)}\n"
        f"- Relieves sector: {target}\n"
        f"- Measured impact: clears {mit.impact.conflicts_resolved} over-demand sector(s), "
        f"creates {mit.impact.conflicts_created} new, +{mit.impact.delay_min:.0f} min delay, "
        f"+{mit.impact.extra_nm:.0f} nm.\n\n"
        f"Write ONE or TWO crisp sentences a controller would say, explaining why this is a "
        f"good next move. Name the flight and sector. No preamble — just the rationale."
    )
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=200,
        system=[{"type": "text",
                 "text": "You are an AI co-pilot in an air-traffic flow-management war room. "
                         "You write tight, confident, operational rationales for sector over-demand fixes.",
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return " ".join(parts).strip() or mit.rationale


# ── Public entrypoint ───────────────────────────────────────────────────────────

def suggest(state: ScenarioState) -> CopilotSuggestResponse:
    """Hybrid: the deterministic solver finds and verifies the best single fix; Claude
    explains it. Falls back to the templated rationale if Claude is unavailable."""
    world_summary = _build_world_summary(state)
    if "All clear" in world_summary:
        return CopilotSuggestResponse(mitigation=None, world_summary=world_summary, source="claude")

    mitigation = deterministic_best(state)
    if mitigation is None:
        return CopilotSuggestResponse(mitigation=None, world_summary=world_summary, source="fallback")

    if not ANTHROPIC_API_KEY:
        return CopilotSuggestResponse(mitigation=mitigation, world_summary=world_summary, source="fallback")

    try:
        mitigation.rationale = _claude_rationale(state, mitigation, world_summary)
        return CopilotSuggestResponse(mitigation=mitigation, world_summary=world_summary, source="claude")
    except Exception as e:
        log.error("Claude rationale failed, using template: %s", e)
        return CopilotSuggestResponse(mitigation=mitigation, world_summary=world_summary, source="fallback")
