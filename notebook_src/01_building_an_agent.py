"""Module 1 source: see scripts/build_notebooks.py."""

# %% [markdown]
# # Module 1: Building an Agent
#
# ### Best practices and common pitfalls
#
# **30 minutes.** This is the only module where we build. Modules 2, 3, and 4 evaluate,
# monitor, and improve what you build here.
#
# The agent is **ARIA**, a maintenance and HSE assistant for a refinery. It answers
# questions about procedures, equipment, and tank status. We picked a safety-adjacent
# domain on purpose: it makes the cost of every shortcut concrete. A hallucinated
# isolation procedure is not a bad demo, it's an incident.
#
# There are two versions of ARIA in this repo:
#
# | | |
# |:--|:--|
# | `aria/agent_v1.py` | What almost everyone ships first. It works. It has eight problems. |
# | `aria/agent_v2.py` | What you'd put in front of a technician. |
#
# We're going to find the eight problems, and fix them with five practices:
#
# 0. **Start from the outcomes, not the capabilities.** Write down the workflows that must
#    work before you write any agent code.
# 1. **Keep application logic out of the agent.** Put it behind a tested API, expose it
#    over MCP.
# 2. **Move rules from the prompt into middleware.** Prompts request; middleware
#    guarantees.
# 3. **Set limits.** Always.
# 4. **Choose the model deliberately**, and then measure that choice (Module 2).

# %% [markdown]
# ---
# ## 0. Setup
#
# If `preflight.py` passed before the session, this cell is a formality.

# %%
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from dotenv import load_dotenv

# Notebooks run from notebooks/, everything else assumes the repo root.
ROOT = Path.cwd().parent if Path.cwd().name == "notebooks" else Path.cwd()
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
load_dotenv(ROOT / ".env")


def show(text: str, width: int = 96) -> None:
    """Wrap long agent answers so they're readable in the notebook."""
    for para in str(text).split("\n"):
        print(textwrap.fill(para, width=width) if para.strip() else "")


HAS_LLM = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"))
HAS_LANGSMITH = bool(os.environ.get("LANGSMITH_API_KEY"))

print(f"repo root:  {ROOT}")
print(f"model:      {os.environ.get('ARIA_MODEL', '(unset)')}")
print(f"LLM key:    {'yes' if HAS_LLM else 'NO, the no-LLM cells still work'}")
print(f"LangSmith:  {'yes' if HAS_LANGSMITH else 'NO, traces will not be recorded'}")
print(f"project:    {os.environ.get('LANGSMITH_PROJECT', '(unset)')}")

# %% [markdown]
# ---
# ## 1. Practice 0. Start from outcomes, not capabilities
#
# This one comes before any code, so it comes first, and it's the practice most likely to
# save you a wasted quarter.
#
# The default way agent projects start:
#
# > *"We've got a document search API and a work-order system. What could an agent do with
# > those?"*
#
# You build something impressive, demo it, and a planner says *"that's neat, but it doesn't
# do the thing I actually need."* You discover the workflow you should have supported after
# building the one you could.
#
# | | |
# |:--|:--|
# | **Capability-first** *(the pitfall)* | Start from the tools you have. Work forwards. Discover requirements last. |
# | **Outcome-first** *(the practice)* | Start from the workflows that must work. Work backwards into the capabilities each one needs. |
#
# ### Make it concrete: write the spec as test cases
#
# The practice has a specific, executable form:
#
# > **Before you write a line of agent code, hand-craft the dataset.**
# >
# > *"When the user says X, the response should look like Y, and the world should look like
# > Z when it's done."*
#
# That table is readable by a reliability manager, a planner, an HSE lead. They will never
# review your prompt and they will not read your graph, but they will absolutely tell you
# which workflows matter and where the edges are, **at design time**, if you show them a
# table.
#
# Here's the one we wrote for closing out work orders, before that capability existed:

# %%
from evals.datasets import spec_table

print(spec_table())

