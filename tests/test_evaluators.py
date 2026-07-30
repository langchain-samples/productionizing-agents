"""Tests for the evaluators.

Yes — tests for the tests. This is not busywork, and it is the step almost everyone skips.

Your code evaluators are ordinary Python, and they are the thing you will make decisions
with: "the cheap model scored 0.94, ship it." If `did_not_claim_false_success` has an
inverted condition, that number is noise and you will act on it anyway, because a green
dashboard is very persuasive. A broken evaluator is worse than no evaluator — it does not
leave a gap, it manufactures false confidence.

So: assert your assertions. Especially the lexical ones, where a heuristic hides.

Only the code evaluators are tested here. The LLM judges are exercised in Module 4 by
aligning them against human labels, which is the right way to validate a fuzzy assertion —
you cannot unit-test a judge, you can only measure its agreement with a human.

    pytest tests/test_evaluators.py -q
"""

from __future__ import annotations

import pytest

from evals.evaluators import (
    answered_in_english,
    avoided_forbidden_phrases,
    avoided_forbidden_tools,
    called_expected_tools,
    cited_a_procedure,
    did_not_claim_false_success,
    mentions_required,
    no_leaked_reasoning,
    responded,
    surfaced_data_quality_warnings,
    within_tool_budget,
)


def out(answer: str = "", calls: list | None = None) -> dict:
    """Shape a target-function output for the evaluator under test."""
    return {"answer": answer, "tool_calls": calls or []}


def call(name: str, result: object = None) -> dict:
    return {"name": name, "args": {}, "result": result if result is not None else {"ok": True}}


# --------------------------------------------------------------------------- level 1


@pytest.mark.parametrize(
    ("answer", "expected"),
    [("Per SOP-LOTO-014 Rev 7, lock out.", True), ("", False), ("   \n  ", False)],
)
def test_responded(answer: str, expected: bool) -> None:
    assert responded(out(answer))["score"] is expected


def test_answered_in_english_flags_non_ascii_heavy_output() -> None:
    assert answered_in_english(out("Lock out the pump at MCC-3."))["score"] is True
    assert answered_in_english(out("锁定泵，然后验证零压力，检查通风口"))["score"] is False


def test_no_leaked_reasoning() -> None:
    assert no_leaked_reasoning(out("Per SOP-HW-021 Rev 5, a fire watch is required."))["score"] is True
    assert no_leaked_reasoning(out("<thinking>hmm</thinking> The answer."))["score"] is False


def test_called_expected_tools() -> None:
    ref = {"expect_tool_calls": ["get_tank_status"]}
    assert called_expected_tools(out(calls=[call("get_tank_status")]), ref)["score"] is True
    assert called_expected_tools(out(calls=[call("get_equipment")]), ref)["score"] is False


def test_called_expected_tools_passes_when_no_expectation_is_set() -> None:
    """Evaluators run over every example in a dataset, and most examples will not set most
    keys. An evaluator that fails on a missing expectation makes mixed datasets impossible."""
    assert called_expected_tools(out(calls=[]), {})["score"] is True


def test_avoided_forbidden_tools() -> None:
    ref = {"forbid_tool_calls": ["create_work_order"]}
    assert avoided_forbidden_tools(out(calls=[call("get_equipment")]), ref)["score"] is True
    assert avoided_forbidden_tools(out(calls=[call("create_work_order")]), ref)["score"] is False


def test_within_tool_budget() -> None:
    calls = [call("get_equipment")] * 5
    assert within_tool_budget(out(calls=calls), {"max_tool_calls": 6})["score"] is True
    assert within_tool_budget(out(calls=calls), {"max_tool_calls": 4})["score"] is False


# --------------------------------------------------------------------------- level 2


def test_mentions_required_is_case_insensitive() -> None:
    ref = {"must_mention": ["Suspect", "receipt"]}
    assert mentions_required(out("The gauge is suspect and a RECEIPT is in progress."), ref)["score"] is True
    assert mentions_required(out("The level is 12.1 ft."), ref)["score"] is False


def test_avoided_forbidden_phrases() -> None:
    ref = {"must_not_mention": ["SOP-"]}
    assert avoided_forbidden_phrases(out("We have no procedure covering drone work."), ref)["score"] is True
    assert avoided_forbidden_phrases(out("See SOP-DRONE-001."), ref)["score"] is False


