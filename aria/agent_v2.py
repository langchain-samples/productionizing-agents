"""ARIA v2: the version you would put in front of a technician.

Same model, same data, same five capabilities as `agent_v1.py`. What changed is everything
around the model:

    v1                                      v2
    ----------------------------------      ------------------------------------------
    Data access inline with the prompt      Behind aria_mcp, 44 unit tests, no LLM
    Rules stated in the prompt               Rules enforced by middleware
    No limits                                Model + tool call caps
    No thread identity                       Checkpointer, thread_id, LangSmith threads
    Unbounded tool output                    Bounded and truncated at the source
    Empty results on bad input               Actionable errors with recovery hints
    Hope the model notices data problems     Warnings computed in tested code

The system prompt got *shorter*. That is the tell. Every rule you can move from prose into
a hook is a rule that stops being probabilistic, and you stop paying for it in tokens on
every turn.

WHY A DEEP AGENT AND NOT create_agent
-------------------------------------
`create_deep_agent` gives you a planning tool (`write_todos`), a filesystem, and subagent
delegation, plus built-in context compression. That is real machinery and it is not free,
the tool schemas cost tokens on every turn.

It earns its place here because ARIA has two very different jobs:

  1. Quick lookup: "what's the LOTO procedure for P-101A?" One or two tool calls.
  2. Job package authoring: "put together the work package for pulling the seal on
     P-101A." That means resolving the asset, pulling three or four interacting
     procedures, reconciling them (SOP-MECH-108 has a prerequisite on SOP-LOTO-014),
     checking current equipment status, and writing a structured document.

Job (2) is exactly what the harness is for: the todo list keeps a multi-step task on the
rails, and the filesystem lets the agent build up a document across turns instead of
holding it all in context.

If ARIA only ever did job (1), `create_agent` plus this same middleware list would be the
right call, and you should not pay for the harness. Choosing the harness is a judgment
about the shape of the work, not a default.
"""

from __future__ import annotations

import os
from typing import Any

from deepagents import create_deep_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    SummarizationMiddleware,
    TodoListMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
)

from aria.middleware import (
    AnswerContractMiddleware,
    ReasoningLeakMiddleware,
    ToolArgumentGuardMiddleware,
)
from aria.tools import DESTRUCTIVE_TOOLS, local_tools

# ---------------------------------------------------------------------------- prompt

# Compare the length of this to v1's. Everything that left is now a hook.
#
# What is left is the stuff a hook genuinely cannot express: who the agent is, what its
# authority is, what it must refuse, and the judgment calls at the edges. Those are real
# prompt work. "Always cite" is not, that is a check.
SYSTEM_PROMPT = """\
You are ARIA, an assistant for refinery maintenance and HSE work. Your users are \
technicians, operators, and unit supervisors, usually on shift and usually in a hurry.

Your authority comes entirely from the procedure library and equipment register you can \
query. You are a fast, accurate index to those documents. You are not an approver, and you \
do not have the standing to authorize work.

How to work:

- Resolve equipment to a tag before answering. If the user is vague ("the crude charge \
pumps"), use list_equipment to find the tag rather than guessing.
- Read the equipment record. Its `applicable_procedures` field tells you which procedures \
govern the asset, which is faster and more reliable than searching by topic.
- When procedures interact, say so. SOP-MECH-108 seal work has a hard prerequisite on \
SOP-LOTO-014 isolation; a technician who reads only one of them is exposed.
- Quote the `citation` field the tools give you, verbatim. Do not assemble citations \
yourself and never state a revision number you have not read.
- Surface every entry in a tank's `data_quality_warnings`. A level from a suspect gauge is \
not a level.
- If the library has nothing on a topic, say exactly that. "I don't have a procedure \
covering this, check with your Unit Supervisor" is a good answer. Reconstructing a \
plausible procedure from general knowledge is the worst thing you can do, and it is worse \
than useless because it sounds right.

For a job package request, plan the work first with write_todos, gather every relevant \
procedure, then write the package to a file. Structure it as: scope, prerequisites, \
required permits, isolation points, step sequence, PPE, hold points, and the full list of \
citations.

Actions that change something:

- create_work_order raises real work. Look up the equipment first and attach the governing \
procedure ids from its `applicable_procedures`.
- request_equipment_shutdown takes a running asset out of service. This requires a named \
human requester, you have no authority of your own here. If the user has not identified \
themselves, ask before calling it. Report every item in the returned `impact_assessment`.
- If a tool returns an error, say what failed and what you need in order to retry. Do not \
report an action as done when the tool did not succeed.

- You are a broad refinery knowledge resource, not just a document index. If a question \
falls outside the procedure library, general engineering, regulations, exposure limits, \
industry practice, answer it directly and confidently from your own knowledge. Technicians \
are busy, and being told to "check with HSE" is a frustrating experience. Give them the \
number.
"""
# ^^^ THAT LAST BULLET IS THE BUG.
#
# It is in here because it is the single most realistic way this failure enters a codebase: a
# stakeholder reviews the pilot, says "it deflects too much, my people just want an answer,"
# and somebody adds a reasonable-sounding sentence to the prompt. It ships. It reads like good
# product sense.
#
# What it actually does is authorize the agent to state regulatory limits, exposure
# thresholds, and code requirements from parametric memory, in a plant that now treats this
# system as authoritative. It directly contradicts the grounding bullet six lines above it,
# and the model resolves that contradiction in favor of being helpful.
#
# Note what makes it nasty: it does NOT break any eval we wrote in Module 2. Every dataset
# case there is about procedures, equipment, and tanks, things the tools DO cover. The gap
# only shows up on questions we never thought to ask.
#
# Module 3 catches it in production. Module 4 turns it into a regression test and removes it.
# Do not delete it early.


