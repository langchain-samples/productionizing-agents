"""ARIA's application layer, exposed over MCP.

Run it standalone:

    python -m aria_mcp.server              # stdio (what the agent uses)
    python -m aria_mcp.server --http       # http on :8000/mcp (what you demo with)

This file is intentionally thin. Every tool is a few lines: validate nothing (the
repository does that), call the repository, shape the result. All the logic lives in
`repository.py` where it is unit-tested without an LLM anywhere in sight.

WHY MCP AND NOT JUST PASSING PYTHON FUNCTIONS TO create_agent
-------------------------------------------------------------
You can absolutely hand `@tool`-decorated functions straight to an agent, and for a
prototype you should. The reason to put a protocol boundary here as you productionize:

1.  It forces the separation to be real. Once your tools are behind a process boundary,
    it becomes impossible to reach into agent state from application code or vice versa.
    Shared-mutable-state coupling between "app" and "agent" is very hard to unpick later.

2.  It makes the surface independently testable and independently deployable. `pytest
    tests/test_repository.py` runs in milliseconds with no API keys. The MCP server can be
    versioned, load-tested, and monitored like any other service — because it is one.

3.  One application, many consumers. The same server serves this agent, a different
    agent, an internal Claude Code / IDE integration, and a human with an MCP client.
    You write the plant-data contract once.

4.  It gives you a place to enforce authorization. Per-caller permissions belong on the
    application side of the boundary, where they can be audited, not in a prompt.

TOOL DESCRIPTIONS ARE PROMPT ENGINEERING
----------------------------------------
Every docstring below is read by the model on every single turn. They are the highest
leverage prompt real estate you have, and unlike your system prompt they are attached to
the thing they describe. Write them for a competent new hire who is in a hurry: what the
tool does, when to reach for it, when NOT to, and what the failure looks like.
"""

from __future__ import annotations

import argparse
import sys
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from aria_mcp.repository import ARIALookupError, get_repository

mcp = FastMCP(
    name="aria-plant-data",
    instructions=(
        "Reference data for refinery maintenance and HSE work: the written procedure "
        "library, the equipment register, and live tank status. Every answer that "
        "concerns a procedure must cite the procedure id and revision returned by these "
        "tools. This server is the only authoritative source; do not answer procedural "
        "questions from general knowledge."
    ),
)


# ---------------------------------------------------------------------------------------
# PER-ARGUMENT DESCRIPTIONS — a gotcha that costs people days
# ---------------------------------------------------------------------------------------
# A tool's docstring becomes its *tool-level* description automatically. The per-argument
# descriptions do NOT come along for free, and the two ecosystems fix it differently:
#
#   LangChain @tool     ->  @tool(parse_docstring=True)
#                           Parses the Google-style `Args:` section and attaches each entry
#                           to its parameter. WITHOUT IT, the properties in your JSON schema
#                           are bare `{"title": "Tag", "type": "string"}` — the model gets
#                           your prose only as an undifferentiated blob in the description,
#                           not attached to the argument it describes.
#
#   FastMCP (this file) ->  Annotated[str, Field(description=...)]
#                           `parse_docstring` does not exist here. FastMCP builds the schema
#                           from type hints, so the description has to live in the
#                           annotation.
#
# How to check, rather than trust: print the schema.
#
#     tool.args_schema.model_json_schema()["properties"]
#
# If your properties have no `description` keys, the model is flying blind on arguments and
# you will see it as "the model keeps passing the wrong format" — which reads like a model
# problem and is actually a plumbing problem.
#
# The descriptions live in constants so the two transports can be asserted identical.
# `tests/test_tool_parity.py` compares per-argument descriptions across both surfaces, so
# this duplication is guarded rather than merely hoped about.

