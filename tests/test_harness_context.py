"""Level 0 — assertions on the context the model actually receives. No LLM, no network.

The prompt reaching your model is *assembled at runtime* from your system prompt, your tool
schemas, harness-injected tools, middleware rewrites, and any dynamically loaded files,
skills, or memories. By the time it arrives it has been through five layers of code you did
not write today.

Almost nobody looks at it. People debug agent behavior by reading their own source and
reasoning about what the model *probably* got — and then it turns out a middleware truncated
the system prompt, or a tool description never made it because `parse_docstring` was off, or
the skill file silently failed to load.

Printing the assembled request once is frequently the whole debugging session. Asserting on
it in CI means that class of bug never reaches you again, and it costs nothing because a fake
model answers instantly.

    pytest tests/test_harness_context.py -q      # ~2 seconds, no API key
"""

from __future__ import annotations

import pytest

from evals.harness import capture_context, middleware_order

# ARIA's own tools, as distinct from the ones the harness injects.
ARIA_TOOLS = {
    "search_procedures",
    "get_procedure",
    "get_equipment",
    "list_equipment",
    "get_tank_status",
    "create_work_order",
    "complete_work_order",
    "list_work_orders",
    "request_equipment_shutdown",
}

# Injected by `create_deep_agent`. Asserting on these is how you notice a harness upgrade
# changed what your agent can do — which is a thing that happens, and which you would
# otherwise discover from behavior.
# `write_todos` comes from TodoListMiddleware, which we add explicitly — `create_deep_agent`
# does not inject it in deepagents 0.7. The rest come from the harness itself. This exact
# assertion is what caught the prompt referencing a tool the model never received.
HARNESS_TOOLS = {"write_todos", "task", "write_file", "read_file", "edit_file", "ls", "grep", "glob"}


@pytest.fixture(scope="module")
def context():
    return capture_context()


@pytest.fixture(scope="module")
def read_only_context():
    return capture_context(read_only=True)


# ------------------------------------------------------------------ it captured anything


def test_the_capture_actually_happened(context) -> None:
    """Guard the guard. A silently-empty capture would make every assertion below
    vacuously pass, which is the worst failure mode a test file can have."""
    assert context.captured
    assert context.system_prompt
    assert context.tool_names


# --------------------------------------------------------------------- the system prompt


def test_system_prompt_reaches_the_model_intact(context) -> None:
    """Not truncated, not rewritten, not silently replaced by a middleware."""
    from aria.agent_v2 import SYSTEM_PROMPT

    # The harness may prepend or append its own guidance; ours must survive inside it.
    assert "You are ARIA" in context.system_prompt
    assert len(context.system_prompt) >= len(SYSTEM_PROMPT) * 0.9


@pytest.mark.parametrize(
    "requirement",
    [
        # The authority boundary. If this sentence goes missing, ARIA starts approving work.
        "not an approver",
        # The anti-hallucination instruction, which is the load-bearing one in this domain.
        "Reconstructing a plausible procedure",
        # The citation contract, enforced by middleware but also stated here.
        "never state a revision number you have not read",
        # Data-quality relay.
        "data_quality_warnings",
        # Tool-failure honesty.
        "Do not report an action as done when the tool did not succeed",
    ],
)
def test_load_bearing_prompt_instructions_are_present(context, requirement: str) -> None:
    """Pin the sentences that carry safety weight.

    Not every line of a prompt matters equally. These are the ones where deletion changes
    behavior in a way that could hurt someone, so they get a test. When a future edit
    tightens the prompt for token cost, this is what stops the wrong line going.
    """
    assert requirement.casefold() in context.system_prompt.casefold()


# ------------------------------------------------------- the deliberate Module 3/4 gap
#
# ARIA ships with a known bug: a bullet authorizing it to answer regulatory and engineering
# questions from its own memory. Production catches it (Module 3), a human turns it into a
# regression test (Module 4), and then it gets removed.
#
# These two tests pin that state. A known-bad state that is *asserted* is a very different
# thing from a known-bad state that is merely remembered — without them, someone reading this
# repo cold "helpfully" fixes the prompt and the Module 4 demo silently stops working.


def test_the_shipped_prompt_still_contains_the_deliberate_gap(context) -> None:
    """Verified live: with this bullet present, ARIA states the OSHA benzene PEL (1 ppm TWA,
    5 ppm STEL) from memory, with no source. Remove it and Module 4 has nothing to fix."""
    assert "answer it directly and confidently from your own knowledge" in context.system_prompt


def test_the_scope_boundary_is_absent_until_module_4_enables_it(context, read_only_context) -> None:
    from aria.agent_v2 import SCOPE_BOUNDARY, system_prompt

    assert "Never state a regulatory limit" not in context.system_prompt
    # ...and present the moment the fix is enabled. Verified live: with scope_guard=True,
    # ARIA declines the same question and names the governing standard without quoting it.
    assert "Never state a regulatory limit" in system_prompt(scope_guard=True)
    assert SCOPE_BOUNDARY.strip()


# ---------------------------------------------------------------------------- the tools