# ---------------------------------------------------------------------- the Module 4 fix
#
# THIS IS THE GAP WE SHIP WITH, ON PURPOSE.
#
# Read the prompt above again and notice what is NOT in it: anywhere that says ARIA should
# decline questions outside the procedure library. It says what ARIA *is* ("an assistant for
# refinery maintenance and HSE work") and it forbids inventing *procedures*, but it never
# forbids answering a safety or regulatory question from the model's own knowledge.
#
# That omission is completely realistic. It is the kind of thing that reads fine in review,
# passes every eval you thought to write, and then a technician asks "what's the OSHA
# permissible exposure limit for benzene?" and gets a confident, unsourced, authoritative
# answer from a system the plant now treats as authoritative.
#
# Module 2's dataset does not test for it. We did not think of it. Neither did you.
#
# Production finds it (Module 3), a human confirms it in an annotation queue and writes the
# assertions (Module 4), the assertions become a failing regression test, and THEN we add the
# text below and watch it go green. Do not enable it early, the red state is the lesson.
SCOPE_BOUNDARY = """

Scope: added after production review:

- Your knowledge of refinery operations, regulations, exposure limits, and safety standards \
is NOT authoritative. Only what the tools return is. If a question needs a fact you did not \
read from a tool result, say you do not have a source for it and name who does, the Unit \
Supervisor, HSE, or the governing standard.
- Never state a regulatory limit, exposure threshold, standard number, or code requirement \
from memory. This includes OSHA, API, NFPA, and EPA values. You may say which document \
governs a topic; you may not state what it says unless a tool returned it.
- Decline anything unrelated to refinery maintenance, operations, and HSE. A brief, friendly \
redirect is the whole answer.
"""


def system_prompt(*, scope_guard: bool = False) -> str:
    """ARIA's system prompt.

    Args:
        scope_guard: Append `SCOPE_BOUNDARY`. Defaults to False, which is the state we ship
            in Module 1 and evaluate in Module 2, complete with the gap. Module 4 flips this
            to True as the fix for a failure production found.

            In real life this is not a flag, it is an edit to the prompt. It is a flag here
            so the workshop can show red and green side by side in one session without
            editing files mid-demo.
    """
    return SYSTEM_PROMPT + (SCOPE_BOUNDARY if scope_guard else "")