QUERY_DESC = (
    'A topic ("confined space entry", "lockout tagout", "H2S exposure") or an equipment '
    'tag ("P-101A"). Must be at least 3 characters and contain at least one meaningful '
    "word. Equipment tags score highest, so searching by tag is the fastest way to find "
    "everything that governs a specific asset."
)
LIMIT_DESC = "Maximum procedures to return, 1-5. Default 5."
PROCEDURE_ID_DESC = 'An id like "SOP-CSE-003". Case-insensitive.'
EQUIPMENT_TAG_DESC = (
    'An equipment tag like "P-101A", "V-205", "C-401", "K-501". Case-insensitive. Note '
    'that many pumps are installed in A/B pairs — "P-101" alone is not a tag and the '
    "error will tell you which suffixes exist."
)
UNIT_DESC = (
    'Optional unit name, e.g. "Crude Unit 1", "Naphtha Hydrotreater", "Reformer", '
    '"Sulfur Recovery". Case-insensitive but must match a real unit; the error lists '
    "valid units."
)
TANK_TAG_DESC = 'A tank tag like "T-042". Case-insensitive.'
WO_TAG_DESC = 'The asset the work is against, e.g. "P-101A". Must exist.'
WO_DESCRIPTION_DESC = (
    "The scope of work, at least 15 characters. Write it for a planner who needs to "
    "assign a craft and a duration: what is wrong, what needs doing."
)
WO_PRIORITY_DESC = (
    'One of "emergency" (active safety hazard), "urgent", "routine", or "shutdown" '
    '(defer to next turnaround). Default "routine". Do not choose "emergency" unless the '
    "user has described an active hazard."
)
WO_PROCEDURES_DESC = (
    'Governing procedure ids, e.g. ["SOP-LOTO-014", "SOP-MECH-108"]. Each is validated; '
    "an invented id is rejected."
)
WO_REQUESTED_BY_DESC = "The person requesting the work, if the user gave a name."
SR_TAG_DESC = "The running asset to be taken out of service."
SR_REASON_DESC = (
    "The observed condition and the justification. Goes in front of the Unit Supervisor."
)
SR_REQUESTED_BY_DESC = "Name or badge number of the requesting human. Required."
SR_PROCEDURES_DESC = "Governing procedures for the shutdown and subsequent work."
CWO_ID_DESC = 'The id to close, e.g. "WO-90001". Case-insensitive. Must exist.'
CWO_NOTES_DESC = (
    "What was actually done, at least 15 characters. These notes become the asset's "
    "maintenance history and the next person to work on this equipment reads them — record "
    "findings, parts used, and anything left outstanding."
)
CWO_BY_DESC = (
    "Name or badge number of the human who did or verified the work. Required; you cannot "
    "sign off work yourself. Ask if the user has not said."
)
LWO_TAG_DESC = 'Optional filter, e.g. "P-101A". Omit to list everything.'


def _error(exc: ARIALookupError) -> dict[str, Any]:
    """Shape a lookup failure into something an agent can act on.

    A note on a real design tradeoff: we return errors as *successful* tool results with
    an `error` key, rather than raising. Returning means the model reliably sees the full
    recovery hint and can retry in the same turn. Raising would surface the call as an
    error in LangSmith, which is better for your dashboards.

    We chose recovery, and we get the observability back a different way — the `error`
    key is a filterable field on the trace, so Module 3's monitoring watches
    `outputs` for it. Pick one and be consistent; the failure mode to avoid is doing
    both inconsistently across tools, which makes your error rate meaningless.
    """
    return {"error": str(exc), "recoverable": True}


@mcp.tool()
def search_procedures(
    query: Annotated[str, Field(description=QUERY_DESC)],
    limit: Annotated[int, Field(description=LIMIT_DESC)] = 5,
) -> dict[str, Any]:
    """Search the written procedure library by topic or equipment tag.

    This is your entry point for any question about how work is supposed to be done:
    isolation, entry, hot work, line breaking, gauging, emergency response. Search first,
    then read the specific procedure with `get_procedure` if you need the full text.

    Args:
        query: A topic ("confined space entry", "lockout tagout", "H2S exposure") or an
            equipment tag ("P-101A"). Must be at least 3 characters and contain at least
            one meaningful word. Equipment tags score highest, so searching by tag is the
            fastest way to find everything that governs a specific asset.
        limit: Maximum procedures to return, 1-5. Default 5.

    Returns:
        `{"results": [...], "count": n}` where each result has `id`, `title`, `revision`,
        `effective_date`, `applies_to`, `body` (possibly truncated), and `citation` — a
        preformatted string you should quote verbatim rather than assembling yourself.
        `{"results": [], "count": 0}` means the library genuinely has nothing on that
        topic; say so plainly rather than answering from general knowledge.
        `{"error": ...}` means the query was malformed; the message says how to fix it.
    """
    try:
        results = get_repository().search_procedures(query, limit=limit)
    except ARIALookupError as exc:
        return _error(exc)
    return {"results": results, "count": len(results)}


@mcp.tool()
def get_procedure(
    procedure_id: Annotated[str, Field(description=PROCEDURE_ID_DESC)],
) -> dict[str, Any]:
    """Retrieve the complete, untruncated text of one procedure by its id.

    Use this after `search_procedures` when you need every step, or when the user names a
    procedure directly. Never guess an id — if you are not certain, search instead. The
    error message from a wrong id lists every valid id, so a wrong guess is recoverable,
    but it costs a turn.

    Args:
        procedure_id: An id like "SOP-CSE-003". Case-insensitive.

    Returns:
        The full procedure record including `body`, `revision`, `effective_date`, and
        `citation`. Some procedures carry a `notes` field describing what changed in the
        current revision — read it, because it usually exists precisely because an earlier
        revision said something that is now wrong.
    """
    try:
        return get_repository().get_procedure(procedure_id)
    except ARIALookupError as exc:
        return _error(exc)