# %% [markdown]
# Six rows, no code. Row 2: *"close out WO-90001, the seal work is finished"* with nobody's
# name attached, is exactly the kind of edge a planner raises in a ten-minute review and
# that you would not think of alone. Row 5 (*"close out that pump work order I raised
# earlier"*) is **why `list_work_orders` exists**: the workflow demanded a capability, rather
# than a capability looking for a use.
#
# This doubles as your test suite (Module 2 runs it red-then-green), which means unlike a
# requirements document it **cannot silently go stale**.
#
# We're doing it out of order today, building first, specifying second, for teaching
# convenience. Don't copy that part.

# %% [markdown]
# ---
# ## 2. The anatomy of an agent in 2026
#
# Strip away the branding and essentially every production agent is the same loop:
#
# ```
#                    ┌──────────────────────────────────────────┐
#                    │                                          │
#                    ▼                                          │
#   user ──▶  [ MIDDLEWARE ]  ──▶  MODEL  ──▶  tool calls?  ─────┘
#                    ▲                            │
#                    │                            ▼ no
#                    └──── tool results ◀──  [ MIDDLEWARE ] ──▶ answer
# ```
#
# That's ReAct: the model reasons, calls tools, reads results, repeats until it's done.
# What separates a prototype from a production agent is almost entirely **what you hang
# on that loop**:
#
# | Layer | What it does | Determinism |
# |:--|:--|:--|
# | **Model** | Decides what to do next | None. It's a sample from a distribution. |
# | **Tools** | Does the work | Yours to guarantee. Make it total. |
# | **Middleware** | Hooks at fixed lifecycle points | Fully deterministic. Runs every time. |
# | **Harness** | Planning, files, subagents, compression | Deterministic scaffolding |
#
# The single most useful lens for the next 30 minutes:
#
# > **You are handed one irreducibly non-deterministic component. Everything you build
# > around it should be as deterministic as you can make it.**
#
# Every practice below is an application of that one idea.
#
# We build on **Deep Agents** (`create_deep_agent`), which is LangChain's harness: a ReAct
# loop with a planning tool, a filesystem, subagent delegation, and context compression
# already wired in. More on when that's the right call, and when it isn't, in §8.

# %% [markdown]
# ---
# ## 3. Practice 1. Get your application logic out of the agent
#
# ### The problem
#
# Here's the anti-pattern, from `aria/agent_v1.py`:

# %%
print(Path("aria/agent_v1.py").read_text().split("@tool")[1].split("@tool")[0][:900])

# %% [markdown]
# Data access, prompt, and agent wiring in one module. It works, and it costs you the
# single most valuable thing you can have when debugging an agent: **the ability to say
# "the tools are correct" and mean it.**
#
# When your agent gives a wrong answer, there are two candidate causes:
#
# 1. The model reasoned badly.
# 2. The tools returned something wrong, empty, or misleading.
#
# If you can't rule out (2) cheaply, every debugging session investigates both. And these
# aren't independent, bad tool output *causes* bad reasoning, so you get to watch the
# model make a reasonable inference from garbage and then argue with yourself about
# whether the model is dumb.
#
# You are already dealing with one non-deterministic component. Do not add a second
# poorly-understood one underneath it.
#
# ### The practice
#
# Application logic goes in its own package, with its own tests, and is exposed to the
# agent over a protocol. In this repo:
#
# ```
# aria_mcp/          the application, no langchain import anywhere in it
#   repository.py      data access, validation, business rules
#   server.py          the same API, exposed over MCP
# aria/              the agent
#   agent_v2.py        prompt, middleware, model
# ```
#
# Let's look at what that buys us. First, the application test suite:

# %%
result = subprocess.run(
    [sys.executable, "-m", "pytest", "tests/", "-q", "--no-header"],
    capture_output=True,
    text=True,
)
print(result.stdout[-1200:])

