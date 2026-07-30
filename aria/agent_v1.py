"""ARIA v1: the version everybody ships first.

DO NOT USE THIS AS A TEMPLATE. It exists so we can dissect it in Module 1.

Every problem in this file is one we have watched a real team hit. None of it is stupid;
all of it is the locally-reasonable choice that becomes a production problem at scale.
It also *works*: that is the trap. Run it, watch it answer a question correctly, and then
read the eight numbered pitfalls below and find them in the code.

Each `PITFALL n` marker corresponds to a slide in Module 1 and to a fix in `agent_v2.py`.
Try to spot them before you read the labels.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain.tools import tool

# PITFALL 1: Application logic living inside the agent package.
# Data access is reimplemented here, next to the prompt, in the same module as the agent.
# There is now no boundary at which you can say "the tools are correct." When the agent
# gives a wrong answer you cannot tell whether the model reasoned badly or the retrieval
# returned garbage, so every debugging session investigates both. Two coupled
# non-deterministic failure modes cost far more than the sum of two independent ones.
_DATA = Path(__file__).resolve().parent.parent / "data"


def _load(name: str) -> list[dict[str, Any]]:
    with (_DATA / name).open() as handle:
        return json.load(handle)


@tool
def search_procedures(query: str) -> str:
    """Search procedures."""
    # PITFALL 2: A one-line tool description.
    # This docstring is sent to the model on every single turn and it is the only guidance
    # the model has about when to reach for this tool. "Search procedures" tells it
    # nothing about what a good query looks like, what the result means, or what to do when
    # nothing matches. Compare with aria_mcp/server.py.
    procs = _load("procedures.json")
    hits = [p for p in procs if query.lower() in json.dumps(p).lower()]

    # PITFALL 3: Unbounded, unstructured tool output.
    # Every matching procedure, in full, dumped as a JSON blob. Four matches is 12k
    # tokens. It works on the demo query and quietly costs you the context window on the
    # query your biggest customer asks. There is also no ordering guarantee here beyond
    # file order, and no citation, so the model assembles provenance itself, which is
    # to say it sometimes invents it.
    return json.dumps(hits)


@tool
def get_equipment(tag: str) -> str:
    """Get equipment info for a tag."""
    items = _load("equipment.json")
    for item in items:
        if item["tag"] == tag:
            return json.dumps(item)

    # PITFALL 4: Returning empty/null instead of an actionable error.
    # This is the highest-severity bug in the file and the easiest to miss in review.
    # A user asks about "P-101". The model calls get_equipment("P-101"). There is no such
    # tag, the real pumps are P-101A and P-101B, so this returns "{}". The model reads
    # an empty object as "that equipment does not exist," tells the user so with complete
    # confidence, and moves on. A correct API produced a lying agent.
    #
    # It is also silently case-sensitive: get_equipment("p-101a") returns "{}" too.
    return "{}"


@tool
def get_tank_status(tag: str) -> str:
    """Get tank level."""
    for tank in _load("tank_readings.json"):
        if tank["tag"] == tag:
            # PITFALL 5: Business rules left to the model's judgment.
            # T-043's automatic gauge is flagged suspect and a receipt is in progress, so
            # its level reading should not be quoted as fact. That rule is in SOP-OPS-055
            # and it is perfectly expressible in Python. Here we hand the model the raw
            # fields and hope it notices `"atg_status": "suspect"` buried in a JSON blob
            # and knows what that implies. Sometimes it does. That is the problem,
            # "sometimes" is not a safety property, and it degrades silently on a model
            # upgrade.
            return json.dumps(tank)
    return "{}"


# PITFALL 6: Using the prompt to enforce what code should guarantee.
# Read this prompt as a list of things that will each be true about 90% of the time.
# "ALWAYS cite": measured at 90%, that is one unsourced safety answer in ten.
# "NEVER show your reasoning": the model that leaks reasoning is not doing so
# deliberately, so asking it not to does not address the cause.
# "Use at most 4 tool calls": the model cannot count its own tool calls reliably;
# it has no reliable access to that state.
# Every one of these is a middleware in agent_v2.py, where it is a guarantee instead of
# a request. The prompt also pays for all of this in tokens on every turn.
SYSTEM_PROMPT = """You are ARIA, a refinery maintenance and HSE assistant.

You help technicians and operators with maintenance procedures, safety requirements, and
equipment status.

IMPORTANT RULES:
- ALWAYS cite the procedure ID and revision number when you reference a procedure.
- NEVER make up procedure IDs or revision numbers.
- NEVER show your reasoning or thinking to the user. Only give the final answer.
- Do not use more than 4 tool calls per question.
- If you don't know, say so.
- Be concise but complete.
- If the question involves a safety-critical procedure, remind the user to verify with
  their supervisor.
- Always mention if data might be unreliable.
- Do not give advice about anything outside the refinery.
- Answer in plain text, not markdown.
- Be helpful and professional.
"""


def build_agent(model: str | None = None):
    """Build ARIA v1.

    PITFALL 7: No limits of any kind.
    No cap on model calls, no cap on tool calls, no summarization. If the model gets into
    a retry loop against a tool that keeps returning "{}", see PITFALL 4, which makes
    that loop *likely*, nothing stops it. The ceiling on your bill is whatever your
    provider's rate limit happens to be. You find out the next morning.

    PITFALL 8: No thread/checkpointer and no session identity.
    Every invocation is isolated, so there is no multi-turn conversation, and in LangSmith
    there is no thread to group related runs. When you get a complaint about "the
    conversation where it told me the wrong isolation points," you have no way to
    reconstruct it. You also cannot attach per-conversation feedback, which is what
    Module 4's improvement loop runs on.
    """
    return create_agent(
        model=model or os.environ.get("ARIA_MODEL", "anthropic:claude-sonnet-5"),
        tools=[search_procedures, get_equipment, get_tank_status],
        system_prompt=SYSTEM_PROMPT,
    )


if __name__ == "__main__":
    agent = build_agent()
    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    # Try this, then try "P-101" instead of "P-101A" and watch PITFALL 4.
                    "content": "What do I need to do before pulling the seal on P-101A?",
                }
            ]
        }
    )
    print(result["messages"][-1].content)
