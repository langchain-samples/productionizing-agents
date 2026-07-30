"""Custom middleware for ARIA.

Middleware is the answer to a question that comes up constantly once you start
productionizing: *"the model usually does the right thing here, how do I make it always
do the right thing?"* The tempting answer is to add another sentence to the system prompt.
The better answer, whenever the rule can be expressed in code, is a hook that runs whether
the model cooperates or not.

    prompt:     "Always cite the procedure id and revision."       (a request)
    middleware: check the answer, flag it if the citation is absent  (a guarantee)

Each class below targets a specific, observed production failure mode:

    ReasoningLeakMiddleware   Model emits its internal reasoning as the user-facing answer.
    ToolArgumentGuard         Model calls the right tool with wrong arguments, or the
                              wrong tool entirely.
    AnswerContractMiddleware  Model gives procedural advice without a citation.

A note on where these run: `before_model` / `after_model` are node-style hooks that run
once per model call. `wrap_tool_call` wraps every tool invocation, so it can inspect and
correct arguments before the tool ever sees them, or short-circuit the call entirely.
Middleware composes: the list order in `create_deep_agent(middleware=[...])` is the
nesting order, outermost first.

RULE: observability and guard code must never be the reason your agent falls over. Every
LangSmith interaction below is wrapped in a bare `except Exception`. That is deliberate,
not sloppy, an agent that 500s because a metadata write failed is strictly worse than one
that loses a tag.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Final

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.messages import AIMessage, ToolMessage
from langchain.tools.tool_node import ToolCallRequest
from langgraph.runtime import Runtime

# --------------------------------------------------------------------------- helpers

EQUIPMENT_TAG: Final[re.Pattern[str]] = re.compile(r"^[A-Z]{1,2}-\d{3}[A-Z]?$")
TANK_TAG: Final[re.Pattern[str]] = re.compile(r"^T-\d{3}$")
# A revision-qualified citation. The `[^\n]{0,140}?` is not laziness, it is the fix for a
# real false negative found by running the agent and reading what it actually produced.
#
# ARIA formats citations with the procedure TITLE between the id and the revision:
#
#     **SOP-LOTO-014** "Lockout/Tagout for Centrifugal Pumps" Rev 7 (effective 2025-03-11)
#     **SOP-MECH-108 (Mechanical Seal Replacement, API 682), Rev 3 (effective 2025-01-27)**
#
# The original `SOP-\w+-\d+\s+Rev\s+\d+` required them adjacent, so it reported "procedure id
# but NO revision" on three of five real formats, with the revision sitting right there in the
# text. An evaluator that says the agent isn't citing when it is will send you off to fix a
# prompt that was fine, and it makes the metric it feeds worthless.
#
# The lesson generalizes further than the regex: write your evaluators against output your
# agent ACTUALLY produced, not against output you imagined it would produce. Same-line window
# only, so it cannot pair an id with a revision from a different bullet.
CITATION: Final[re.Pattern[str]] = re.compile(
    r"\bSOP-[A-Z]+-\d+[^\n]{0,140}?\bRev(?:ision)?\.?\s*\d+",
    re.IGNORECASE,
)
PROCEDURE_ID: Final[re.Pattern[str]] = re.compile(r"\bSOP-[A-Z]+-\d+\b", re.IGNORECASE)


def _annotate_run(**fields: Any) -> None:
    """Attach metadata to the current LangSmith run, if there is one.

    This is how a middleware makes itself visible to Module 3. Anything written here
    becomes a filterable field on the trace, which means you can build a monitoring chart
    or an alert on it, `metadata.reasoning_leak_detected = true`, for instance.

    Silently does nothing when tracing is off, which is what you want for local runs and
    for unit tests.
    """
    try:
        from langsmith.run_helpers import get_current_run_tree

        run = get_current_run_tree()
        if run is None:
            return
        metadata = run.extra.setdefault("metadata", {})
        for key, value in fields.items():
            # Counters accumulate across a run; everything else is last-write-wins.
            if key.endswith("_count") and isinstance(value, int):
                metadata[key] = metadata.get(key, 0) + value
            else:
                metadata[key] = value
    except Exception:  # noqa: BLE001, never let telemetry break the agent
        pass


def _text_of(message: AIMessage) -> str:
    """Flatten an AIMessage's user-visible text, whatever content shape the provider used."""
    content = message.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def _replace_text(message: AIMessage, new_text: str) -> AIMessage:
    """Return a copy of `message` with its text replaced, preserving the id.

    Preserving `id` is the whole trick: LangGraph's `add_messages` reducer treats a message
    with an existing id as a replacement rather than an append. Drop the id and you get
    two assistant messages instead of one corrected one.

    Reasoning content blocks are dropped rather than rewritten, they are the thing we are
    removing from the visible answer.
    """
    if isinstance(message.content, str):
        new_content: Any = new_text
    else:
        new_content = [{"type": "text", "text": new_text}]

    return message.model_copy(update={"content": new_content})


# ------------------------------------------------------------------- 1. reasoning leak


