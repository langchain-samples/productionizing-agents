"""Two transports, one application.

The same five capabilities are exposed to the agent two ways:

    local_tools()      thin in-process adapters over aria_mcp.repository
    mcp_tools()         the real thing, a subprocess speaking MCP

Both are backed by identical `aria_mcp.repository` calls, and that is the point worth
noticing. Once your application logic lives behind a narrow, tested API, *how* the agent
reaches it becomes a deployment decision rather than an architectural one. You can swap
stdio for HTTP for in-process without touching a line of business logic or re-running your
application test suite.

Which to use:

    mcp_tools()    What you ship. Real process boundary, real protocol, and the same
                   server can serve other agents, an IDE, or a human with an MCP client.
                   Async: MCP is an async protocol and there is no honest way around it.

    local_tools()  What you use in notebooks, unit tests, and eval loops. Synchronous, no
                   subprocess to manage, ~1ms per call. When you are running an eval over
                   200 examples with 8-way concurrency you do not want 200 subprocess
                   handshakes in the measurement.

Keeping both honest is a test, not a convention: `tests/test_tool_parity.py` asserts the
two surfaces expose the same tool names with the same schemas. Without that test this
file is a slowly-diverging liability.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from langchain.tools import tool

from aria_mcp.repository import ARIALookupError, get_repository

SERVER_MODULE = "aria_mcp.server"
REPO_ROOT = Path(__file__).resolve().parent.parent


def _error(exc: ARIALookupError) -> dict[str, Any]:
    return {"error": str(exc), "recoverable": True}


# --------------------------------------------------------------------------- local


@tool(parse_docstring=True)
def search_procedures(query: str, limit: int = 5) -> dict[str, Any]:
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
        `effective_date`, `applies_to`, `body` (possibly truncated), and `citation`, a
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


@tool(parse_docstring=True)
def get_procedure(procedure_id: str) -> dict[str, Any]:
    """Retrieve the complete, untruncated text of one procedure by its id.

    Use this after `search_procedures` when you need every step, or when the user names a
    procedure directly. Never guess an id, if you are not certain, search instead.

    Args:
        procedure_id: An id like "SOP-CSE-003". Case-insensitive.

    Returns:
        The full procedure record including `body`, `revision`, `effective_date`, and
        `citation`. Some procedures carry a `notes` field describing what changed in the
        current revision, read it, because it usually exists precisely because an earlier
        revision said something that is now wrong.
    """
    try:
        return get_repository().get_procedure(procedure_id)
    except ARIALookupError as exc:
        return _error(exc)


@tool(parse_docstring=True)
def get_equipment(tag: str) -> dict[str, Any]:
    """Look up the register entry for a piece of equipment: pump, vessel, column,
    exchanger, or compressor.

    Tells you what the asset is, what it is in service on, its hazards, its area electrical
    classification, its current status, its PM and inspection dates, its open work orders,
    and, importantly, `applicable_procedures`, the list of procedure ids that govern work
    on it. Reach for those ids rather than searching blind.

    Tanks are NOT here. Use `get_tank_status` for anything starting with "T-".

    Args:
        tag: An equipment tag like "P-101A", "V-205", "C-401", "K-501". Case-insensitive.
            Note that many pumps are installed in A/B pairs. "P-101" alone is not a tag
            and the error will tell you which suffixes exist.

    Returns:
        The full equipment record, or `{"error": ...}` naming near-miss tags if the tag is
        unknown.
    """
    try:
        return get_repository().get_equipment(tag)
    except ARIALookupError as exc:
        return _error(exc)


@tool(parse_docstring=True)
def list_equipment(unit: str | None = None) -> dict[str, Any]:
    """List equipment tags with a one-line description, optionally scoped to one unit.

    Use this to orient yourself when the user refers to equipment vaguely ("the crude
    charge pumps", "the amine drum") and you need to resolve it to a tag. It returns
    summaries only, follow up with `get_equipment` for details.

    Args:
        unit: Optional unit name, e.g. "Crude Unit 1", "Naphtha Hydrotreater", "Reformer",
            "Sulfur Recovery". Case-insensitive but must match a real unit; the error lists
            valid units.

    Returns:
        `{"equipment": [{"tag", "description", "unit", "status", "criticality"}, ...],
        "count": n}`, sorted by tag.
    """
    try:
        items = get_repository().list_equipment(unit)
    except ARIALookupError as exc:
        return _error(exc)
    return {"equipment": items, "count": len(items)}


@tool(parse_docstring=True)
def get_tank_status(tag: str) -> dict[str, Any]:
    """Current status of a storage tank: level, temperature, gauging history, inspection
    dates, and any data-quality problems.

    Pay attention to `data_quality_warnings`. It is a precomputed list of conditions that
    make the level reading unreliable or the situation hazardous, a suspect automatic
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


@tool(parse_docstring=True)
def create_work_order(
    equipment_tag: str,
    description: str,
    priority: str = "routine",
    procedure_ids: list[str] | None = None,
    requested_by: str = "ARIA",
) -> dict[str, Any]:
    """Create a maintenance work order against a piece of equipment.

    Use this when the user asks you to raise, schedule, or write up work. Always look up
    the equipment first and include the governing procedure ids from its
    `applicable_procedures`, a work order that does not reference its procedures makes the
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
        `created: false` and `duplicate_of` set, that is idempotency working, not an
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


@tool(parse_docstring=True)
def request_equipment_shutdown(
    equipment_tag: str,
    reason: str,
    requested_by: str,
    procedure_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Request that a running piece of equipment be taken OUT OF SERVICE.

    This is a consequential action. Taking a crude charge pump out of service is a
    production decision, and on a criticality-A asset with no running spare it can force a
    unit rate cut. It files a request for supervisor approval, it does not shut anything
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
        `impact_assessment` list. Surface every item in that list to the user, it names
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


@tool(parse_docstring=True)
def complete_work_order(
    work_order_id: str,
    completion_notes: str,
    completed_by: str,
) -> dict[str, Any]:
    """Close out an existing work order as complete.

    Use this when the user reports that work has been finished. You must know the work order
    id, call `list_work_orders` if the user refers to the work without giving an id, and ask
    them to confirm which one rather than guessing.

    A work order can only be closed once. If it is already complete, say so; do not close it
    again, because that would overwrite the original completion record.

    Args:
        work_order_id: The id to close, e.g. "WO-90001". Case-insensitive. Must exist.
        completion_notes: What was actually done, at least 15 characters. These notes become
            the asset's maintenance history and the next person to work on this equipment
            reads them, record findings, parts used, and anything left outstanding.
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


@tool(parse_docstring=True)
def list_work_orders(equipment_tag: str | None = None) -> dict[str, Any]:
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


#: The read-only surface. Safe to retry, safe to run unattended.
READ_TOOLS = [
    search_procedures,
    get_procedure,
    get_equipment,
    list_equipment,
    get_tank_status,
]

#: The write surface. Not safe to retry blindly; `request_equipment_shutdown` is gated by
#: human-in-the-loop in `aria/agent_v2.py`. Keeping these in a separate list is not
#: cosmetic, it is what lets you configure interrupts, retries, and permissions by
#: category instead of enumerating tool names in four different places.
WRITE_TOOLS = [
    create_work_order,
    complete_work_order,
    request_equipment_shutdown,
]

#: `list_work_orders` reads session state rather than reference data, so it lives with the
#: write surface conceptually but is safe to retry. Kept in READ_TOOLS for permissioning.
READ_TOOLS.append(list_work_orders)

LOCAL_TOOLS = [*READ_TOOLS, *WRITE_TOOLS]

#: Tools that must never execute without explicit human approval.
DESTRUCTIVE_TOOLS: list[str] = ["request_equipment_shutdown"]


def local_tools(*, include_writes: bool = True) -> list[Any]:
    """In-process tools. Synchronous, fast, no subprocess. Use in notebooks and evals.

    Args:
        include_writes: Set False for a read-only agent, useful for the Level 1 and 2
            evals in Module 2, where you want to measure retrieval behavior without any
            chance of a test run filing work orders.
    """
    return list(LOCAL_TOOLS) if include_writes else list(READ_TOOLS)


# ----------------------------------------------------------------------------- mcp


async def mcp_tools(*, transport: str = "stdio", url: str | None = None) -> list[Any]:
    """The same capabilities, reached over MCP. This is the production path.

    Args:
        transport: "stdio" launches `python -m aria_mcp.server` as a subprocess, the right
            choice when the server is a local sidecar. "http" connects to an already-running
            server, which is what you use once the application is deployed independently
            and shared between consumers.
        url: Required for `transport="http"`. Defaults to the local dev server.

    Returns:
        LangChain tools, one per `@mcp.tool()` on the server. Note that the descriptions
        the model sees come from the *server's* docstrings, not from this file, the
        application owns its own contract, which is exactly what you want. Update a tool
        description on the server and every consumer picks it up on reconnect.
    """
    from langchain_mcp_adapters.client import MultiServerMCPClient

    if transport == "stdio":
        connection: dict[str, Any] = {
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-m", SERVER_MODULE],
            # Run from the repo root so the server finds data/ regardless of the caller's cwd.
            "cwd": str(REPO_ROOT),
        }
    elif transport == "http":
        connection = {"transport": "http", "url": url or "http://localhost:8000/mcp"}
    else:
        raise ValueError(f"transport must be 'stdio' or 'http', got {transport!r}")

    client = MultiServerMCPClient({"aria": connection})
    return await client.get_tools()


# ------------------------------------------------------------------------- parity


def arg_schema(t: Any) -> dict[str, dict[str, Any]]:
    """The `properties` block of a tool's argument schema, literally what the model sees."""
    schema = getattr(t, "args_schema", None)
    if schema is None:
        return {}
    if isinstance(schema, dict):
        return schema.get("properties", {}) or {}
    to_json = getattr(schema, "model_json_schema", None)
    return (to_json() if callable(to_json) else {}).get("properties", {}) or {}


def tool_surface(tools: list[Any]) -> dict[str, list[str]]:
    """Tool name -> sorted argument names. Used by the parity test and handy for debugging.

    Run this against both surfaces when a tool call starts failing only in production;
    a schema that drifted between transports is a five-minute bug that can otherwise
    eat an afternoon.
    """
    return {t.name: sorted(arg_schema(t)) for t in tools}


def describe_surface(tools: list[Any]) -> dict[str, dict[str, str]]:
    """Tool name -> {argument: description}. The thing to print when a model keeps getting
    an argument wrong.

    A missing description here is the single most common cause of "the model keeps passing
    the wrong format", and it looks like a model problem right up until you print the
    schema. Two different fixes depending on the ecosystem:

        LangChain @tool     @tool(parse_docstring=True)
        FastMCP             Annotated[str, Field(description=...)]

    Neither is the default. Check, don't assume.
    """
    return {
        t.name: {arg: spec.get("description", "") for arg, spec in arg_schema(t).items()}
        for t in tools
    }


def missing_arg_descriptions(tools: list[Any]) -> list[str]:
    """`tool.arg` strings for every argument the model has no description for."""
    return [
        f"{name}.{arg}"
        for name, args in describe_surface(tools).items()
        for arg, description in args.items()
        if not description.strip()
    ]


if __name__ == "__main__":
    tools = local_tools()
    print(json.dumps(describe_surface(tools), indent=2))
    gaps = missing_arg_descriptions(tools)
    print(f"\narguments with no description: {gaps or 'none'}")