# %% [markdown]
# **168 tests. No API key. No model. No network. Six seconds.**
#
# That is the bar for the layer underneath your agent, and it is completely achievable
# because none of it involves an LLM. Now when ARIA misbehaves, I get to skip an entire
# branch of the search tree.
#
# ### What "a good API surface" means when the caller is a model
#
# The design rules change when your consumer is an LLM rather than a programmer. The
# biggest one, and the source of the nastiest bug in v1:

# %%
# --- v1: unknown tag returns empty ---
from aria.agent_v1 import get_equipment as v1_get_equipment

print("v1  get_equipment('P-101') →", v1_get_equipment.invoke({"tag": "P-101"}))

# %% [markdown]
# `{}`.
#
# Think about what the model does with that. The user asked about "P-101". The real pumps
# are **P-101A** and **P-101B**, an A/B pair, which is how essentially all critical
# refinery pumps are installed. The model called the tool correctly, got an empty object,
# and will now tell the user, with total confidence, that there is no such equipment.
#
# **A technically-correct API produced a lying agent.** No exception was raised. Nothing
# is in your error logs. Your dashboards are green.
#
# Now the same call against the application layer:

# %%
from aria.tools import get_equipment

print("v2  get_equipment('P-101') →")
print(json.dumps(get_equipment.invoke({"tag": "P-101"}), indent=2))

# %% [markdown]
# The error **names the fix**. The model reads "P-101A, P-101B", picks one, and recovers
# inside the same turn without a human involved.
#
# Write your error messages for the model that will read them. That sounds like a small
# thing. It is one of the highest-leverage changes you can make to an agent's reliability,
# because it converts a silent wrong answer into a self-correcting retry.
#
# The rest of the rules in `aria_mcp/repository.py`:
#
# | Rule | Why it matters when a model is calling |
# |:--|:--|
# | Unknown identifier → error, not `[]` | An empty result reads as "doesn't exist". Silent wrong answers. |
# | Errors carry the fix | Turns a dead end into a recoverable retry. |
# | Output is **bounded** | Unbounded tool output is a context-window incident waiting for your largest customer. |
# | Output is **deterministic** | Stable sort order. Otherwise your evals flake for reasons unrelated to the model. |
# | Provenance travels with data | You cannot evaluate groundedness if there's nothing to be grounded in. |
# | Business rules in code, not prompt | See below. |
#
# That last one deserves a demonstration. Tank T-043's automatic gauge is flagged suspect
# *and* it has a receipt in progress, so its level must not be quoted as fact. SOP-OPS-055
# says so. Here's v1's approach, hand the model raw JSON and hope:

# %%
from aria.agent_v1 import get_tank_status as v1_tank

raw = json.loads(v1_tank.invoke({"tag": "T-043"}))
print("v1 gives the model these fields and hopes it notices the right two:\n")
print(json.dumps(raw, indent=2)[:700])

# %% [markdown]
# Somewhere in there is `"atg_status": "suspect"`. The model has to (a) notice it,
# (b) know what it implies, and (c) decide it's important enough to surface. Often it
# does! That is precisely the problem, "often" is not a safety property, and it degrades
# silently when you upgrade the model.
#
# The application layer computes it instead:

# %%
from aria.tools import get_tank_status

for warning in get_tank_status.invoke({"tag": "T-043"})["data_quality_warnings"]:
    show(f"  ⚠  {warning}")

# %% [markdown]
# Tested, ordered, cited, and true on every single call. `tests/test_repository.py`
# asserts both warnings appear *and* that they appear in a fixed order, because Module 2
# is going to write an evaluator that checks the agent surfaced them, and that evaluator
# shouldn't drift run to run.
#
# > **Anything you can compute, compute. Don't ask the model to notice.**

# %% [markdown]
# ### Now expose it over MCP
#
# The application is a library. To make the boundary real, put it behind a protocol:

# %%
print(Path("aria_mcp/server.py").read_text().split('@mcp.tool()')[1].split('"""')[1][:700])