class ReasoningLeakMiddleware(AgentMiddleware):
    """Strip internal reasoning that leaked into the user-facing answer.

    The failure looks like this in production: instead of an answer, the user gets
    "Okay, so the user is asking about lockout/tagout for P-101A. Let me search the
    procedure library. I should probably also check the equipment record...", a wall of
    first-person deliberation. It is inconspicuous in your own testing because you *can*
    read it and it *is* roughly correct, so it survives review. Your users hate it.

    It happens for a few reasons: a model whose reasoning tokens are not being separated
    into the right channel, a prompt that asked the model to "think step by step" without
    saying where, or a provider/version change that altered how reasoning is returned.
    That last one is why this belongs in code, it can start happening on a Tuesday
    without you shipping anything.

    Two mechanisms, with deliberately different confidence levels:

    * Explicitly delimited blocks (`<thinking>...</thinking>`, `<scratchpad>`, and
      friends) are **removed**. Zero false positives, these markers never legitimately
      appear in an answer about pump seals.
    * Reasoning-sounding *prose* is only **flagged**, never removed. A regex confident
      enough to delete prose is a regex confident enough to delete a real answer. We
      annotate the run so Module 3 can alert on the rate, and Module 2 writes an evaluator
      that scores it properly.

    The distinction matters more than the code does: automate the intervention where you
    have certainty, and route to measurement where you do not.
    """

    #: Tag-delimited reasoning. Safe to remove outright.
    _BLOCKS: Final[re.Pattern[str]] = re.compile(
        r"<\s*(thinking|thought|thoughts|scratchpad|reasoning|internal|antml:thinking)\s*>"
        r".*?"
        r"<\s*/\s*\1\s*>",
        re.IGNORECASE | re.DOTALL,
    )
    #: An unclosed opening tag: the model started a block and never terminated it, so the
    #: remainder of the message is reasoning. Applied only after the paired form.
    _DANGLING: Final[re.Pattern[str]] = re.compile(
        r"<\s*(thinking|thought|scratchpad|reasoning|internal)\s*>.*\Z",
        re.IGNORECASE | re.DOTALL,
    )
    #: Prose that reads like deliberation, at the very start of the answer. Flag only.
    _PROSE: Final[re.Pattern[str]] = re.compile(
        r"\A\s*(okay|ok|alright|so|right|hmm|let me|let's|i need to|i should|i'll start|"
        r"the user is asking|the user wants|first,? i)\b",
        re.IGNORECASE,
    )

    def after_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        messages = state["messages"]
        if not messages:
            return None

        last = messages[-1]
        if not isinstance(last, AIMessage):
            return None

        # A message with pending tool calls is not the final answer; leave it alone.
        if last.tool_calls:
            return None

        original = _text_of(last)
        cleaned = self._BLOCKS.sub("", original)
        cleaned = self._DANGLING.sub("", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

        if cleaned != original.strip():
            _annotate_run(
                reasoning_leak_detected=True,
                reasoning_leak_kind="delimited_block",
                reasoning_leak_chars_removed=len(original) - len(cleaned),
            )
            # A message that was *entirely* reasoning leaves us with nothing to show. Say
            # so rather than returning an empty bubble; an empty answer is invisible in
            # aggregate metrics but very visible to the person waiting for it.
            if not cleaned:
                cleaned = (
                    "I wasn't able to produce an answer for that. Please rephrase, or ask "
                    "your Unit Supervisor."
                )
            return {"messages": [_replace_text(last, cleaned)]}

        if self._PROSE.match(original):
            _annotate_run(
                reasoning_leak_detected=True,
                reasoning_leak_kind="suspected_prose",
            )

        return None


# ---------------------------------------------------------------- 2. tool argument guard


class ToolArgumentGuardMiddleware(AgentMiddleware):
    """Normalize and route tool calls before the tool sees them.

    The second most common model failure, after leaked reasoning, is calling a tool
    *nearly* right: `get_equipment("p-101a")` instead of `"P-101A"`, `get_equipment("T-042")`
    when tanks live behind a different tool, `limit=20` when the tool accepts 1-5.

    You have three options for each of these. Ranked:

    1.  **Fix it in code** where the correct behavior is unambiguous. Case normalization is
        not a judgment call, the model should not have to spend a turn learning that.
    2.  **Return a corrective message** where a fix would be a guess. Redirecting a tank
        tag to the tank tool is arguably a fix, but silently changing which tool ran makes
        the trace lie about what happened, so we tell the model instead and let the next
        turn be honest.
    3.  **Add a sentence to the prompt.** Last resort. It costs tokens on every single turn
        and it works most of the time, which is the worst reliability profile there is.

    Everything corrected here is annotated onto the run, so `metadata.tool_args_corrected`
    becomes a number you can watch. A rising correction rate is an early signal that a
    model upgrade has changed behavior, or that a tool description needs work.
    """

    _TAG_ARG_TOOLS: Final[frozenset[str]] = frozenset(
        {"get_equipment", "get_tank_status"}
    )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Any],
    ) -> ToolMessage | Any:
        name = request.tool_call.get("name", "")
        args = dict(request.tool_call.get("args") or {})
        call_id = request.tool_call.get("id", "")
        corrections: list[str] = []

        # --- normalize an equipment/tank tag: strip, uppercase, drop stray words -------
        if name in self._TAG_ARG_TOOLS and isinstance(args.get("tag"), str):
            raw = args["tag"]
            normalized = raw.strip().upper().replace("TANK ", "T-").replace("_", "-")
            if normalized != raw:
                args["tag"] = normalized
                corrections.append(f"tag {raw!r} -> {normalized!r}")

            tag = args["tag"]

            # --- route to the correct tool rather than silently substituting ----------
            if name == "get_equipment" and TANK_TAG.match(tag):
                _annotate_run(tool_misroute_count=1, tool_misroute_last=name)
                return ToolMessage(
                    content=(
                        f"{tag} is a storage tank. get_equipment covers rotating "
                        f"equipment, vessels, columns, and exchangers only. Call "
                        f"get_tank_status(tag='{tag}') instead."
                    ),
                    tool_call_id=call_id,
                    name=name,
                )
            if name == "get_tank_status" and not TANK_TAG.match(tag):
                _annotate_run(tool_misroute_count=1, tool_misroute_last=name)
                return ToolMessage(
                    content=(
                        f"{tag} is not a tank tag (tanks are T- followed by three "
                        f"digits). Call get_equipment(tag='{tag}') instead."
                    ),
                    tool_call_id=call_id,
                    name=name,
                )

        # --- clamp a search limit into the range the tool actually accepts -------------
        if name == "search_procedures" and "limit" in args:
            raw_limit = args["limit"]
            try:
                clamped = max(1, min(5, int(raw_limit)))
            except (TypeError, ValueError):
                clamped = 5
            if clamped != raw_limit:
                args["limit"] = clamped
                corrections.append(f"limit {raw_limit!r} -> {clamped}")

        if corrections:
            _annotate_run(
                tool_args_corrected_count=len(corrections),
                tool_args_corrected_last=f"{name}: {'; '.join(corrections)}",
            )
            request = self._with_args(request, args)

        return handler(request)

    @staticmethod
    def _with_args(request: ToolCallRequest, args: dict[str, Any]) -> ToolCallRequest:
        """Apply corrected arguments, preferring the immutable path when available.

        `ToolCallRequest.override` is the supported API in current LangChain; the in-place
        fallback keeps this working on older point releases. Version-defensive code in a
        workshop repo earns its keep, half the room will be on a different minor.
        """
        override = getattr(request, "override", None)
        if callable(override):
            try:
                return override(tool_call={**request.tool_call, "args": args})
            except Exception:  # noqa: BLE001
                pass
        request.tool_call["args"] = args
        return request


