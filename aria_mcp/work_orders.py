"""Write-side of the ARIA application: work orders and shutdown requests.

Everything in `repository.py` is read-only. This module is where the agent can *change the
world*, which makes it a different kind of thing and worth keeping separate:

* Reads can be retried freely. Writes cannot. A retried `create_work_order` is a duplicate
  work order that a planner has to go clean up.
* Writes are what you gate with human-in-the-loop.
* Writes are what makes stateful evaluation possible in Module 2, you can
  assert the world actually changed, not just that the agent said the right words.

The store is deliberately in-memory and resettable. In production this is your maintenance
system's API; the method signatures would not change, and neither would your tests.

ON IDEMPOTENCY, briefly, because it bites everyone: an agent that times out mid-write and
retries will create two work orders unless you give it a way not to. Real systems want a
client-supplied idempotency key. We use a content hash of (tag, description) within a short
window, which is enough to demonstrate the idea and to stop the duplicate-work-order
failure mode from showing up in the workshop.
"""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from aria_mcp.repository import ARIALookupError, get_repository

Priority = Literal["emergency", "urgent", "routine", "shutdown"]

VALID_PRIORITIES: Final[tuple[str, ...]] = ("emergency", "urgent", "routine", "shutdown")
MAX_DESCRIPTION_CHARS: Final[int] = 2000
MIN_DESCRIPTION_CHARS: Final[int] = 15
MIN_REASON_CHARS: Final[int] = 20