# %% [markdown]
# Note what that docstring is doing. **Tool descriptions are prompt engineering.** They're
# sent to the model on every single turn, and unlike your system prompt they're attached
# to the thing they describe. Compare with v1's `"""Search procedures."""`.
#
# Write them for a competent new hire in a hurry: what it does, when to use it, when
# *not* to, and what failure looks like.
#
# Connect the agent to the real server over stdio:

# %%
from aria.tools import mcp_tools

mcp_toolset = await mcp_tools(transport="stdio")

print(f"connected: {len(mcp_toolset)} tools from the MCP server\n")
for t in mcp_toolset:
    print(f"  {t.name:20} {t.description.splitlines()[0][:64]}")

# %% [markdown]
# Those descriptions come from **the server**, not from the agent repo. The application
# owns its own contract. Update a tool description on the server and every consumer picks
# it up on reconnect, this agent, a different agent, someone's IDE, a human with an MCP
# client.
#
# ### Why bother with the protocol boundary?
#
# You can absolutely pass `@tool` functions straight to `create_agent`, and for a
# prototype you should. Four reasons to add the boundary as you productionize:
#
# 1. **It forces the separation to be real.** Once there's a process boundary, it's
#    *impossible* to reach into agent state from application code. That coupling is very
#    hard to unpick later.
# 2. **Independently testable and deployable.** The MCP server is a service. Version it,
#    load-test it, monitor it, page on it.
# 3. **One application, many consumers.** Write the plant-data contract once.
# 4. **A place to enforce authorization.** Per-caller permissions belong on the
#    application side where they can be audited, not in a prompt.
#
# ⚠️ **One catch worth knowing:** each MCP tool call over stdio here spawns a session. For
# eval loops running 200 examples with 8-way concurrency, use the in-process path
# (`local_tools()`), same repository, same behavior, no subprocess. `tests/
# test_tool_parity.py` asserts the two surfaces can't drift.

# %% [markdown]
# ---
# ## 4. See the pitfalls bite
#
# Two agents, same model, same data, same question. The question is the realistic one:
# a user who says "P-101" when they mean "P-101A".

# %%
from langgraph.checkpoint.memory import InMemorySaver

from aria.agent_v1 import build_agent as build_v1
from aria.agent_v2 import build_agent as build_v2

QUESTION = "What do I need to do before pulling the seal on P-101?"

if HAS_LLM:
    v1 = build_v1()
    answer_v1 = v1.invoke({"messages": [{"role": "user", "content": QUESTION}]})
    print("=" * 78, "\nARIA v1\n", "=" * 78, sep="")
    show(answer_v1["messages"][-1].content)
else:
    print("Skipped: no model key.")

# %%
if HAS_LLM:
    v2 = build_v2(checkpointer=InMemorySaver())
    answer_v2 = v2.invoke(
        {"messages": [{"role": "user", "content": QUESTION}]},
        config={"configurable": {"thread_id": "module-1-demo"}},
    )
    print("=" * 78, "\nARIA v2\n", "=" * 78, sep="")
    show(answer_v2["messages"][-1].content)

# %% [markdown]
# **What to look for.** Results vary run to run (that's the point of the whole workshop)
# but the pattern is consistent:
#
# - **v1** typically either says P-101 doesn't exist, or answers about a generic pump with
#   no citation. Both are wrong; the second is worse because it looks right.
# - **v2** recovers from the ambiguous tag, cites `SOP-MECH-108 Rev 3` and
#   `SOP-LOTO-014 Rev 7`, and flags that seal work has a hard prerequisite on isolation.
#
# Count the tool calls in each trajectory:

# %%
if HAS_LLM:
    for label, result in (("v1", answer_v1), ("v2", answer_v2)):
        calls = [
            (c["name"], c["args"])
            for m in result["messages"]
            for c in (getattr(m, "tool_calls", None) or [])
        ]
        print(f"\n{label}: {len(calls)} tool calls")
        for name, args in calls:
            print(f"   {name}({', '.join(f'{k}={v!r}' for k, v in args.items())})")

