"""The deployable entry point. `langgraph.json` points here.

    langgraph dev                    # local server on :2024, hot reload
    langgraph deploy                 # to LangSmith Cloud, real REST endpoint

WHY THIS FILE EXISTS SEPARATELY
-------------------------------
`build_agent()` takes arguments. A deployment cannot, the server imports one module-level
graph object. So this file is where the production *configuration decisions* get made and
frozen, which is a useful place to have them all visible in one screen:

    scope_guard        False. This is the gap we ship with. Module 4 flips it.
    require_approval   True. The shutdown tool is gated. Non-negotiable in production.
    read_only          False. It is a real agent; it can raise work.
    checkpointer       Not passed. The platform supplies one; passing one is a load error.

DO NOT PASS A CHECKPOINTER HERE
------------------------------
The platform supplies a durable Postgres checkpointer, and it does not merely ignore a custom
one, it **refuses to load the graph at all**:

    GraphLoadError: Failed to load graph 'aria' from ./aria/graph.py: Heads up! Your graph
    includes a custom checkpointer (InMemorySaver). With LangGraph API, persistence is handled
    automatically by the platform...

Worth knowing because it collides with human-in-the-loop. `build_agent(require_approval=True)`
normally *requires* a checkpointer, that guard exists so an approval gate can never be
silently absent. Deployment is the one legitimate case where the caller has persistence but
cannot pass it, so it gets an explicit opt-out rather than a loophole:

    build_agent(require_approval=True, platform_persistence=True)

Two words instead of one, and the intent is on the page. (I found this the direct way: the
first `langgraph dev` refused to boot.)

TRACING PROJECT
---------------
Set `LANGSMITH_PROJECT=aria-production` for this. Keep production traces OUT of the project
your experiments write to. Mixing them ruins both: your production error rate gets polluted by
deliberately-failing eval runs, and your experiment history gets buried in live traffic.
"""

from __future__ import annotations

import os

from aria.agent_v2 import build_agent

#: The gap we ship with, on purpose. Module 3 catches it in production; Module 4 fixes it.
#: Flip to True (or set ARIA_SCOPE_GUARD=true) once you have watched the regression test fail.
SCOPE_GUARD = os.environ.get("ARIA_SCOPE_GUARD", "").strip().lower() in {"1", "true", "yes"}

graph = build_agent(
    scope_guard=SCOPE_GUARD,
    require_approval=True,
    # The platform owns persistence. See the note above, passing one is a load error.
    platform_persistence=True,
)

__all__ = ["graph"]
