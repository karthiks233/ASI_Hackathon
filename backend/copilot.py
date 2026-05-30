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
A disruption has put sectors over capacity and pushed flights into weather.

Your job: propose the SINGLE most valuable next mitigation to relieve the worst conflict,
with the least delay and distance added, creating no new conflicts.

Rules:
- Prefer rerouting or altitude changes for weather conflicts.
- Prefer ground holds (DELAY) for over-demand when rerouting is unavailable.
- Always evaluate a candidate fix with the what-if tools before recommending it.
- Return ONLY ONE mitigation — the best single fix.
- The operator will call you again for the next fix after applying yours.

Respond by calling the `recommend` tool with the chosen mitigation and a concise,
plain-language rationale that mentions the specific flight and sector."""

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

def _build_world_summary(state: ScenarioState) -> str:
    from datetime import timedelta
    mid_t = state.t_start + (state.t_end - state.t_start) / 2
    frame = compute_frame(state, mid_t)
    od = frame.metrics.over_demand_sectors
    wx = frame.metrics.weather_flights
    cl = frame.metrics.closed_flights
    top = frame.conflicts[:5]
    parts = []
    if od:
        parts.append(f"{od} sector(s) over capacity")
    if wx:
        parts.append(f"{wx} flight(s) in weather")
    if cl:
        parts.append(f"{cl} flight(s) in closed sectors")
    if not parts:
        return "All clear — no conflicts detected."
    summary = "; ".join(parts) + "."
    if top:
        summary += " Top conflicts: " + ", ".join(c.label for c in top[:3]) + "."
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

    user_content = (
        f"Current conflict snapshot:\n{world_summary}\n\n"
        f"Simulation time window: {state.t_start.isoformat()} to {state.t_end.isoformat()}.\n"
        f"Analyse the conflicts, evaluate candidate fixes, and call `recommend` with the best single mitigation."
    )

    messages = [{"role": "user", "content": user_content}]

    log.info("Calling Claude co-pilot (%s)…", CLAUDE_MODEL)
    recommended: dict | None = None

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

            result = _exec_tool(tool_name, tool_input, state, mid_t)
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
        log.warning("Claude did not call recommend — falling back")
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


# ── Public entrypoint ───────────────────────────────────────────────────────────

def suggest(state: ScenarioState) -> CopilotSuggestResponse:
    if not ANTHROPIC_API_KEY:
        log.info("No ANTHROPIC_API_KEY — using deterministic fallback")
        return _fallback_suggest(state)
    try:
        return _claude_suggest(state)
    except Exception as e:
        log.error("Claude co-pilot error: %s", e, exc_info=True)
        return _fallback_suggest(state)