# %% [markdown]
# If tracing is on, open these in LangSmith now. Module 3 lives in that UI, and it helps
# to have seen it once. The v2 run carries metadata the middleware attached:
# `answer_has_citation`, `tool_args_corrected_count`, and friends. We alert on those in
# Module 3.

# %% [markdown]
# ---
# ## 5. Practice 2. Move rules from the prompt into middleware
#
# ### The prompt is not an enforcement mechanism
#
# Here's v1's system prompt:

# %%
from aria.agent_v1 import SYSTEM_PROMPT as V1_PROMPT

print(V1_PROMPT)

# %% [markdown]
# Read it as a list of things that will each be true *most* of the time:
#
# | Prompt rule | Reality |
# |:--|:--|
# | "ALWAYS cite the procedure ID and revision" | ~90%. That's one unsourced safety answer in ten. |
# | "NEVER show your reasoning" | The model isn't leaking on purpose, so asking nicely doesn't address the cause. |
# | "Do not use more than 4 tool calls" | The model cannot reliably count its own tool calls. It has no dependable access to that state. |
# | "Always mention if data might be unreliable" | Depends on it noticing `"atg_status": "suspect"` in a JSON blob. |
#
# And you pay for all of it in tokens, on every turn, forever.
#
# ### Middleware is the enforcement mechanism
#
# Middleware = hooks that run at fixed points in the agent lifecycle, whether or not the
# model cooperates:
#
# | Hook | Runs |
# |:--|:--|
# | `before_agent` / `after_agent` | Once per invocation |
# | `before_model` / `after_model` | Around every model call |
# | `wrap_model_call` | *Around* each model call: can retry, swap models, rewrite the request |
# | `wrap_tool_call` | *Around* each tool call: can inspect args, correct them, or short-circuit |
#
# ```
#   prompt:      "Always cite the procedure id and revision."     ← a request
#   middleware:  check the answer; flag it if the citation is absent  ← a guarantee
# ```
#
# **The heuristic: if a rule can be expressed in code, it belongs in a hook.** ARIA's v2
# prompt is *shorter* than v1's, and it's more reliable. That's the tell.
#
# Here's the whole stack:

# %%
from aria.agent_v2 import middleware_stack

for i, mw in enumerate(middleware_stack(), 1):
    doc = (type(mw).__doc__ or "").strip().splitlines()
    print(f"{i}. {type(mw).__name__:32} {doc[0] if doc else ''}"[:100])

# %% [markdown]
# Five of those are built in. **Go shopping in the built-in list before you write
# anything**, `SummarizationMiddleware`, `PIIMiddleware`, `HumanInTheLoopMiddleware`,
# `ToolRetryMiddleware`, `ModelFallbackMiddleware`, `LLMToolSelectorMiddleware`,
# `ContextEditingMiddleware`, `TodoListMiddleware`. Each one is a problem you don't have
# to solve.
#
# The three custom ones target specific *observed* production failure modes. Let's run
# each in isolation, no model needed, which is itself the argument for this approach.

# %% [markdown]
# ### Failure mode 1: reasoning leaking into the answer
#
# The user asks a question and gets back a wall of first-person deliberation: *"Okay, so
# the user is asking about lockout/tagout for P-101A. Let me search the procedure library.
# I should probably also check..."*
#
# This is *inconspicuous* in your own testing, which is why it ships. You can read it, and
# it's roughly correct, so it survives review. Your users hate it.
#
# It happens when reasoning tokens aren't separated into the right channel, when a prompt
# says "think step by step" without saying where, or when a provider changes how reasoning
# is returned. **That last one can start happening on a Tuesday without you shipping
# anything**, which is exactly why it belongs in code.

# %%
from langchain.messages import AIMessage

from aria.middleware import ReasoningLeakMiddleware

leaked = AIMessage(
    content=(
        "<thinking>The user wants LOTO for P-101A. I should search first, then check "
        "the equipment record for applicable procedures.</thinking>\n\n"
        "Per SOP-LOTO-014 Rev 7, obtain a signed work permit from the Unit Operator "
        "before any isolation begins."
    ),
    id="demo-msg",
)

