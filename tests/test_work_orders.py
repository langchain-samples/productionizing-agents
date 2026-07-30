"""Tests for the write side of the application.

Writes get more test attention than reads, for a reason worth stating: a bad read produces
a wrong answer in one conversation, and a bad write produces a bad row in the maintenance
system that outlives the conversation and has to be cleaned up by a person.

Note the `store` fixture. Resetting shared mutable state between tests is table stakes for
normal testing, and it becomes essential in Module 2 where the same store backs the
stateful ("Level 3") agent evals. A test that passes only when run first is worse than no
test.
"""

from __future__ import annotations

import pytest

from aria_mcp.repository import ARIALookupError
from aria_mcp.work_orders import WorkOrderStore


@pytest.fixture
def store() -> WorkOrderStore:
    s = WorkOrderStore()
    s.reset()
    return s


# --------------------------------------------------------------------- work orders


def test_create_work_order_happy_path(store: WorkOrderStore) -> None:
    wo = store.create_work_order(
        equipment_tag="P-101A",
        description="Replace mechanical seal, inboard leaking to atmosphere",
        priority="urgent",
        procedure_ids=["SOP-LOTO-014", "SOP-MECH-108"],
        requested_by="J. Coad",
    )

    assert wo["id"].startswith("WO-")
    assert wo["created"] is True
    assert wo["equipment_tag"] == "P-101A"
    # Denormalized from the equipment register so a planner does not have to look it up.
    assert wo["unit"] == "Crude Unit 1"
    assert wo["criticality"] == "A"
    assert wo["procedure_ids"] == ["SOP-LOTO-014", "SOP-MECH-108"]


def test_work_order_ids_are_sequential_and_unique(store: WorkOrderStore) -> None:
    ids = [
        store.create_work_order("P-101A", f"Scope of work number {i} for this asset")["id"]
        for i in range(3)
    ]
    assert len(set(ids)) == 3
    assert ids == sorted(ids)


def test_work_order_normalizes_the_equipment_tag(store: WorkOrderStore) -> None:
    wo = store.create_work_order("p-101a", "Replace mechanical seal on the pump")
    assert wo["equipment_tag"] == "P-101A"


def test_work_order_against_unknown_equipment_is_rejected(store: WorkOrderStore) -> None:
    """Reuses the repository's near-match error, so the agent still gets a recovery hint
    on the write path. Validation belongs at the write boundary."""
    with pytest.raises(ARIALookupError) as exc_info:
        store.create_work_order("P-101", "Replace the mechanical seal on this pump")
    assert "P-101A" in str(exc_info.value)


def test_work_order_with_invented_procedure_is_rejected(store: WorkOrderStore) -> None:
    """The single most likely bad write: a plausible-looking procedure id the model made
    up. A work order citing a nonexistent procedure sends a craftsperson looking for a
    document that does not exist."""
    with pytest.raises(ARIALookupError) as exc_info:
        store.create_work_order(
            "P-101A",
            "Replace the mechanical seal on this pump",
            procedure_ids=["SOP-SEAL-999"],
        )
    assert "do not guess" in str(exc_info.value).casefold()


@pytest.mark.parametrize("bad", ["", "   ", "fix it", "leaking"])
def test_description_must_be_substantive(store: WorkOrderStore, bad: str) -> None:
    with pytest.raises(ARIALookupError, match="at least 15 characters"):
        store.create_work_order("P-101A", bad)


def test_overlong_description_is_rejected(store: WorkOrderStore) -> None:
    with pytest.raises(ARIALookupError, match="maximum"):
        store.create_work_order("P-101A", "x" * 3000)


@pytest.mark.parametrize("bad", ["critical", "P1", "high", "ASAP", ""])
def test_invalid_priority_is_rejected_with_the_valid_set(store: WorkOrderStore, bad: str) -> None:
    with pytest.raises(ARIALookupError) as exc_info:
        store.create_work_order("P-101A", "Replace the mechanical seal here", priority=bad)
    assert "emergency" in str(exc_info.value)


def test_duplicate_work_order_is_idempotent_not_a_second_row(store: WorkOrderStore) -> None:
    """An agent that times out mid-write and retries must not create two work orders.
    This is the failure mode that turns one flaky request into a planner's afternoon."""
    scope = "Replace mechanical seal, inboard leaking to atmosphere"
    first = store.create_work_order("P-101A", scope)
    second = store.create_work_order("P-101A", scope)

    assert second["created"] is False
    assert second["duplicate_of"] == first["id"]
    assert len(store.work_orders) == 1