# ------------------------------------------------------------- 3. answer contract check


class AnswerContractMiddleware(AgentMiddleware):
    """Check the final answer against a contract that can be checked deterministically.

    ARIA's contract: *any answer that gives procedural guidance must carry a citation in
    the form `SOP-XXX-NNN Rev N`.* That is checkable with a regex, which means it does not
    need a model to check it, which means it costs nothing and never flakes.

    We deliberately **do not** rewrite the answer. Two reasons:

    * We cannot invent a citation. If the model gave advice with no source, appending a
      plausible-looking source is the worst possible outcome, we would be manufacturing
      exactly the false confidence the contract exists to prevent.
    * A visible, measured violation is more valuable than a hidden, patched one. This is
      the middleware that makes "our agent gave unsourced safety advice" a number on a
      dashboard with an alert on it, rather than something a user discovers.

    So: annotate, and let Modules 3 and 4 do their jobs. This is the seam between building
    an agent and operating one, and it is worth pausing on during the workshop, the
    decision *not* to auto-fix is the engineering judgment being taught here.
    """

    def after_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        messages = state["messages"]
        if not messages:
            return None

        last = messages[-1]
        if not isinstance(last, AIMessage) or last.tool_calls:
            return None

        answer = _text_of(last)
        if not answer.strip():
            return None

        # Did the agent actually consult the procedure library this turn?
        consulted = any(
            isinstance(m, ToolMessage)
            and m.name in {"search_procedures", "get_procedure"}
            for m in messages
        )
        mentions_procedure = bool(PROCEDURE_ID.search(answer))
        has_citation = bool(CITATION.search(answer))

        _annotate_run(
            consulted_procedures=consulted,
            answer_has_citation=has_citation,
        )

        # Gave procedural guidance with no revision-qualified citation.
        if (consulted or mentions_procedure) and not has_citation:
            _annotate_run(
                contract_violation="missing_citation",
                # Cheap, high-signal triage field: was there a source available to cite,
                # or did the agent answer from nowhere? Those are different bugs with
                # different fixes, and you want to tell them apart from the dashboard.
                citation_available=consulted,
            )

        # Cited a procedure it never actually read. Rarer and much worse, this is
        # fabricated provenance, which is more dangerous than no provenance because it
        # survives review.
        if mentions_procedure and not consulted:
            _annotate_run(contract_violation="uncited_source_fabricated")

        return None