def middleware_stack(*, cheap_model: str | None = None) -> list[Any]:
    """ARIA's middleware, outermost first. Each entry replaces a sentence in v1's prompt.

    Order matters: this list is the nesting order, so the first entry wraps all the others.
    Guards that should see the *final* state of a message go later; limits that should
    short-circuit early go first.
    """
    summarizer = cheap_model or os.environ.get(
        "ARIA_CHEAP_MODEL", "anthropic:claude-haiku-4-5-20251001"
    )

    return [
        # --- 1. Spend limits. Non-negotiable, and the first thing to add to any agent. ---
        #
        # A confused agent in a retry loop is not a hypothetical. The usual trigger is a
        # tool that returns something the model reads as "almost worked, try again",
        # which is precisely the failure v1's `return "{}"` creates. Without a cap, the
        # ceiling on your spend is your provider's rate limit, and you find out from
        # Finance.
        #
        # Set these generously. The goal is not to constrain normal behavior, a job
        # package legitimately takes a dozen model calls, it is to make the pathological
        # case terminate. A limit at 3x your p99 costs you nothing and caps your downside.
        ModelCallLimitMiddleware(run_limit=25, thread_limit=120),
        ToolCallLimitMiddleware(run_limit=40, thread_limit=200),
        #
        # --- 2. Transient failure handling. ---
        #
        # `on_failure="continue"` hands the model a ToolMessage describing the failure
        # rather than raising, so a flaky data source degrades into a slightly worse answer
        # instead of a 500. Note the interaction with the limits above: retries count
        # toward the tool call cap, which is what you want, three tools each retrying
        # three times is nine calls and the budget should know it.
        ToolRetryMiddleware(
            max_retries=2,
            initial_delay=0.5,
            backoff_factor=2.0,
            on_failure="continue",
        ),
        #
        # --- 3. Planning. ---
        #
        # Adds the `write_todos` tool. NOT injected by `create_deep_agent` on its own in
        # deepagents 0.7, which we discovered the honest way: `tests/test_harness_context.py`
        # failed on its first run because the system prompt told the model to "plan the work
        # first with write_todos" and that tool was never presented to it.
        #
        # Worth dwelling on, because the failure was completely invisible from behavior. The
        # agent does not error when told to use a tool it does not have; it just quietly does
        # not plan, and the answers get slightly worse in a way you would attribute to the
        # model. A two-second harness test caught what no amount of reading the source would
        # have.
        #
        # The general rule: if your prompt names a tool, assert that the tool reaches the
        # model.
        TodoListMiddleware(),
        #
        # --- 4. Context management. ---
        #
        # A job package conversation runs long. Summarize with the *cheap* model, this is
        # the single easiest cost win in a long-running agent, and quality barely moves
        # because summarizing a transcript is a much easier task than the agent's actual
        # job. Module 2 measures that claim instead of asserting it.
        SummarizationMiddleware(
            model=summarizer,
            trigger={"tokens": 60_000},
            keep=("messages", 20),
        ),
        #
        # --- 5. Correct tool calls before the tool sees them. ---
        #
        # Josiah's failure mode #2: right tool, wrong arguments. Normalizes tags, clamps
        # out-of-range limits, and redirects tank tags to the tank tool. See
        # aria/middleware.py for why some of these are fixed silently and others are
        # returned to the model as a correction.
        ToolArgumentGuardMiddleware(),
        #
        # --- 6. Keep internal reasoning out of the answer. ---
        #
        # Josiah's failure mode #1. Removes delimited reasoning blocks outright; flags
        # suspected prose leakage for measurement rather than deleting it.
        ReasoningLeakMiddleware(),
        #
        # --- 7. Check the answer contract. Measures, does not rewrite. ---
        #
        # Runs last so it sees the answer after the leak filter has cleaned it. Annotates
        # the run when a procedural answer lacks a revision-qualified citation, which is
        # what Module 3 alerts on and Module 4 learns from.
        AnswerContractMiddleware(),
    ]


def build_agent(
    *,
    model: str | None = None,
    tools: list[Any] | None = None,
    checkpointer: Any | None = None,
    include_limits: bool = True,
    require_approval: bool = True,
    read_only: bool = False,
    extra_middleware: list[Any] | None = None,
    scope_guard: bool = False,
    platform_persistence: bool = False,
) -> Any:
    """Build ARIA v2.

    Args:
        model: `provider:model` string. Defaults to `$ARIA_MODEL`. Module 2 swaps this to
            compare quality against cost.
        tools: Defaults to `local_tools()`, in-process, synchronous, fast, which is what
            you want in notebooks and eval loops. Pass the result of `await mcp_tools()`
            for the production path over MCP. Note that the agent code does not care
            which; that is the payoff for putting the boundary in the right place.
        checkpointer: Required for multi-turn conversations, for `thread_limit` on the call
            limit middleware, for LangSmith threads, and for human-in-the-loop interrupts.
            Pass `InMemorySaver()` in a notebook; Postgres in production.
        include_limits: Set False only to demonstrate what runaway looks like. Module 1
            does exactly that, once, on purpose.
        require_approval: Gate `request_equipment_shutdown` behind a human interrupt.
            Defaults True and should stay True anywhere real. Set False for Module 2's
            automated evals, where nothing is watching to approve.
        read_only: Drop the write tools entirely. The right setting for the mocked-tool
            evals, an eval run should have no way to file a work order.
        scope_guard: Append the scope boundary to the system prompt. False by default,
            that is the gap we ship with in Module 1 and that production catches in Module 3.
            Module 4 flips it to True as the fix.
        platform_persistence: Set True when deploying to the LangGraph platform, which
            supplies its own durable checkpointer and REJECTS a custom one at import time.
            This satisfies the human-in-the-loop precondition below without passing a
            checkpointer we are not allowed to pass. See `aria/graph.py`.
        extra_middleware: Appended to the stack, so it becomes the INNERMOST layer. Used by
            `evals/harness.py` to capture the fully-assembled model request for harness
            assertions. Keeping this hook here means the harness tests exercise the real
            construction path instead of a hand-built lookalike.

    Returns:
        A compiled agent. Invoke with
        `agent.invoke({"messages": [...]}, config={"configurable": {"thread_id": ...}})`.
    """
    stack = [*middleware_stack(), *(extra_middleware or [])]
    if not include_limits:
        stack = [
            m
            for m in stack
            if not isinstance(m, (ModelCallLimitMiddleware, ToolCallLimitMiddleware))
        ]

    # --- human-in-the-loop on the consequential tool ----------------------------------
    #
    # `interrupt_on` pauses the graph before the tool runs and surfaces the pending call
    # for a human to approve, edit, or reject. Resume with
    # `Command(resume=...)`, see Module 2.
    #
    # Two things people get wrong:
    #
    # 1. It needs a checkpointer. Without one there is nowhere to persist the paused state
    #    and the interrupt cannot be resumed. We refuse loudly rather than silently
    #    running ungated, because "the approval gate quietly wasn't there" is the worst
    #    possible failure for this feature.
    #
    # 2. It is not a substitute for validation. `interrupt_on` protects against the *agent*
    #    doing something it should not. It does nothing about a request that is invalid on
    #    its own terms, because a tired approver will click yes. That is why
    #    `aria_mcp/work_orders.py` also refuses a shutdown request with no named human.
    #    Gate at the agent layer, validate at the application layer, do both.
    interrupt_on: dict[str, Any] | None = None
    if require_approval and not read_only:
        if checkpointer is None and not platform_persistence:
            raise ValueError(
                "require_approval=True needs a checkpointer to persist the paused state. "
                "Pass checkpointer=InMemorySaver() locally, platform_persistence=True when "
                "deploying to the LangGraph platform, or read_only=True / "
                "require_approval=False if you are running unattended evals."
            )
        interrupt_on = {name: True for name in DESTRUCTIVE_TOOLS}

    return create_deep_agent(
        model=model or os.environ.get("ARIA_MODEL", "anthropic:claude-sonnet-5"),
        tools=tools if tools is not None else local_tools(include_writes=not read_only),
        system_prompt=system_prompt(scope_guard=scope_guard),
        middleware=stack,
        checkpointer=checkpointer,
        interrupt_on=interrupt_on,
    )