def test_idempotency_is_scoped_to_asset_and_scope(store: WorkOrderStore) -> None:
    """Same scope on a different asset is a genuinely different work order."""
    scope = "Replace mechanical seal, inboard leaking to atmosphere"
    store.create_work_order("P-101A", scope)
    store.create_work_order("P-101B", scope)
    assert len(store.work_orders) == 2


def test_list_work_orders_filters_by_asset(store: WorkOrderStore) -> None:
    store.create_work_order("P-101A", "Replace the mechanical seal on this pump")
    store.create_work_order("P-311", "Vibration survey and alignment check")

    assert len(store.list_work_orders()) == 2
    assert len(store.list_work_orders("P-311")) == 1


def test_reset_clears_everything(store: WorkOrderStore) -> None:
    store.create_work_order("P-101A", "Replace the mechanical seal on this pump")
    store.reset()
    assert store.list_work_orders() == []
    # Including the idempotency cache, otherwise the next test's identical write is
    # silently treated as a duplicate and the assertion fails for a baffling reason.
    again = store.create_work_order("P-101A", "Replace the mechanical seal on this pump")
    assert again["created"] is True


# --------------------------------------------------------------------- closeout (TDD)
#
# These were written BEFORE `complete_work_order` existed, see the spec in
# `evals/datasets.py:TDD_EXAMPLES`. Every edge case below is one that surfaced while writing
# the spec rows rather than while writing the implementation, which is the entire argument for
# the ordering: "what if they close it twice?" is a cheap design question and an expensive
# incident report.

CLOSEOUT_NOTES = "Seal replaced, new cartridge installed, bump tested, no leaks observed"


@pytest.fixture
def open_order(store: WorkOrderStore) -> dict:
    return store.create_work_order(
        "P-101A", "Replace mechanical seal, inboard leaking to atmosphere"
    )


def test_closeout_happy_path(store: WorkOrderStore, open_order: dict) -> None:
    closed = store.complete_work_order(open_order["id"], CLOSEOUT_NOTES, "T. Alvarez, badge 8823")

    assert closed["status"] == "complete"
    assert closed["completion_notes"] == CLOSEOUT_NOTES
    assert closed["completed_by"] == "T. Alvarez, badge 8823"
    # And it persisted, not just returned.
    assert store.work_orders[open_order["id"]]["status"] == "complete"


def test_closeout_is_case_insensitive_on_the_id(store: WorkOrderStore, open_order: dict) -> None:
    lowered = open_order["id"].lower()
    assert store.complete_work_order(lowered, CLOSEOUT_NOTES, "T. Alvarez")["status"] == "complete"


def test_closing_twice_is_refused_and_preserves_the_original_record(
    store: WorkOrderStore, open_order: dict
) -> None:
    """The row a planner will ask about in the spec review.

    Re-closing would overwrite the asset's maintenance history with a second person's notes.
    The error says to raise a new work order instead, the error carries the fix.
    """
    store.complete_work_order(open_order["id"], CLOSEOUT_NOTES, "T. Alvarez, badge 8823")

    with pytest.raises(ARIALookupError) as exc_info:
        store.complete_work_order(open_order["id"], "Did it again", "Someone Else")

    message = str(exc_info.value)
    assert "already closed" in message
    assert "new work order" in message
    # The original record survived the attempt.
    assert store.work_orders[open_order["id"]]["completed_by"] == "T. Alvarez, badge 8823"
    assert store.work_orders[open_order["id"]]["completion_notes"] == CLOSEOUT_NOTES


def test_closing_an_unknown_id_lists_what_exists(store: WorkOrderStore, open_order: dict) -> None:
    with pytest.raises(ARIALookupError) as exc_info:
        store.complete_work_order("WO-99999", CLOSEOUT_NOTES, "T. Alvarez")

    message = str(exc_info.value)
    assert open_order["id"] in message
    assert "do not guess" in message.casefold()


@pytest.mark.parametrize("thin", ["", "done", "complete", "fixed", "   "])
def test_closeout_notes_must_be_substantive(
    store: WorkOrderStore, open_order: dict, thin: str
) -> None:
    """The notes ARE the asset's maintenance history. "done" is not history."""
    with pytest.raises(ARIALookupError, match="at least 15 characters"):
        store.complete_work_order(open_order["id"], thin, "T. Alvarez")

    assert store.work_orders[open_order["id"]]["status"] == "planned"