update = ReasoningLeakMiddleware().after_model({"messages": [leaked]}, None)
print("BEFORE:\n")
show(leaked.content)
print("\nAFTER:\n")
show(update["messages"][0].content)

# %% [markdown]
# Note the design decision inside that middleware, because it's the transferable part:
#
# - **Tag-delimited blocks** (`<thinking>...</thinking>`) are **removed**. Zero false
#   positives, those markers never legitimately appear in an answer about pump seals.
# - **Reasoning-sounding prose** is **flagged, never deleted**. A regex confident enough to
#   delete prose is confident enough to delete a real answer.
#
# > Automate the intervention where you have certainty. Route to measurement where you
# > don't.
#
# The flag becomes `metadata.reasoning_leak_detected` on the trace, a number Module 3
# alerts on and Module 2 writes a proper evaluator for.

# %% [markdown]
# ### Failure mode 2: right tool, wrong arguments
#
# The other one you'll see constantly. `get_equipment("p-101a")` instead of `"P-101A"`.
# `get_equipment("T-042")` when tanks live behind a different tool. `limit=20` when the
# tool accepts 1–5.

# %%
from langchain.messages import ToolMessage
from langchain.tools.tool_node import ToolCallRequest

from aria.middleware import ToolArgumentGuardMiddleware

guard = ToolArgumentGuardMiddleware()


def demo(tool_name: str, args: dict) -> None:
    seen: dict = {}

    def handler(req):
        seen.update(req.tool_call["args"])
        return ToolMessage(content="<tool ran>", tool_call_id="c1")

    request = ToolCallRequest(
        tool_call={"name": tool_name, "args": args, "id": "c1"},
        tool=None,
        state={"messages": []},
        runtime=None,
    )
    out = guard.wrap_tool_call(request, handler)
    verdict = f"corrected → {seen}" if seen else f"short-circuited → {out.content}"
    print(f"  {tool_name}({args})\n      {verdict}\n")


demo("get_equipment", {"tag": " p-101a "})
demo("search_procedures", {"query": "loto", "limit": 20})
demo("get_equipment", {"tag": "T-042"})
demo("get_tank_status", {"tag": "tank 042"})

# %% [markdown]
# Three tiers of response, and choosing between them is the actual engineering:
#
# 1. **Fix it in code** where correct behavior is unambiguous. Case normalization is not a
#    judgment call; the model shouldn't spend a turn learning it.
# 2. **Return a corrective message** where a fix would be a guess. Routing a tank tag to
#    the tank tool is *arguably* a fix, but silently changing which tool ran makes the
#    trace lie about what happened. So we tell the model and let the next turn be honest.
# 3. **Add a sentence to the prompt.** Last resort. Costs tokens every turn and works most
#    of the time, which is the worst reliability profile available.

# %% [markdown]
# ### Failure mode 3: advice without a source
#
# ARIA's contract: *any answer giving procedural guidance must carry a citation in the
# form `SOP-XXX-NNN Rev N`.* That's a regex. No model needed, costs nothing, never flakes.
#
# The interesting decision is that `AnswerContractMiddleware` **does not rewrite the
# answer**:

# %%
from aria.middleware import CITATION, PROCEDURE_ID, AnswerContractMiddleware

for answer in [
    "Per SOP-LOTO-014 Rev 7, lock out at the MCC breaker.",
    "See SOP-LOTO-014 for isolation requirements.",
    "You'll want to isolate the pump and bleed the casing first.",
]:
    mentions = bool(PROCEDURE_ID.search(answer))
    cited = bool(CITATION.search(answer))
    verdict = "OK" if cited else ("procedure id but NO revision" if mentions else "no source at all")
    print(f"  [{verdict:28}] {answer[:60]}")