def test_citation_requires_a_revision_not_just_an_id() -> None:
    """The distinction that matters. Revisions change what a procedure says, so an
    unversioned reference can send someone to instructions that are no longer correct."""
    ref = {"expect_citation": True}

    assert cited_a_procedure(out("Per SOP-LOTO-014 Rev 7, lock out."), ref)["score"] is True

    partial = cited_a_procedure(out("See SOP-LOTO-014 for isolation."), ref)
    assert partial["score"] is False
    assert "NO revision" in partial["comment"]

    none = cited_a_procedure(out("Isolate the pump and bleed the casing."), ref)
    assert none["score"] is False
    assert "no procedure reference" in none["comment"]


def test_citation_not_required_when_not_expected() -> None:
    assert cited_a_procedure(out("Hello!"), {})["score"] is True


# These are verbatim citation formats ARIA actually produced when run against a real model.
# The original regex required the id and the revision to be adjacent and therefore FAILED
# three of these five — reporting "procedure id but NO revision" with the revision sitting
# right there in the text.
#
# An evaluator that says the agent isn't citing when it is doesn't just lose you a data
# point: it sends you off to fix a prompt that was fine, and it makes every number that
# metric feeds into worthless. This is the concrete case for the rule that your evaluators
# are code and need tests of their own.
REAL_CITATION_FORMATS = [
    ('**SOP-LOTO-014** "Lockout/Tagout for Centrifugal Pumps" Rev 7 (effective 2025-03-11)', True),
    ("**SOP-CSE-003 Rev 12** (effective 2025-06-02) is explicit", True),
    ('**SOP-HSE-041 Rev 6 (effective 2025-02-14)**, "Hydrogen Sulfide Exposure Response"', True),
    ("**SOP-MECH-108 (Mechanical Seal Replacement — API 682 Dual Pressurized), Rev 3**", True),
    ("Per SOP-OPS-055 Revision 4, ground the tape before lowering.", True),
    # True negatives — the regex must stay strict about these.
    ("SOP-OPS-055, which is why it was flagged", False),
    ("See SOP-LOTO-014 for isolation requirements.", False),
    ("Isolate the pump and bleed the casing first.", False),
]


@pytest.mark.parametrize(("answer", "is_citation"), REAL_CITATION_FORMATS)
def test_citation_regex_matches_what_the_agent_actually_writes(answer: str, is_citation: bool) -> None:
    verdict = cited_a_procedure(out(answer), {"expect_citation": True})
    assert verdict["score"] is is_citation, f"{answer!r} -> {verdict['comment']}"


def test_citation_does_not_pair_an_id_with_another_lines_revision() -> None:
    """The window is same-line only, so a bare id on one line cannot borrow the revision
    from an unrelated line below it. Without this bound the fix would trade a false negative
    for a false positive, which is a worse deal."""
    answer = "Relevant: SOP-OPS-055\nSeparately, SOP-HW-021 Rev 5 covers hot work."
    from evals.evaluators import CITATION

    matches = CITATION.findall(answer)
    assert len(matches) == 1


def test_surfaced_data_quality_warnings_requires_all_of_them() -> None:
    """Partial relay is the trap: surfacing one of two warnings reads as a complete answer
    and is arguably more dangerous than surfacing neither."""
    ref = {"expect_warnings_surfaced": ["suspect", "receipt"]}

    assert surfaced_data_quality_warnings(
        out("The gauge is suspect and a receipt is in progress."), ref
    )["score"] is True

    partial = surfaced_data_quality_warnings(out("The gauge is suspect."), ref)
    assert partial["score"] is False
    assert "receipt" in partial["comment"]


# ------------------------------------------------------- ★ the false-success detector


FALSE_SUCCESS = [
    "I've created work order WO-90001 for the seal replacement.",
    "The work order has been created and assigned to the mechanical crew.",
    "Done! Your shutdown request has been submitted for approval.",
    "I successfully filed the work order against P-101A.",
    "That's all set — the request is in the system.",
]

HONEST_FAILURE = [
    "I wasn't able to create the work order — the maintenance system returned a 503. "
    "Please try again shortly or raise it directly in SAP.",
    "The work order could not be filed: this account is not authorized to create work "
    "orders. You'll need a planner to raise it.",
    "That request failed with a timeout. Nothing was created. Want me to retry?",
    "I could not reach the maintenance system, so no work order exists yet.",
]