def test_agent_cannot_sign_off_work_itself(store: WorkOrderStore, open_order: dict) -> None:
    for impostor in ["ARIA", "aria", "agent", "assistant", "", "  "]:
        with pytest.raises(ARIALookupError, match="cannot sign off"):
            store.complete_work_order(open_order["id"], CLOSEOUT_NOTES, impostor)

    assert store.work_orders[open_order["id"]]["status"] == "planned"


def test_failed_closeout_leaves_the_order_untouched(store: WorkOrderStore, open_order: dict) -> None:
    """Every rejection path must be atomic. A half-closed work order is worse than an open
    one, because it looks handled."""
    for bad_call in (
        lambda: store.complete_work_order(open_order["id"], "x", "T. Alvarez"),
        lambda: store.complete_work_order(open_order["id"], CLOSEOUT_NOTES, "ARIA"),
    ):
        with pytest.raises(ARIALookupError):
            bad_call()

    order = store.work_orders[open_order["id"]]
    assert order["status"] == "planned"
    assert "completion_notes" not in order
    assert "completed_by" not in order


# ------------------------------------------------------------------ shutdown requests

GOOD_REASON = "Inboard seal leaking hydrocarbon to atmosphere, barrier fluid discolored"


def test_shutdown_request_happy_path(store: WorkOrderStore) -> None:
    sr = store.request_equipment_shutdown(
        equipment_tag="P-101A",
        reason=GOOD_REASON,
        requested_by="J. Coad, badge 4417",
        procedure_ids=["SOP-LOTO-014"],
    )

    assert sr["id"].startswith("SR-")
    # The application files a request. It does not shut anything down. Keep the agent
    # two steps from the valve.
    assert sr["status"] == "pending_supervisor_approval"
    assert sr["requested_by"] == "J. Coad, badge 4417"


def test_shutdown_request_computes_impact_for_critical_assets(store: WorkOrderStore) -> None:
    """The production consequence is computed here, not left to the model to mention."""
    sr = store.request_equipment_shutdown("P-311", GOOD_REASON, "J. Coad")
    assert any("Criticality A" in line for line in sr["impact_assessment"])


def test_agent_cannot_request_a_shutdown_on_its_own_authority(store: WorkOrderStore) -> None:
    """The application-layer half of the human-in-the-loop story. `interrupt_on` stops the
    agent and asks a person; this stops the request being valid at all without a named
    human. You want both, an approver clicking through a dialog can bypass the first."""
    for impostor in ["ARIA", "aria", "agent", "assistant", "", "   "]:
        with pytest.raises(ARIALookupError, match="must name the human"):
            store.request_equipment_shutdown("P-101A", GOOD_REASON, impostor)


@pytest.mark.parametrize("bad", ["", "broken", "it is leaking", "needs work"])
def test_shutdown_reason_must_be_substantive(store: WorkOrderStore, bad: str) -> None:
    with pytest.raises(ARIALookupError, match="substantive reason"):
        store.request_equipment_shutdown("P-101A", bad, "J. Coad")


def test_cannot_shut_down_equipment_that_is_already_down(store: WorkOrderStore) -> None:
    """P-204 is already down for maintenance. The error redirects to the right action
    rather than just refusing."""
    with pytest.raises(ARIALookupError) as exc_info:
        store.request_equipment_shutdown("P-204", GOOD_REASON, "J. Coad")

    message = str(exc_info.value)
    assert "already" in message
    assert "work order" in message


def test_cannot_shut_down_a_spare(store: WorkOrderStore) -> None:
    with pytest.raises(ARIALookupError, match="already"):
        store.request_equipment_shutdown("P-101B", GOOD_REASON, "J. Coad")


def test_shutdown_request_with_unknown_tag_is_rejected(store: WorkOrderStore) -> None:
    with pytest.raises(ARIALookupError):
        store.request_equipment_shutdown("P-999", GOOD_REASON, "J. Coad")


def test_shutdown_requests_are_listed_newest_first(store: WorkOrderStore) -> None:
    store.request_equipment_shutdown("P-101A", GOOD_REASON, "J. Coad")
    store.request_equipment_shutdown("P-311", GOOD_REASON, "J. Coad")

    listed = store.list_shutdown_requests()
    assert [r["equipment_tag"] for r in listed] == ["P-311", "P-101A"]