# %% [markdown]
# Note the middle case. `"See SOP-LOTO-014"` is **not** a citation. Revisions change what a
# procedure *says*, SOP-CSE-003 Rev 12 tightened blinding requirements over Rev 11, so an
# unversioned reference can send someone to instructions that are no longer correct.
#
# Why not auto-fix? Two reasons:
#
# - **We can't invent a citation.** Appending a plausible-looking source to unsourced
#   advice would manufacture exactly the false confidence the contract exists to prevent.
# - **A measured violation beats a patched one.** This middleware is what makes "our agent
#   gave unsourced safety advice" a number on a dashboard with an alert on it, instead of
#   something a user discovers.
#
# This is the seam between *building* an agent and *operating* one, and it's the handoff
# to Modules 3 and 4.

# %% [markdown]
# ---
# ## 6. Practice 3. Always set limits
#
# Non-negotiable, and the first thing to add to any agent.

# %%
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware

print("""
    ModelCallLimitMiddleware(run_limit=25, thread_limit=120)
    ToolCallLimitMiddleware(run_limit=40,  thread_limit=200)

    run_limit     · per invocation, resets each user turn
    thread_limit  · across a whole conversation (needs a checkpointer)
    exit_behavior: 'continue' (block + tell the model) | 'error' | 'end'
""")

# %% [markdown]
# A confused agent in a retry loop is not hypothetical. The usual trigger is a tool that
# returns something the model reads as *"almost worked, try again"*, which is precisely
# what v1's `return "{}"` creates. **The two pitfalls compound**: a bad API surface makes
# the runaway loop *likely*, and no limit makes it *unbounded*.
#
# Without a cap, the ceiling on your spend is your provider's rate limit. You find out
# from Finance.
#
# **Set them generously.** The goal isn't to constrain normal behavior, a job package
# legitimately takes a dozen model calls. It's to make the pathological case *terminate*.
# A limit at 3× your p99 costs nothing and caps your downside.
#
# `build_agent(include_limits=False)` exists so you can watch what unbounded looks like.
# We do that once, on purpose, and never again.
#
# ### Also worth wiring on day one
#
# | | |
# |:--|:--|
# | `ToolRetryMiddleware` | Transient failures degrade into a worse answer, not a 500. Retries count against your tool cap, which is correct. |
# | `SummarizationMiddleware` | Long conversations. Summarize with the **cheap** model: easiest cost win in a long-running agent, and Module 2 measures whether quality moved. |
# | `HumanInTheLoopMiddleware` | The moment your agent can *write* anything. Needs a checkpointer. |
# | `ModelFallbackMiddleware` | Provider outage → degraded, not down. |

# %% [markdown]
# ---
# ## 7. Practice 4. Choose the model deliberately
#
# The honest state of things:
#
# - Frontier Anthropic and OpenAI models are the most capable and the most expensive.
# - **The gap to good open-weight models has narrowed much faster than most people's
#   mental model has updated.**
# - If cost is a real constraint, **GLM 5.2** is the one to look at, open-weight, strong
#   on tool use, roughly an order of magnitude cheaper per token. A genuine option for the
#   high-volume, well-scoped parts of your workload.
#
# And then the part that matters more than any of that:

# %%
from aria.agent_v2 import MODEL_CANDIDATES

print(json.dumps(MODEL_CANDIDATES, indent=2))
print("""
    "Cost-effective" is a property of a model ON A TASK, not a property of a model.

    A model that scores 90% of frontier on a public benchmark might be 99% as good
    on your narrow, well-scoped task, or 40%. You cannot know which without
    measuring on your own data.

    Which is Module 2.  ───────────────────────────────────────────────▶
""")

# %% [markdown]
# ---
# ## 8. Where the harness earns its keep
#
# Everything so far would work with `create_agent`. So why `create_deep_agent`?
#
# The harness adds a planning tool (`write_todos`), a filesystem, subagent delegation, and
# context compression. That's real machinery and it isn't free, those tool schemas cost
# tokens on every turn.
#
# It earns its place when the *work* has the right shape. ARIA has two very different jobs:
#
# 1. **Quick lookup**, *"what's the LOTO procedure for P-101A?"* One or two tool calls.
#    `create_agent` + this same middleware would be the right call.
# 2. **Job package authoring**: *"put together the work package for pulling the seal on
#    P-101A."* Resolve the asset, pull four interacting procedures, reconcile them
#    (SOP-MECH-108 has a hard prerequisite on SOP-LOTO-014), check current status, write a
#    structured document.
#
# Job 2 is what the harness is for. Watch the todo list and the filesystem get used:

# %%
if HAS_LLM:
    packager = build_v2(checkpointer=InMemorySaver())
    out = packager.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Put together a job package for replacing the mechanical seal on "
                        "P-101A. Write it to work_package_P-101A.md."
                    ),
                }
            ]
        },
        config={"configurable": {"thread_id": "job-package-1"}},
    )

    used = [
        c["name"]
        for m in out["messages"]
        for c in (getattr(m, "tool_calls", None) or [])
    ]
    print(f"tool calls ({len(used)}): {used}\n")
    print("harness tools used:", sorted({t for t in used if t in {
        "write_todos", "write_file", "read_file", "edit_file", "ls", "task"
    }}) or "none")
    print("\n" + "=" * 78)
    show(out["messages"][-1].content)
else:
    print("Skipped: no model key.")

# %% [markdown]
# > **Choosing the harness is a judgment about the shape of the work, not a default.** If
# > ARIA only ever did quick lookups, paying for the harness would be waste.

# %% [markdown]
# ---
# ## Module 1 recap
#
# **The one idea:** you get one irreducibly non-deterministic component. Make everything
# around it as deterministic as you can.
#
# | Practice | v1 | v2 |
# |:--|:--|:--|
# | Start from outcomes | Capability-first | Workflows specified as a dataset, first |
# | App logic separate, behind MCP | Inline with the prompt | `aria_mcp`, 168 tests, no LLM |
# | Errors carry the fix | `return "{}"` | Names near-miss tags |
# | Output bounded & deterministic | Whole JSON blob | Truncated, stable order, cited |
# | Business rules in code | "hope it notices" | Computed warnings |
# | Rules as middleware | 11 prompt bullets | 7 middleware, shorter prompt |
# | Limits | None | Model + tool caps |
# | Thread identity | None | Checkpointer + `thread_id` |
# | Model choice | Whatever was in the tutorial | Measured (Module 2) |
#
# **The two failure modes you'll actually hit**, from experience:
# 1. Reasoning leaking into the answer, inconspicuous, ships easily, users hate it.
# 2. Wrong tool or wrong arguments, usually fixable deterministically.
#
# **And the honest limit of this module:** there is only so much you can do before you
# ship. You cannot enumerate your agent's failure modes at your desk. Some of them only
# exist in contact with real users.
#
# That's not a reason to ship carelessly, it's a reason to build the machinery that finds
# them for you.
#
# ### → Module 2: prove it works, and find out what it costs.

# %% [markdown]
# ---
# ### Exercises, if you finish early
#
# 1. **Find pitfall 4 in the wild.** Run v1 with `"p-101a"` (lowercase). Then `"Tank 42"`.
#    Then `"the crude charge pump"`. How many of these does v1 answer wrongly *without any
#    indication that something went wrong*?
#
# 2. **Write a middleware.** ARIA should refuse to answer about equipment whose status is
#    `shutdown_for_turnaround` without saying so first (V-206 qualifies). Write it as
#    `after_model`, or as `wrap_tool_call` on `get_equipment`. Which hook is right, and why?
#
# 3. **Break the parity test.** Add an argument to `search_procedures` in
#    `aria_mcp/server.py` but not in `aria/tools.py`. Run
#    `pytest tests/test_tool_parity.py`. This is the test that stops your evals from
#    measuring a tool surface that no longer exists in production.
#
# 4. **Delete a limit.** `build_v2(include_limits=False)`, then ask something ambiguous
#    enough to cause thrashing. Watch the tool call count. Now imagine it's 3am.