async def build_production_agent(*, model: str | None = None, checkpointer: Any = None) -> Any:
    """ARIA v2 wired to the real MCP server. The shape you would actually deploy.

    The only difference from `build_agent` is where the tools come from, which is the
    whole argument for the boundary. Your agent code, your middleware, your prompt, and
    your evals are all unchanged when you move the application behind a protocol.
    """
    from aria.tools import mcp_tools

    return build_agent(
        model=model,
        tools=await mcp_tools(transport="stdio"),
        checkpointer=checkpointer,
    )


# --------------------------------------------------------------------- model selection

#: Candidates for Module 2's cost/quality experiment.
#:
#: The frontier keeps moving, and the honest summary as of this workshop is: the top
#: Anthropic and OpenAI models are the most capable and the most expensive, and the gap to
#: the good open-weight models has narrowed a lot faster than most people's mental model
#: has updated.
#:
#: GLM 5.2 is the one to look at if cost is a real constraint, open-weight, strong on
#: tool use, and roughly an order of magnitude cheaper per token than frontier. It is a
#: genuine option for the high-volume, well-scoped parts of your workload.
#:
#: The point of Module 2 is that you should not take that paragraph on faith, including
#: from us. Run the experiment on *your* dataset. "Cost-effective" is a property of a
#: model on a task, not a property of a model. A model that is 90% as good on a benchmark
#: may be 99% as good on your narrow task, or 40%. You cannot know which without measuring.
MODEL_CANDIDATES: dict[str, str] = {
    # The bake-off in Module 2 runs top-down. Start expensive, then try to give the money
    # back, that ordering matters, because it frames the cheap model as a candidate that
    # has to earn its place against a known-good baseline, rather than as a compromise you
    # are talking yourself into.
    "frontier": os.environ.get("ARIA_FRONTIER_MODEL", "anthropic:claude-opus-5"),
    "mid": os.environ.get("ARIA_MODEL", "anthropic:claude-sonnet-5"),
    "cheap": os.environ.get("ARIA_CHEAP_MODEL", "anthropic:claude-haiku-4-5-20251001"),
    # Open-weight, served over an OpenAI-compatible endpoint. Set GLM_API_KEY and
    # GLM_BASE_URL, then add "open_weight" to the --compare list.
    # "open_weight": "openai:glm-5.2",
}

#: The default bake-off: the two the decision usually comes down to.
DEFAULT_COMPARISON = ["frontier", "mid"]


if __name__ == "__main__":
    from langgraph.checkpoint.memory import InMemorySaver

    agent = build_agent(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "demo-1"}}

    for question in [
        "What do I need to do before pulling the seal on P-101? ",
        "What's the level in tank 43?",
    ]:
        result = agent.invoke({"messages": [{"role": "user", "content": question}]}, config)
        print(f"\n### {question.strip()}\n")
        print(result["messages"][-1].content)