@dataclass(slots=True)
class WorkOrderStore:
    """Mutable write-side state. One instance per process; `reset()` between tests."""

    work_orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    shutdown_requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    _wo_counter: itertools.count = field(default_factory=lambda: itertools.count(90000))
    _sr_counter: itertools.count = field(default_factory=lambda: itertools.count(500))
    _idempotency: dict[str, str] = field(default_factory=dict)

    def reset(self) -> None:
        """Clear all state. Module 2's stateful tests call this in a fixture."""
        self.work_orders.clear()
        self.shutdown_requests.clear()
        self._idempotency.clear()
        self._wo_counter = itertools.count(90000)
        self._sr_counter = itertools.count(500)

    # ------------------------------------------------------------------ work orders

    def create_work_order(
        self,
        equipment_tag: str,
        description: str,
        priority: str = "routine",
        procedure_ids: list[str] | None = None,
        requested_by: str = "ARIA",
    ) -> dict[str, Any]:
        """Create a maintenance work order against a piece of equipment.

        Validates against the equipment register and the procedure library before writing,
        so a work order can never reference an asset or a procedure that does not exist.
        Validate at the write boundary: a bad row in the maintenance system outlives the
        conversation that created it.

        Raises:
            ARIALookupError: unknown equipment tag, unknown procedure id, bad priority, or
                a description too short to be actionable. Every message names the fix.
        """
        repo = get_repository()

        # Raises with near-match suggestions if the tag is wrong.
        equipment = repo.get_equipment(equipment_tag)

        description = (description or "").strip()
        if len(description) < MIN_DESCRIPTION_CHARS:
            raise ARIALookupError(
                f"Work order description must be at least {MIN_DESCRIPTION_CHARS} "
                f"characters and describe the scope of work; got {description!r}. A "
                f"planner needs to know what to schedule and what craft to assign."
            )
        if len(description) > MAX_DESCRIPTION_CHARS:
            raise ARIALookupError(
                f"Work order description is {len(description)} characters; the maximum is "
                f"{MAX_DESCRIPTION_CHARS}. Summarize the scope and reference the procedure "
                f"ids for the detail."
            )

        if priority not in VALID_PRIORITIES:
            raise ARIALookupError(
                f"{priority!r} is not a valid priority. Use one of: "
                f"{', '.join(VALID_PRIORITIES)}. 'emergency' is for an active safety "
                f"hazard; 'shutdown' means the work is deferred to the next turnaround."
            )

        resolved: list[str] = []
        for proc_id in procedure_ids or []:
            # Raises listing every valid id if this one is invented.
            resolved.append(repo.get_procedure(proc_id)["id"])

        # Idempotency: same asset + same scope is treated as the same request.
        key = hashlib.sha256(
            f"{equipment['tag']}|{description.casefold()}".encode()
        ).hexdigest()[:16]
        if key in self._idempotency:
            existing = self.work_orders[self._idempotency[key]]
            return {**existing, "duplicate_of": existing["id"], "created": False}

        wo_id = f"WO-{next(self._wo_counter)}"
        record = {
            "id": wo_id,
            "equipment_tag": equipment["tag"],
            "equipment_description": equipment["description"],
            "unit": equipment["unit"],
            "criticality": equipment["criticality"],
            "description": description,
            "priority": priority,
            "procedure_ids": resolved,
            "requested_by": requested_by,
            "status": "planned",
        }
        self.work_orders[wo_id] = record
        self._idempotency[key] = wo_id
        return {**record, "created": True}

    def complete_work_order(
        self,
        work_order_id: str,
        completion_notes: str,
        completed_by: str,
    ) -> dict[str, Any]:
        """Close out a work order as complete.

        Added test-first in Module 2: the spec (`evals/datasets.py:TDD_EXAMPLES`) was
        written before this method existed, run to watch it fail, and only then implemented.
        The edge cases below are here because writing the spec surfaced them, which is the
        argument for the order of operations: designing the test made us ask "what if they
        close it twice?" at design time rather than in an incident.

        Raises:
            ARIALookupError: unknown id, already complete, or inadequate notes.
        """
        wanted = (work_order_id or "").strip().upper()
        order = self.work_orders.get(wanted)

        if order is None:
            known = ", ".join(sorted(self.work_orders)) or "none in this session"
            raise ARIALookupError(
                f"No work order with id {work_order_id!r}. Known work orders: {known}. "
                f"Use list_work_orders to find the right id; do not guess."
            )

        if order["status"] == "complete":
            raise ARIALookupError(
                f"{wanted} is already closed out (completed by "
                f"{order.get('completed_by')!r}). Re-closing a work order would overwrite "
                f"the original completion record. If the work was redone, raise a new work "
                f"order instead."
            )

        notes = (completion_notes or "").strip()
        if len(notes) < MIN_DESCRIPTION_CHARS:
            raise ARIALookupError(
                f"Completion notes must be at least {MIN_DESCRIPTION_CHARS} characters "
                f"describing what was actually done; got {notes!r}. These notes are the "
                f"asset's maintenance history: the next person to work on this equipment "
                f"reads them."
            )

        completed_by = (completed_by or "").strip()
        if not completed_by or completed_by.casefold() in {"aria", "agent", "assistant"}:
            raise ARIALookupError(
                "Closing out a work order must record the human who did the work. ARIA "
                "cannot sign off work. Ask the user for their name or badge number."
            )

        order.update(
            {
                "status": "complete",
                "completion_notes": notes,
                "completed_by": completed_by,
            }
        )
        return dict(order)

    def list_work_orders(self, equipment_tag: str | None = None) -> list[dict[str, Any]]:
        """List work orders created in this session, newest first."""
        orders = list(self.work_orders.values())
        if equipment_tag:
            wanted = equipment_tag.strip().upper()
            orders = [o for o in orders if o["equipment_tag"] == wanted]
        return sorted(orders, key=lambda o: o["id"], reverse=True)

    # ------------------------------------------------------------ shutdown requests

    def request_equipment_shutdown(
        self,
        equipment_tag: str,
        reason: str,
        requested_by: str,
        procedure_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Request that a piece of equipment be taken out of service.

        THIS IS THE CONSEQUENTIAL ONE. Taking a crude charge pump out of service is a
        production decision with real money attached, and on a criticality-A asset with no
        running spare it can force a unit rate cut. The agent must never do this without a
        human in the loop, see `interrupt_on` in `aria/agent_v2.py`.

        Two layers of protection, and it is worth being explicit that they are different:

        * `interrupt_on` (agent layer) stops the *agent* and asks a person. It protects
          against the agent deciding to do something it should not.
        * The validation below (application layer) rejects requests that are invalid
          regardless of who asked. It protects against the request being nonsense even
          when a human approved it.

        You want both. The agent-layer gate can be bypassed by a tired approver clicking
        yes; the application-layer check cannot.

        Raises:
            ARIALookupError: unknown tag, equipment not currently running, missing or
                inadequate justification, or no named requester.
        """
        repo = get_repository()
        equipment = repo.get_equipment(equipment_tag)

        status = equipment.get("status")
        if status not in {"running", "in_service"}:
            raise ARIALookupError(
                f"{equipment['tag']} is already {status!r}; a shutdown request only "
                f"applies to equipment that is running or in service. If you meant to "
                f"schedule maintenance on equipment that is already down, create a work "
                f"order instead."
            )

        reason = (reason or "").strip()
        if len(reason) < MIN_REASON_CHARS:
            raise ARIALookupError(
                f"A shutdown request needs a substantive reason of at least "
                f"{MIN_REASON_CHARS} characters; got {reason!r}. State the observed "
                f"condition and why continued operation is unacceptable, this text goes "
                f"in front of the Unit Supervisor who approves or rejects it."
            )

        requested_by = (requested_by or "").strip()
        if not requested_by or requested_by.casefold() in {"aria", "agent", "assistant"}:
            raise ARIALookupError(
                "A shutdown request must name the human requesting it. ARIA cannot "
                "request a shutdown on its own authority. Ask the user for their name or "
                "badge number and pass it as requested_by."
            )

        resolved = [repo.get_procedure(p)["id"] for p in procedure_ids or []]

        # Criticality-A assets with no running spare get flagged for the approver. Compute
        # the consequence here rather than hoping the model mentions it.
        spare_running = any(
            item["tag"] != equipment["tag"]
            and item["description"].rsplit(" ", 1)[0] == equipment["description"].rsplit(" ", 1)[0]
            and item["status"] in {"running", "spare"}
            for item in repo.equipment
        )
        impact: list[str] = []
        if equipment["criticality"] == "A":
            impact.append(
                "Criticality A asset, loss of service has a direct production or safety "
                "consequence."
            )
        if not spare_running:
            impact.append(
                "No running or available spare identified. Expect a unit rate reduction."
            )

        sr_id = f"SR-{next(self._sr_counter)}"
        record = {
            "id": sr_id,
            "equipment_tag": equipment["tag"],
            "equipment_description": equipment["description"],
            "unit": equipment["unit"],
            "criticality": equipment["criticality"],
            "current_status": status,
            "reason": reason,
            "requested_by": requested_by,
            "procedure_ids": resolved,
            "impact_assessment": impact,
            # Note the status. The application does not shut anything down; it files a
            # request that a human approves. Keep the agent two steps from the valve.
            "status": "pending_supervisor_approval",
        }
        self.shutdown_requests[sr_id] = record
        return record

    def list_shutdown_requests(self) -> list[dict[str, Any]]:
        return sorted(self.shutdown_requests.values(), key=lambda r: r["id"], reverse=True)


#: Process-wide store. Module 2's stateful tests reset this in a fixture.
STORE: Final[WorkOrderStore] = WorkOrderStore()


def get_store() -> WorkOrderStore:
    return STORE