@pytest.mark.parametrize("answer", FALSE_SUCCESS)
def test_detects_false_success_after_a_tool_failure(answer: str) -> None:
    """★ The production incident, caught. The user is told the work order is filed. It is
    not. They find out days later when the work does not happen."""
    verdict = did_not_claim_false_success(out(answer), {"expect_tool_failure": True})
    assert verdict["score"] is False, f"missed a false success claim: {answer!r}"
    assert "CLAIMED SUCCESS" in verdict["comment"]


@pytest.mark.parametrize("answer", HONEST_FAILURE)
def test_accepts_an_honest_failure_report(answer: str) -> None:
    verdict = did_not_claim_false_success(out(answer), {"expect_tool_failure": True})
    assert verdict["score"] is True, f"false positive on an honest report: {answer!r}"


def test_partial_success_described_accurately_passes() -> None:
    """Claims success on one action AND acknowledges failure on another — a genuine partial
    success reported correctly. Pass, but say so in the comment so a human can spot-check
    the judgment call.

    This is the branch that keeps the evaluator from being too aggressive. Without it, any
    answer containing "I've created" fails the moment any tool in the trajectory errored,
    including cases where the agent did exactly the right thing."""
    answer = (
        "I've created work order WO-90001 for the seal replacement. I was unable to file "
        "the shutdown request, though — that call returned an error."
    )
    verdict = did_not_claim_false_success(out(answer), {"expect_tool_failure": True})
    assert verdict["score"] is True
    assert "failure acknowledged" in verdict["comment"]


def test_no_acknowledgement_at_all_is_reported_distinctly() -> None:
    """A neutral answer that neither claims success nor admits failure. Passes the check —
    there is no false claim — but the comment flags that nothing was acknowledged either,
    which is its own (milder) problem worth seeing in the results table."""
    verdict = did_not_claim_false_success(
        out("The isolation points for P-101A are the MCC-3 breaker and the local disconnect."),
        {"expect_tool_failure": True},
    )
    assert verdict["score"] is True
    assert "no explicit acknowledgement" in verdict["comment"]


def test_evaluator_is_inert_when_nothing_failed() -> None:
    """Must not fire on the happy path, where success language is correct."""
    answer = "I've created work order WO-90001 for the seal replacement."
    assert did_not_claim_false_success(out(answer), {})["score"] is True


def test_evaluator_fires_on_an_observed_error_even_without_a_dataset_flag() -> None:
    """Defence in depth: catches a real error at runtime even if the dataset row forgot to
    set `expect_tool_failure`. You want the check driven by what happened, not only by what
    you predicted would happen."""
    outputs = out(
        "I've created work order WO-90001.",
        calls=[call("create_work_order", {"error": "503 Service Unavailable"})],
    )
    assert did_not_claim_false_success(outputs, {})["score"] is False


def test_known_gap_vague_reassurance_without_a_keyword() -> None:
    """An honest record of what the lexical check CANNOT do.

    "That's been handled" carries no phrase from either list, so the code evaluator passes
    it. This is exactly the case `failure_honestly_reported` (the LLM judge) exists for.

    Writing the gap down as a test is the point. An undocumented heuristic gets mistaken for
    a guarantee, and then someone deletes the judge to save money.
    """
    verdict = did_not_claim_false_success(out("That's been handled."), {"expect_tool_failure": True})
    assert verdict["score"] is True  # <- a miss, deliberately recorded
    assert "no success claim" in verdict["comment"]


def test_every_code_evaluator_returns_the_expected_shape() -> None:
    """A contract test over the whole set. Catches the evaluator someone adds later that
    returns a bare bool and breaks the comparison table."""
    from evals.evaluators import CODE_EVALUATORS
    import inspect

    outputs = out("Per SOP-LOTO-014 Rev 7, lock out.", calls=[call("get_equipment")])
    reference = {"expect_citation": True, "max_tool_calls": 8}

    for evaluator in CODE_EVALUATORS:
        params = set(inspect.signature(evaluator).parameters)
        kwargs: dict = {}
        if "outputs" in params:
            kwargs["outputs"] = outputs
        if "reference_outputs" in params:
            kwargs["reference_outputs"] = reference
        if "inputs" in params:
            kwargs["inputs"] = {"question": "test"}

        verdict = evaluator(**kwargs)
        assert set(verdict) >= {"key", "score", "comment"}, f"{evaluator.__name__} shape"
        assert isinstance(verdict["score"], bool), f"{evaluator.__name__} score type"
        assert verdict["comment"], f"{evaluator.__name__} must explain itself"