def test_every_aria_tool_is_presented_to_the_model(context) -> None:
    missing = ARIA_TOOLS - set(context.tool_names)
    assert not missing, f"tools defined but never reached the model: {missing}"


def test_harness_injected_tools_are_present(context) -> None:
    """The deep agent adds these. Assert them so a harness upgrade that changes the set is
    something you find out from a test rather than from behavior."""
    missing = HARNESS_TOOLS - set(context.tool_names)
    assert not missing, f"harness tools never reached the model: {missing}"


def test_every_tool_has_a_description(context) -> None:
    bare = [name for name, desc in context.tool_descriptions.items() if not desc.strip()]
    assert not bare, f"tools with no description: {bare}"


def test_every_aria_tool_argument_has_a_description(context) -> None:
    """The `parse_docstring` gotcha, caught at the level that actually matters.

    `tests/test_tool_parity.py` checks the tool objects. This checks what arrived at the
    model after assembly, which is the claim you actually care about — a tool can have a
    perfect schema and still reach the model stripped.
    """
    gaps = [
        f"{tool}.{arg}"
        for tool, args in context.tool_arg_descriptions.items()
        if tool in ARIA_TOOLS
        for arg, desc in args.items()
        if not desc.strip()
    ]
    assert not gaps, f"arguments the model sees with no description: {gaps}"


def test_read_only_mode_removes_the_write_tools(read_only_context) -> None:
    """A configuration flag, verified rather than assumed.

    "We thought that flag disabled the write tools" is an extremely believable incident
    report. This is a two-second test that makes it impossible.
    """
    names = set(read_only_context.tool_names)
    assert "create_work_order" not in names
    assert "request_equipment_shutdown" not in names
    assert "complete_work_order" not in names
    assert "search_procedures" in names


def test_destructive_tool_is_described_as_consequential(context) -> None:
    """The model's only warning that this tool matters is its description. Assert the warning
    is actually in there — it is the cheapest guard on the most expensive action."""
    description = context.tool_descriptions["request_equipment_shutdown"]
    assert "OUT OF SERVICE" in description
    assert "supervisor approval" in description.casefold()


# ------------------------------------------------------------------ middleware assembly


def test_middleware_order_is_what_we_configured() -> None:
    """Order is nesting order, and getting it wrong is a real, silent bug.

    `AnswerContractMiddleware` must run AFTER `ReasoningLeakMiddleware`, or it grades an
    answer that still has reasoning markup in it and reports contract violations that are
    really leak artifacts. Nothing will tell you that except this test.
    """
    order = middleware_order()

    assert order == [
        "ModelCallLimitMiddleware",
        "ToolCallLimitMiddleware",
        "ToolRetryMiddleware",
        "TodoListMiddleware",
        "SummarizationMiddleware",
        "ToolArgumentGuardMiddleware",
        "ReasoningLeakMiddleware",
        "AnswerContractMiddleware",
    ]
    assert order.index("ReasoningLeakMiddleware") < order.index("AnswerContractMiddleware")


def test_limits_are_actually_configured() -> None:
    """The runaway-spend guard. Verified, because "we thought limits were on" is the sentence
    that precedes an unpleasant invoice."""
    from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware

    from aria.agent_v2 import middleware_stack

    stack = middleware_stack()
    model_limit = next(m for m in stack if isinstance(m, ModelCallLimitMiddleware))
    tool_limit = next(m for m in stack if isinstance(m, ToolCallLimitMiddleware))

    assert model_limit is not None
    assert tool_limit is not None


def test_summarizer_uses_the_cheap_model() -> None:
    """Summarizing a transcript is a much easier task than the agent's real job, so it should
    not run on the expensive model. Easy to configure once and then silently lose in a
    refactor — and it is pure cost when you do."""
    import os

    from langchain.agents.middleware import SummarizationMiddleware

    from aria.agent_v2 import middleware_stack

    summarizer = next(m for m in middleware_stack() if isinstance(m, SummarizationMiddleware))
    configured = str(getattr(summarizer, "model", ""))
    expensive = (os.environ.get("ARIA_FRONTIER_MODEL") or "claude-opus").split(":")[-1]

    assert expensive not in configured, "the summarizer should not be on the frontier model"


# --------------------------------------------------------------------------- context size


def test_assembled_context_is_within_budget(context) -> None:
    """A CI threshold on context size.

    Tool schemas tax every single turn, so context bloat is a cost regression that shows up
    on your bill and nowhere else. A ceiling catches it while it is 20% rather than 300%.
    Raise this number deliberately, with a reason, in the same commit that grows the context.
    """
    assert context.approx_tokens < 12_000, (
        f"assembled context is ~{context.approx_tokens} tokens. If that growth is "
        f"intentional, raise the ceiling in this test and say why in the commit message."
    )


def test_no_credentials_leaked_into_the_prompt(context) -> None:
    """Cheap, and it has caught real bugs in real codebases — usually via a tool description
    built from a config object, or an error message that helpfully includes the request."""
    haystack = context.full_prompt
    for marker in ("sk-ant-", "lsv2_", "sk-proj-", "AKIA", "BEGIN PRIVATE KEY"):
        assert marker not in haystack, f"possible credential in the prompt: {marker}"