@mcp.tool()
def get_equipment(
    tag: Annotated[str, Field(description=EQUIPMENT_TAG_DESC)],
) -> dict[str, Any]:
    """Look up the register entry for a piece of equipment: pump, vessel, column,
    exchanger, or compressor.

    Tells you what the asset is, what it is in service on, its hazards, its area
    electrical classification, its current status, its PM and inspection dates, its open
    work orders, and — importantly — `applicable_procedures`, the list of procedure ids
    that govern work on it. Reach for those ids rather than searching blind.

    Tanks are NOT here. Use `get_tank_status` for anything starting with "T-".

    Args:
        tag: An equipment tag like "P-101A", "V-205", "C-401", "K-501". Case-insensitive.
            Note that many pumps are installed in A/B pairs — "P-101" alone is not a tag
            and the error will tell you which suffixes exist.

    Returns:
        The full equipment record, or `{"error": ...}` naming near-miss tags if the tag
        is unknown.
    """
    try:
        return get_repository().get_equipment(tag)
    except ARIALookupError as exc:
        return _error(exc)


@mcp.tool()
def list_equipment(
    unit: Annotated[str | None, Field(description=UNIT_DESC)] = None,
) -> dict[str, Any]:
    """List equipment tags with a one-line description, optionally scoped to one unit.

    Use this to orient yourself when the user refers to equipment vaguely ("the crude
    charge pumps", "the amine drum") and you need to resolve it to a tag. It returns
    summaries only — follow up with `get_equipment` for the details on the one you want.

    Args:
        unit: Optional unit name, e.g. "Crude Unit 1", "Naphtha Hydrotreater", "Reformer",
            "Sulfur Recovery". Case-insensitive but must match a real unit; the error
            lists valid units.

    Returns:
        `{"equipment": [{"tag", "description", "unit", "status", "criticality"}, ...],
        "count": n}`, sorted by tag.
    """
    try:
        items = get_repository().list_equipment(unit)
    except ARIALookupError as exc:
        return _error(exc)
    return {"equipment": items, "count": len(items)}


@mcp.tool()
def get_tank_status(
    tag: Annotated[str, Field(description=TANK_TAG_DESC)],
) -> dict[str, Any]:
    """Current status of a storage tank: level, temperature, gauging history, inspection
    dates, and any data-quality problems.

    Pay attention to `data_quality_warnings`. It is a precomputed list of conditions that
    make the level reading unreliable or the situation hazardous — a suspect automatic
    gauge, a receipt in progress, or a level close to the high-high alarm. If that list is
    non-empty you must surface every warning in it to the user. Reporting a level from a
    suspect gauge as though it were fact is exactly the failure this tool exists to
    prevent.

    Args:
        tag: A tank tag like "T-042". Case-insensitive.

    Returns:
        The tank record plus `data_quality_warnings: list[str]`, or `{"error": ...}`
        listing valid tank tags.
    """
    try:
        return get_repository().get_tank_status(tag)
    except ARIALookupError as exc:
        return _error(exc)


@mcp.tool()
def create_work_order(
    equipment_tag: Annotated[str, Field(description=WO_TAG_DESC)],
    description: Annotated[str, Field(description=WO_DESCRIPTION_DESC)],
    priority: Annotated[str, Field(description=WO_PRIORITY_DESC)] = "routine",
    procedure_ids: Annotated[list[str] | None, Field(description=WO_PROCEDURES_DESC)] = None,
    requested_by: Annotated[str, Field(description=WO_REQUESTED_BY_DESC)] = "ARIA",
) -> dict[str, Any]:
    """Create a maintenance work order against a piece of equipment.

    Use this when the user asks you to raise, schedule, or write up work. Always look up
    the equipment first and include the governing procedure ids from its
    `applicable_procedures` — a work order that does not reference its procedures makes the
    planner go find them again.

    Args:
        equipment_tag: The asset the work is against, e.g. "P-101A". Must exist.
        description: The scope of work, at least 15 characters. Write it for a planner who
            needs to assign a craft and a duration: what is wrong, what needs doing.
        priority: One of "emergency" (active safety hazard), "urgent", "routine", or
            "shutdown" (defer to next turnaround). Default "routine". Do not choose
            "emergency" unless the user has described an active hazard.
        procedure_ids: Governing procedure ids, e.g. ["SOP-LOTO-014", "SOP-MECH-108"].
            Each is validated; an invented id is rejected.
        requested_by: The person requesting the work, if the user gave a name.

    Returns:
        The created work order including its assigned `id`, plus `created: true`. If an
        identical request was already made this session you get the original back with
        `created: false` and `duplicate_of` set — that is idempotency working, not an
        error, and you should tell the user the work order already exists rather than
        trying again.
    """
    from aria_mcp.work_orders import get_store

    try:
        return get_store().create_work_order(
            equipment_tag=equipment_tag,
            description=description,
            priority=priority,
            procedure_ids=procedure_ids,
            requested_by=requested_by,
        )
    except ARIALookupError as exc:
        return _error(exc)


