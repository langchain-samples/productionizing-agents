"""Tests for ARIA's custom middleware.

Note what makes these tests cheap: middleware hooks are plain functions over state. You can
call `after_model({"messages": [...]}, runtime)` directly with a hand-built message and
assert on exactly what comes back — no model, no API key, no network, no flake.

That is the strongest practical argument for moving a rule out of the prompt and into a
hook. "Always cite the procedure revision" in a prompt can only be verified statistically,
by running the model many times and measuring. The same rule as middleware is verified by
`assert`. You still want the statistical measurement (that is Module 2), but you want it
for the things that genuinely need judgment, not for things a regex settles.
"""

from __future__ import annotations

import pytest
from langchain.messages import AIMessage, HumanMessage, ToolMessage
from langchain.tools.tool_node import ToolCallRequest

from aria.middleware import (
    AnswerContractMiddleware,
    ReasoningLeakMiddleware,
    ToolArgumentGuardMiddleware,
    _text_of,
)

RUNTIME = None  # these hooks never touch the runtime; None keeps the tests honest about that


def state(*messages) -> dict:
    return {"messages": list(messages)}


# ------------------------------------------------------------------ reasoning leak


@pytest.fixture
def leak() -> ReasoningLeakMiddleware:
    return ReasoningLeakMiddleware()


def test_delimited_reasoning_block_is_removed(leak: ReasoningLeakMiddleware) -> None:
    message = AIMessage(
        content=(
            "<thinking>The user wants LOTO for P-101A. Let me search.</thinking>\n\n"
            "Per SOP-LOTO-014 Rev 7, obtain a signed work permit first."
        ),
        id="msg-1",
    )
    update = leak.after_model(state(HumanMessage("x"), message), RUNTIME)

    assert update is not None
    cleaned = _text_of(update["messages"][0])
    assert "thinking" not in cleaned
    assert "The user wants" not in cleaned
    assert cleaned.startswith("Per SOP-LOTO-014 Rev 7")


def test_corrected_message_keeps_its_id(leak: ReasoningLeakMiddleware) -> None:
    """The id is what makes LangGraph replace the message instead of appending a second
    one. Lose it and the user sees the leak *and* the answer."""
    message = AIMessage(content="<thinking>hmm</thinking>Answer.", id="msg-42")
    update = leak.after_model(state(message), RUNTIME)

    assert update["messages"][0].id == "msg-42"


def test_unclosed_reasoning_tag_truncates_the_remainder(leak: ReasoningLeakMiddleware) -> None:
    message = AIMessage(content="Real answer here.\n<thinking>and then I rambled forever", id="m")
    update = leak.after_model(state(message), RUNTIME)

    assert _text_of(update["messages"][0]) == "Real answer here."


def test_message_that_is_entirely_reasoning_gets_a_fallback(leak: ReasoningLeakMiddleware) -> None:
    """An empty bubble is invisible in your metrics and infuriating to the user."""
    message = AIMessage(content="<thinking>all of it was reasoning</thinking>", id="m")
    update = leak.after_model(state(message), RUNTIME)

    text = _text_of(update["messages"][0])
    assert text
    assert "Unit Supervisor" in text


def test_clean_answer_is_left_alone(leak: ReasoningLeakMiddleware) -> None:
    message = AIMessage(content="Per SOP-HW-021 Rev 5, a fire watch is required.", id="m")
    assert leak.after_model(state(message), RUNTIME) is None


def test_prose_leak_is_flagged_but_not_deleted(leak: ReasoningLeakMiddleware) -> None:
    """We flag prose, we never cut it. A regex confident enough to delete prose is
    confident enough to delete a real answer."""
    message = AIMessage(content="Okay, so the user is asking about P-101A. The answer is X.", id="m")
    assert leak.after_model(state(message), RUNTIME) is None


def test_message_with_pending_tool_calls_is_not_touched(leak: ReasoningLeakMiddleware) -> None:
    """Mid-trajectory messages are not the final answer. Filtering them would corrupt the
    tool-calling loop."""
    message = AIMessage(
        content="<thinking>searching</thinking>",
        tool_calls=[{"name": "search_procedures", "args": {"query": "loto"}, "id": "c1"}],
        id="m",
    )
    assert leak.after_model(state(message), RUNTIME) is None


def test_structured_content_blocks_are_handled(leak: ReasoningLeakMiddleware) -> None:
    """Providers differ on whether content is a string or a list of blocks. Both paths
    have to work or this middleware silently no-ops on half your models."""
    message = AIMessage(
        content=[{"type": "text", "text": "<thinking>x</thinking>Clean answer."}],
        id="m",
    )
    update = leak.after_model(state(message), RUNTIME)
    assert _text_of(update["messages"][0]) == "Clean answer."


# ---------------------------------------------------------------- tool argument guard


@pytest.fixture
def guard() -> ToolArgumentGuardMiddleware:
    return ToolArgumentGuardMiddleware()


def call(name: str, args: dict) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "args": args, "id": "call-1"},
        tool=None,
        state={"messages": []},
        runtime=None,
    )


