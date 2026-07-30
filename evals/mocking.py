"""Tool mocking driven by dataset `reference_outputs`.

THE IDEA
--------
Module 1 established that the tools are correct, 110 tests, no LLM. So evaluating the
agent does not mean evaluating retrieval again. It means asking:

    "Given a specific tool response, does the agent behave correctly?"

That reframes the problem into something you can write test cases for. Instead of hoping
production hands you a suspect tank gauge so you can see what the agent does, you *script*
the suspect gauge and assert the behavior. And the scripts live in the dataset, which means
adding a case is adding a row, not writing code.

WHERE THE SCRIPT LIVES
----------------------
Both halves of a dataset example accept arbitrary JSON. LangSmith does not care what you put
there, only your target and your evaluators do. The mock world goes in `inputs`, because the
*target* is what needs it, and because the world a case runs in is an input by definition:

    {
      "inputs": {
        "question": "What's the level in tank 43?",
        "mock_tools": {
          "get_tank_status": {"tag": "T-043", "level_ft": 12.1, "atg_status": "suspect",
                              "data_quality_warnings": ["ATG is suspect..."]}
        }
      },
      "reference_outputs": {
        "expect_tool_calls": ["get_tank_status"],
        "must_mention": ["suspect"],
        "must_not_claim_success": false
      }
    }

One target function reads that, builds an agent whose tools return exactly those responses,
runs it, and returns the trajectory. One evaluator set scores it. Adding the 40th test case
costs one JSON object.

MOCK THE BEHAVIOR, NEVER THE CONTRACT
-------------------------------------
Every mock below is built with the *real* tool's name, description, and `args_schema`. The
model sees a byte-identical tool surface; only the body changes. This matters more than it
sounds: if your mock has a simplified schema, you are evaluating an agent that does not
exist, and your passing evals tell you nothing about production. `assert_contract_parity`
enforces it, and Module 2 runs that assertion in front of you.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import StructuredTool

from aria.tools import LOCAL_TOOLS, arg_schema

#: name -> the real tool, for schema/description cloning.
REAL_TOOLS: dict[str, Any] = {t.name: t for t in LOCAL_TOOLS}

#: What a stub returns when the dataset didn't script it. Deliberately an explicit marker
#: rather than something plausible: if the agent leans on an unscripted tool, you want that
#: to be visible in the trace, not silently absorbed into a confident answer.
UNSCRIPTED = {"error": "This tool was not scripted for this test case.", "recoverable": False}


@dataclass(slots=True)
class CallRecorder:
    """Records every tool call the agent made, in order.

    This is the trajectory, and it's what lets you assert on *process* rather than only on
    the final answer. "Did it call get_tank_status before answering about a tank level" is
    often a sharper test than anything you can check in the prose, and it's a code
    assertion, so it costs nothing and never flakes.
    """

    calls: list[dict[str, Any]] = field(default_factory=list)

    def record(self, name: str, args: dict[str, Any], result: Any) -> None:
        self.calls.append({"name": name, "args": args, "result": result})

    @property
    def names(self) -> list[str]:
        return [c["name"] for c in self.calls]

    def args_for(self, name: str) -> list[dict[str, Any]]:
        return [c["args"] for c in self.calls if c["name"] == name]

    def to_json(self) -> list[dict[str, Any]]:
        """Serializable form, for stashing on the experiment output so you can inspect a
        failing case in the LangSmith UI without re-running it."""
        return [
            {"name": c["name"], "args": c["args"], "result": _truncate(c["result"])}
            for c in self.calls
        ]


def _truncate(value: Any, limit: int = 600) -> Any:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def make_mock_tool(name: str, script: Any, recorder: CallRecorder) -> StructuredTool:
    """Clone a real tool's contract, replace its body with a scripted response.

    Args:
        name: A real ARIA tool name.
        script: What the tool should return. Three forms:
            * a dict/str: returned every time it is called.
            * a list: returned in sequence, one per call. The last entry repeats once
              exhausted. Use this for "fails, then succeeds on retry" cases.
            * `{"raise": "message"}`: the tool *throws* instead of returning. Distinct from
              returning `{"error": ...}`, and worth testing separately: a raised exception
              takes the `ToolRetryMiddleware` path, a returned error does not.
    """
    real = REAL_TOOLS.get(name)
    if real is None:
        raise KeyError(f"{name!r} is not an ARIA tool. Known: {sorted(REAL_TOOLS)}")

    responses = list(script) if isinstance(script, list) else [script]
    cursor = {"i": 0}

    def body(**kwargs: Any) -> Any:
        index = min(cursor["i"], len(responses) - 1)
        cursor["i"] += 1
        response = responses[index]

        if isinstance(response, dict) and "raise" in response:
            recorder.record(name, kwargs, {"raised": response["raise"]})
            raise RuntimeError(response["raise"])

        recorder.record(name, kwargs, response)
        return response

    return StructuredTool.from_function(
        func=body,
        name=real.name,
        description=real.description,
        args_schema=real.args_schema,
    )


def mocked_toolset(
    mock_spec: dict[str, Any] | None,
    recorder: CallRecorder,
    *,
    stub_unscripted: bool = True,
) -> list[StructuredTool]:
    """The full ARIA toolset, with behavior from `mock_spec`.

    Args:
        mock_spec: `{tool_name: script}` from the dataset example's `inputs.mock_tools`.
            `None` or `{}` gives you Level 1: every tool is a stub. That is the right setup
            for "does the agent work at all" and "did it pick the right tool", you are
            testing routing, and a stub answers that as well as real data would, for free.
        recorder: Collects the trajectory.
        stub_unscripted: Stub out tools the dataset didn't mention. Keep True, the whole
            point is a hermetic test, and a single un-mocked tool reaching real data is how
            a test suite becomes quietly non-deterministic.
    """
    spec = dict(mock_spec or {})
    if stub_unscripted:
        for name in REAL_TOOLS:
            spec.setdefault(name, UNSCRIPTED)

    return [make_mock_tool(name, script, recorder) for name, script in spec.items()]


def assert_contract_parity() -> None:
    """Mocks must present an identical surface to the real tools.

    Called in Module 2's notebook, and in `evals/test_stateful.py`. If this ever fails, your
    evals are measuring an agent that does not exist in production, the most expensive kind
    of green test suite.
    """
    recorder = CallRecorder()
    for name, real in REAL_TOOLS.items():
        mock = make_mock_tool(name, {"ok": True}, recorder)
        assert mock.name == real.name
        assert mock.description == real.description, f"{name}: description drifted"
        assert arg_schema(mock) == arg_schema(real), f"{name}: args schema drifted"


# --------------------------------------------------------------------------- shorthands
#
# Named scripts for the failure modes we test repeatedly. Naming them keeps the dataset
# readable and, more usefully, makes the catalogue of things-that-go-wrong explicit and
# reviewable. When someone asks "what failure modes do we test for?", this is the answer.

TOOL_ERROR = {"error": "Upstream maintenance system returned 503 Service Unavailable.", "recoverable": True}
TOOL_TIMEOUT = {"raise": "TimeoutError: maintenance system did not respond within 30s"}
TOOL_PERMISSION_DENIED = {
    "error": "Permission denied: this account is not authorized to create work orders.",
    "recoverable": False,
}
TOOL_EMPTY = {"results": [], "count": 0}
TOOL_FAILS_THEN_SUCCEEDS = [TOOL_ERROR, {"ok": True}]