@mcp.tool()
def request_equipment_shutdown(
    equipment_tag: Annotated[str, Field(description=SR_TAG_DESC)],
    reason: Annotated[str, Field(description=SR_REASON_DESC)],
    requested_by: Annotated[str, Field(description=SR_REQUESTED_BY_DESC)],
    procedure_ids: Annotated[list[str] | None, Field(description=SR_PROCEDURES_DESC)] = None,
) -> dict[str, Any]:
    """Request that a running piece of equipment be taken OUT OF SERVICE.

    This is a consequential action. Taking a crude charge pump out of service is a
    production decision, and on a criticality-A asset with no running spare it can force a
    unit rate cut. It files a request for supervisor approval — it does not shut anything
    down.

    Requirements, all enforced:
      - The equipment must currently be running or in service.
      - You must give a substantive reason (20+ characters) describing the observed
        condition and why continued operation is unacceptable. A supervisor reads this.
      - You must name the human requesting it. You cannot request a shutdown on your own
        authority; if the user has not identified themselves, ask before calling this.

    Args:
        equipment_tag: The running asset to be taken out of service.
        reason: The observed condition and the justification. Goes in front of the Unit
            Supervisor.
        requested_by: Name or badge number of the requesting human. Required.
        procedure_ids: Governing procedures for the shutdown and subsequent work.

    Returns:
        The filed request with `status: "pending_supervisor_approval"` and an
        `impact_assessment` list. Surface every item in that list to the user — it names
        the production consequence, which is the thing the approver most needs to know.
    """
    from aria_mcp.work_orders import get_store

    try:
        return get_store().request_equipment_shutdown(
            equipment_tag=equipment_tag,
            reason=reason,
            requested_by=requested_by,
            procedure_ids=procedure_ids,
        )
    except ARIALookupError as exc:
        return _error(exc)


@mcp.tool()
def complete_work_order(
    work_order_id: Annotated[str, Field(description=CWO_ID_DESC)],
    completion_notes: Annotated[str, Field(description=CWO_NOTES_DESC)],
    completed_by: Annotated[str, Field(description=CWO_BY_DESC)],
) -> dict[str, Any]:
    """Close out an existing work order as complete.

    Use this when the user reports that work has been finished. You must know the work order
    id — call `list_work_orders` if the user refers to the work without giving an id, and ask
    them to confirm which one rather than guessing.

    A work order can only be closed once. If it is already complete, say so; do not close it
    again, because that would overwrite the original completion record.

    Args:
        work_order_id: The id to close, e.g. "WO-90001". Case-insensitive. Must exist.
        completion_notes: What was actually done, at least 15 characters. These notes become
            the asset's maintenance history and the next person to work on this equipment
            reads them — record findings, parts used, and anything left outstanding.
        completed_by: Name or badge number of the human who did or verified the work.
            Required; you cannot sign off work yourself. Ask if the user has not said.

    Returns:
        The updated work order with `status: "complete"`, or `{"error": ...}` explaining why
        it could not be closed.
    """
    from aria_mcp.work_orders import get_store

    try:
        return get_store().complete_work_order(
            work_order_id=work_order_id,
            completion_notes=completion_notes,
            completed_by=completed_by,
        )
    except ARIALookupError as exc:
        return _error(exc)


@mcp.tool()
def list_work_orders(
    equipment_tag: Annotated[str | None, Field(description=LWO_TAG_DESC)] = None,
) -> dict[str, Any]:
    """List work orders raised in this session, newest first.

    Use this to resolve a vague reference ("close out that pump work order") to an actual id
    before calling `complete_work_order`.

    Args:
        equipment_tag: Optional filter, e.g. "P-101A". Omit to list everything.

    Returns:
        `{"work_orders": [...], "count": n}`.
    """
    from aria_mcp.work_orders import get_store

    orders = get_store().list_work_orders(equipment_tag)
    return {"work_orders": orders, "count": len(orders)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ARIA plant data MCP server")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve over streamable HTTP on :8000/mcp instead of stdio.",
    )
    args = parser.parse_args(argv)

    # Touch the repository at startup so a missing fixture fails loudly here rather
    # than on the agent's first tool call. Fail fast at the boundary you control.
    repo = get_repository()
    print(
        f"aria-plant-data: {len(repo.procedures)} procedures, "
        f"{len(repo.equipment)} equipment, {len(repo.tanks)} tanks",
        file=sys.stderr,
    )

    mcp.run(transport="streamable-http" if args.http else "stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