def test_lowercase_tag_is_normalized(guard: ToolArgumentGuardMiddleware) -> None:
    seen: dict = {}

    def handler(request: ToolCallRequest) -> ToolMessage:
        seen.update(request.tool_call["args"])
        return ToolMessage(content="ok", tool_call_id="call-1")

    guard.wrap_tool_call(call("get_equipment", {"tag": " p-101a "}), handler)
    assert seen["tag"] == "P-101A"


def test_tank_tag_sent_to_get_equipment_is_redirected(guard: ToolArgumentGuardMiddleware) -> None:
    """We return a correction rather than silently running a different tool. Silently
    swapping the tool would make the trace lie about what happened."""
    called = False

    def handler(request: ToolCallRequest) -> ToolMessage:
        nonlocal called
        called = True
        return ToolMessage(content="ok", tool_call_id="call-1")

    result = guard.wrap_tool_call(call("get_equipment", {"tag": "T-042"}), handler)

    assert called is False
    assert "get_tank_status" in result.content


def test_equipment_tag_sent_to_get_tank_status_is_redirected(guard: ToolArgumentGuardMiddleware) -> None:
    result = guard.wrap_tool_call(
        call("get_tank_status", {"tag": "P-101A"}),
        lambda r: ToolMessage(content="ok", tool_call_id="call-1"),
    )
    assert "get_equipment" in result.content


def test_tank_prefix_is_rewritten(guard: ToolArgumentGuardMiddleware) -> None:
    """'tank 42' is what a person says out loud, so it is what a model repeats."""
    result = guard.wrap_tool_call(
        call("get_tank_status", {"tag": "tank 042"}),
        lambda r: ToolMessage(content=r.tool_call["args"]["tag"], tool_call_id="call-1"),
    )
    assert result.content == "T-042"


@pytest.mark.parametrize(
    ("given", "expected"),
    [(20, 5), (0, 1), (-3, 1), ("7", 5), (3, 3), ("nonsense", 5)],
)
def test_search_limit_is_clamped(guard: ToolArgumentGuardMiddleware, given, expected) -> None:
    def handler(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(content=str(request.tool_call["args"]["limit"]), tool_call_id="call-1")

    result = guard.wrap_tool_call(call("search_procedures", {"limit": given, "query": "x"}), handler)
    assert result.content == str(expected)


def test_valid_call_passes_through_untouched(guard: ToolArgumentGuardMiddleware) -> None:
    result = guard.wrap_tool_call(
        call("get_equipment", {"tag": "P-101A"}),
        lambda r: ToolMessage(content="passed", tool_call_id="call-1"),
    )
    assert result.content == "passed"


# ---------------------------------------------------------------- answer contract


@pytest.fixture
def contract() -> AnswerContractMiddleware:
    return AnswerContractMiddleware()


def tool_result(name: str) -> ToolMessage:
    return ToolMessage(content="{}", tool_call_id="c", name=name)


def test_contract_middleware_never_rewrites_the_answer(contract: AnswerContractMiddleware) -> None:
    """The whole design decision, asserted. We cannot invent a citation, so we must not
    try — a fabricated source is worse than a missing one."""
    message = AIMessage(content="Do the thing.", id="m")
    update = contract.after_model(state(tool_result("search_procedures"), message), RUNTIME)
    assert update is None


def test_cited_answer_is_recognized(contract: AnswerContractMiddleware) -> None:
    message = AIMessage(content="Per SOP-LOTO-014 Rev 7 (effective 2025-03-11), lock out.", id="m")
    assert contract.after_model(state(tool_result("get_procedure"), message), RUNTIME) is None


def test_procedure_id_without_a_revision_is_not_a_citation(contract: AnswerContractMiddleware) -> None:
    """'See SOP-LOTO-014' is not good enough. Revisions change what the procedure says —
    SOP-CSE-003 Rev 12 tightened blinding requirements over Rev 11 — so an unversioned
    reference can send someone to instructions that are no longer correct."""
    from aria.middleware import CITATION, PROCEDURE_ID

    text = "See SOP-LOTO-014 for isolation."
    assert PROCEDURE_ID.search(text)
    assert not CITATION.search(text)


def test_message_with_pending_tool_calls_is_skipped(contract: AnswerContractMiddleware) -> None:
    message = AIMessage(
        content="",
        tool_calls=[{"name": "search_procedures", "args": {}, "id": "c1"}],
        id="m",
    )
    assert contract.after_model(state(message), RUNTIME) is None


def test_middleware_is_safe_with_no_messages(
    leak: ReasoningLeakMiddleware, contract: AnswerContractMiddleware
) -> None:
    assert leak.after_model(state(), RUNTIME) is None
    assert contract.after_model(state(), RUNTIME) is None


def test_annotate_run_is_a_no_op_without_tracing() -> None:
    """Observability code must never be the reason the agent falls over. This asserts the
    swallow-everything behavior is real, because a test that only passes with tracing
    configured is a test that will fail in someone's CI."""
    from aria.middleware import _annotate_run

    _annotate_run(anything="at all", some_count=1)  # must not raise
