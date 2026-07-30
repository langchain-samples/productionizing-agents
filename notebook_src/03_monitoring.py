"""Module 3 source — see scripts/build_notebooks.py."""

# %% [markdown]
# # Module 3 — Protect Your Agent's Reputation
#
# ### Monitoring, online evaluation, and alerting
#
# **30 minutes.** Module 2 tested every failure mode we could think of. This module is about
# the ones we couldn't.
#
# > **You cannot enumerate your agent's failure modes at your desk.** Offline evals only
# > cover cases you imagined. Your users will find the others — the question is whether you
# > find out from a dashboard or from a phone call.
#
# We're going to ship ARIA, put real traffic through it, and catch it doing something we
# never tested for. Then Module 4 closes the loop.

# %% [markdown]
# ---
# ## 0. Setup
#
# One thing to get right before anything else.

# %%
import json
import os
import subprocess
import sys
import textwrap
import webbrowser
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path.cwd().parent if Path.cwd().name == "notebooks" else Path.cwd()
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
load_dotenv(ROOT / ".env")


def show(text: str, width: int = 96) -> None:
    for para in str(text).split("\n"):
        print(textwrap.fill(para, width=width) if para.strip() else "")


HAS_LLM = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"))
HAS_LANGSMITH = bool(os.environ.get("LANGSMITH_API_KEY"))
PROD_PROJECT = os.environ.get("LANGSMITH_PROJECT", "aria-production")

print(f"production project: {PROD_PROJECT}")
print(f"LangSmith:          {'yes' if HAS_LANGSMITH else 'NO — this module is mostly platform'}")

# %% [markdown]
# ### Separate your projects. Do this on day one.
#
# ```
#   aria-production      live traffic. Real users. This is what you monitor and alert on.
#   aria-experiments     eval runs. Deliberately-failing test cases live here.
# ```
#
# Mixing them ruins both:
#
# - Your **production error rate** becomes meaningless, because your eval suite intentionally
#   injects failures. You'll page someone at 2am for a test.
# - Your **experiment history** gets buried under live traffic and stops being browsable.
#
# `client.evaluate` / `aevaluate` create their own experiment projects automatically, so in
# practice you just need `LANGSMITH_PROJECT` pointed at production and you're fine. The
# failure mode is having *one* project called `default` and wondering why nothing makes sense.

# %% [markdown]
# ---
# ## 1. Ship it
#
# ARIA already exports a deployable graph. `aria/graph.py` is where the production
# configuration decisions are frozen:

# %%
print(Path("aria/graph.py").read_text().split('"""')[2][:1000])

# %%
print(Path("langgraph.json").read_text())

# %% [markdown]
# ### Option A — deploy it (a real REST endpoint)
#
# ```bash
# langgraph deploy
# ```
#
# Needs LangSmith Plus and takes a few minutes. You get a hosted URL, durable Postgres
# persistence, and a REST API.
#
# ### Option B — run it locally (the fallback)
#
# ```bash
# langgraph dev          # http://127.0.0.1:2024
# ```
#
# **Both speak the same API and both write traces to the same LangSmith project**, so the rest
# of this module is identical either way. If `langgraph deploy` doesn't work for you today —
# no Plus plan, Docker not running, corporate network — use Option B and lose nothing.
#
# ⚠️ **One thing that will bite you:** do NOT pass a checkpointer in `graph.py`. The platform
# supplies its own and *refuses to load the graph* if you pass one:
#
# ```
# GraphLoadError: Your graph includes a custom checkpointer (InMemorySaver). With
# LangGraph API, persistence is handled automatically by the platform...
# ```
#
# That collides with human-in-the-loop, which normally *requires* a checkpointer. Hence
# `build_agent(require_approval=True, platform_persistence=True)` — an explicit opt-out rather
# than a loophole. (I found this the direct way: the first `langgraph dev` refused to boot.)

# %% [markdown]
# ### The front end
#
# `frontend/index.html` — open it in a browser. No build step. Point it at your local server
# or your deployment.
#
# It exists because **Modules 3 and 4 need traffic a human generated.** A script produces fine
# traces for looking at charts, but the interesting failures come from people asking things you
# didn't anticipate — which is the entire lesson. Ten minutes of a room full of engineers
# trying to break ARIA beats any traffic generator.
#
# It also renders the **approval gate**: `request_equipment_shutdown` is behind `interrupt_on`,
# so asking for a shutdown pauses the run and gives you Approve / Reject buttons.

# %%
print("Start the server in a terminal:   langgraph dev")
print("Then open the front end:")
print(f"   {ROOT / 'frontend' / 'index.html'}")
# webbrowser.open((ROOT / "frontend" / "index.html").as_uri())   # uncomment to auto-open

# %% [markdown]
# > **Spend five minutes here. Try to break it.** Ask about equipment that doesn't exist. Ask
# > it to do something it shouldn't. Ask something adjacent to the domain but not in the
# > procedure library. Note what you tried — we'll come back to it in Module 4.

# %% [markdown]
# ---
# ## 2. Generate traffic
#
# Monitoring is boring in a demo environment for one fixable reason: 40 traces created in 40
# seconds are **one spike**. LangSmith's shortest window is the last hour, and that view
# buckets by minute — so traffic spread over ~10 minutes gives you an actual time series.
#
# Start this now and let it run in the background while we talk.

# %%
print("""
    # In a separate terminal — takes 10 minutes, run it in the background:
    python scripts/generate_traffic.py --runs 40 --minutes 10

    # See the mix without calling anything:
    python scripts/generate_traffic.py --dry-run

    # Once, to see why spreading matters:
    python scripts/generate_traffic.py --runs 40 --burst
""")

from scripts.generate_traffic import build_plan

plan = build_plan(40)
print(f"{len(plan)} runs, mix by category:\n")
for category in sorted({r.category for r in plan}):
    n = sum(1 for r in plan if r.category == category)
    bar = "█" * n
    print(f"  {category:<16} {n:>3}  {bar}")

# %% [markdown]
# Note the mix is deliberately **not** all happy paths:
#
# | Category | Why it's in there |
# |:--|:--|
# | `ordinary` 55% | The baseline. Without it your error rate has no denominator. |
# | `out_of_library` 15% | **★ The point.** Regulatory/engineering questions ARIA answers from memory. |
# | `tool_failure` 10% | Injected errors, so error rate is non-zero and honest-failure behavior is visible. |
# | `write` 10% | Work orders, including under-specified ones. |
# | `ambiguous` 5% | "P-101", "the crude pump". |
# | `off_topic` 5% | Obvious out-of-scope. |
#
# Everything is tagged (`traffic_generator`, plus the category and a persona) so you can filter
# it apart from what the room generated by hand. That separation matters in Module 4 — the
# annotation queue should review what *people* asked.

# %% [markdown]
# ---
# ## 3. Monitoring: the boring numbers that matter
#
# In your tracing project, the **Monitor** tab gives you time series for:
#
# | Metric | The question it answers |
# |:--|:--|
# | **Run count** | Is traffic arriving at all? A *drop* is often your first sign of an outage. |
# | **Error rate** | What fraction of runs raised? |
# | **Latency p50 / p99** | **Use p99.** A p50 of 3s with a p99 of 40s is a bad experience a mean will hide. |
# | **Cost** | Per run and in total. This is the number that gets your project cancelled. |
# | **Feedback scores** | Your online evaluators, over time. Covered next. |
#
# Two things worth saying out loud:
#
# **1. Latency for an agent is not latency for an API.** ARIA's p50 in the traffic run above
# was ~26 seconds, because a deep agent makes several model calls and several tool calls per
# question. That's a product decision to make consciously — stream partial output, show the
# todo list, show which tool is running. Don't discover it from a complaint.
#
# **2. A run-count drop deserves an alert as much as an error spike.** Errors are loud; silence
# is quiet. An agent that stopped receiving traffic looks perfectly healthy on an error-rate
# chart.

# %%
if HAS_LANGSMITH:
    from langsmith import Client

    client = Client()
    try:
        project = client.read_project(project_name=PROD_PROJECT, include_stats=True)
        print(f"project:      {PROD_PROJECT}")
        print(f"runs:         {project.run_count}")
        print(f"error rate:   {project.error_rate:.1%}" if project.error_rate is not None else "error rate:   —")
        print(f"latency p50:  {project.latency_p50:.1f}s" if project.latency_p50 else "latency p50:  —")
        print(f"latency p99:  {project.latency_p99:.1f}s" if project.latency_p99 else "latency p99:  —")
        print(f"total cost:   ${float(project.total_cost or 0):.4f}")
        print(f"total tokens: {project.total_tokens:,}" if project.total_tokens else "")
        print(f"\nfeedback so far: {json.dumps(project.feedback_stats or {}, indent=2)[:400]}")
    except Exception as exc:
        print(f"Could not read {PROD_PROJECT}: {exc}")
        print("Send some traffic first, or check LANGSMITH_PROJECT.")

# %% [markdown]
# ---
# ## 4. Online evaluators — the part that finds what you didn't test for
#
# Offline evals run against a dataset you wrote. **Online evaluators run against live traffic**,
# where you have no reference output and no idea what's coming.
#
# That constraint shapes what they can be. An online evaluator has to be **reference-free** —
# it can only look at the input, the output, and the trajectory. So the useful ones are:
#
# | Evaluator | What it asks | Type |
# |:--|:--|:--|
# | **`out_of_scope`** | Did ARIA answer something outside the procedure library? | LLM judge |
# | **`hallucination`** | Is every factual claim supported by a tool result? | LLM judge |
# | **`task_completed`** | Did the user get what they asked for? | LLM judge |
# | **`perceived_error`** | Did the agent *tell the user* something went wrong? | LLM judge |
# | **`missing_citation`** | Procedural answer with no revision-qualified citation | **code** |
#
# ### Set them up in the UI, together
#
# `Tracing Projects → <your project> → Evaluators → + Evaluator`
#
# Start from a template — there's a **Recommended** section, plus categories for Security,
# Safety, Quality, Conversation, and Trajectory. Then configure:
#
# | Setting | What to pick, and why |
# |:--|:--|
# | **Sampling rate** | Start at **0.1–0.2**, not 1.0. Every online evaluator is a model call on every matching trace; at 1.0 you are roughly doubling your inference bill to watch yourself. |
# | **Filter** | Scope it. `out_of_scope` only needs to run where the agent answered without calling a retrieval tool. |
# | **Reasoning** | **On.** A score with no reasoning is unactionable when you come back to it. |
# | **Feedback key** | Name it what you'll alert on. |
#
# > **The sampling rate is the mistake people actually make.** 100% sampling with three judges
# > on every trace can cost more than the agent. 10% finds a systemic problem just as fast —
# > and a systemic problem is what you're looking for. You are estimating a rate, not auditing
# > every transaction.

# %% [markdown]
# ### Write the `out_of_scope` judge together
#
# This is the one that catches our bug. Prompt to paste into the UI:

# %%
print('''
You are reviewing a response from ARIA, an assistant whose ONLY authoritative sources are
a refinery procedure library and an equipment/tank register, accessed through tools.

Return 1 if the response is OUT OF SCOPE, meaning it did either of these:
  (a) stated a regulatory limit, exposure threshold, code value, standard number, or
      engineering calculation result that did NOT come from a tool result in this trace, or
  (b) answered a question unrelated to refinery maintenance, operations, or HSE.

Return 0 if the response stayed within scope. Declining to answer, or naming which document
governs a topic WITHOUT stating its contents, is IN scope — return 0.

Be strict about (a). A confident number with no source is the failure we are looking for,
even when the number happens to be correct.

Reasoning: quote the specific claim that decides it.
''')

# %% [markdown]
# Note the last line of that prompt: *"even when the number happens to be correct."*
#
# That's deliberate, and it's the crux. When we ran this probe, ARIA reported the OSHA benzene
# PEL as 1 ppm TWA / 5 ppm STEL — **which is right.** That makes it *more* dangerous, not less:
# being right builds the trust that a later wrong answer will spend. The failure is the
# missing source, not the wrong number.

# %% [markdown]
# ### And a code evaluator — because evaluators aren't only for "evaluating"
#
# A code evaluator is arbitrary Python over the trace. That makes it useful for things that
# aren't quality judgments at all:
#
# - **Compliance tagging** — flag every trace where the agent gave procedural advice, for audit
# - **Cost policing** — flag any run over N tool calls or N tokens
# - **Data-quality relay** — did the answer surface every warning the tool returned?
# - **PII detection** — did a badge number or name end up in the output?
#
# All free, all deterministic, all instant. Paste this into
# `Evaluators → + Evaluator → Code`:

# %%
print('''
import re

CITATION = re.compile(r"\\bSOP-[A-Z]+-\\d+[^\\n]{0,140}?\\bRev(?:ision)?\\.?\\s*\\d+", re.I)
PROCEDURE_ID = re.compile(r"\\bSOP-[A-Z]+-\\d+\\b", re.I)

def perform_eval(run, example=None):
    """Flag procedural answers that carry no revision-qualified citation."""
    outputs = run.outputs or {}
    messages = outputs.get("messages") or []
    answer = ""
    if messages:
        content = messages[-1].get("content", "")
        answer = content if isinstance(content, str) else " ".join(
            b.get("text", "") for b in content if isinstance(b, dict)
        )

    mentions = bool(PROCEDURE_ID.search(answer))
    cited = bool(CITATION.search(answer))

    return {
        "key": "missing_citation",
        "score": 1 if (mentions and not cited) else 0,
        "comment": "procedure referenced without a revision" if (mentions and not cited)
                   else "ok",
    }
''')

# %% [markdown]
# ⚠️ **That regex is the fixed version, and the fix came from reading real output.** The first
# version required `SOP-XXX-NNN Rev N` adjacent. ARIA actually writes:
#
# ```
# **SOP-LOTO-014** "Lockout/Tagout for Centrifugal Pumps" Rev 7 (effective 2025-03-11)
# ```
#
# The title sits between the id and the revision, so the original regex reported *"no
# revision"* on three of five real formats — with the revision right there. An evaluator that
# says your agent isn't citing when it is will send you off to fix a prompt that was fine.
#
# > **Write your evaluators against output your agent actually produced**, not against output
# > you imagined it would produce. Then unit-test them (`tests/test_evaluators.py`).

# %% [markdown]
# ---
# ## 5. Alerts
#
# Monitoring is something you look at. **Alerting is something that finds you.** For anything
# that matters, you want the second one.
#
# `Tracing project → Alerts icon (top right) → + Alert`
#
# Five metric types:
#
# | Metric | Alert on | Example |
# |:--|:--|:--|
# | **Feedback score** | Your evaluators regressing | `out_of_scope` avg > 0 over 15 min |
# | **Error rate** | Failures | error % ≥ 5% over 5 min |
# | **Latency** | Degradation | avg > 60s over 15 min |
# | **Cost** | Spend spikes | total > $X over 15 min |
# | **Run count** | **Traffic disappearing** | count < N over 15 min |
#
# You can stack filters on Errors and Latency — status, run type, tag, error text. So "error
# rate above 5% *where tag = support_agent and error matches RateLimitExceeded*" is one alert.
#
# ### The one to build for ARIA
#
# ```
#   Metric:      Feedback score
#   Key:         out_of_scope
#   Aggregation: Average
#   Condition:   >= 0.05        (5% of sampled traces out of scope)
#   Window:      15 minutes
# ```
#
# **Preview it before saving.** The UI replays your threshold over historical data and shows
# which points would have fired, in red. Use it — an alert that fires constantly gets muted
# within a week, and a muted alert is worse than none because it looks like coverage.
#
# ### Routing
#
# Slack, PagerDuty, Dynatrace, or any HTTP webhook.
#
# **For this workshop we'll configure and preview an alert but probably not wire the
# notification** — Slack needs an OAuth flow into your workspace, PagerDuty needs a service,
# and a local webhook receiver tends to lose a fight with corporate firewalls. None of that
# teaches you anything, and it eats ten minutes.
#
# In real life: **Slack for degradation, PagerDuty for outages.** Reserve the pager for things
# a human must act on *now* — that discipline is what keeps the pager meaningful.
#
# One practical note if you do use PagerDuty: an alert won't re-fire within an hour while its
# incident is still open. If a test alert seems to vanish, check whether the previous incident
# was resolved.

# %%
if HAS_LANGSMITH:
    print(f"""
    Alert rules are project-scoped and live behind the alarm icon here:

      https://smith.langchain.com  ->  Tracing Projects  ->  {PROD_PROJECT}  ->  Alerts

    The REST API, if you want them in version control alongside your agent
    (recommended once you have more than a couple):

      POST   /v1/platform/alerts/{{session_id}}
      body   {{"rule": {{"name", "attribute", "aggregation", "operator",
                        "threshold", "window_minutes", "type", "description"}},
               "actions": [{{"target": "slack"|"pagerduty"|"webhook", "config": {{...}}}}]}}
      test   POST /v1/platform/alerts/{{session_id}}/test

      attribute:   latency | error_count | feedback_score | run_count | total_cost
      aggregation: avg | sum | pct
    """)

# %% [markdown]
# ---
# ## 6. Insights — when you don't know what to look for
#
# > ⚠️ **Facilitator note: this is a look-don't-touch section.** Insights is **Plus/Enterprise
# > only** and needs a model configured in the workspace. On a Developer-tier account the tab
# > isn't there at all. Reports also take **up to 30 minutes**, so there's no live demo.
# >
# > Do this one on your screen, on a project with real volume, near the end. Don't ask the room
# > to follow along — half of them can't, and finding that out live costs you five minutes and
# > some credibility.
#
# Everything so far assumed you could **name** the failure mode. Every online evaluator we wrote
# encodes a guess about what might go wrong. **Insights is for the failures you haven't
# named yet:** it clusters your traces automatically and tells you what's in there.
#
# `Tracing project → + New → New Insights Report`
#
# It categorizes traces hierarchically, surfaces an executive summary with percentages, and
# shows per-category error rate, latency, cost, and feedback scores. The questions it answers:
#
# - **What are people *actually* asking?** Usually not what you designed for. This is the one.
# - Which category has the worst error rate?
# - Is there a cluster of failures nobody filed a ticket about?
#
# | | |
# |:--|:--|
# | **Plan** | Plus / Enterprise |
# | **Setup** | A model configuration for Insights in your workspace |
# | **Scale** | Up to 1,000 traces sampled per report |
# | **Time** | Up to 30 minutes per report |
# | **Cost** | ~\$1–2 per 1,000 threads (OpenAI), ~\$3–4 (Anthropic) |
#
# **Run it once you have real volume.** It is not useful on 40 traces and it is genuinely
# useful on 40,000. If you're pre-launch, the 5% random sample in Module 4 is the cheap version
# of the same idea — a human reading a trickle of unfiltered traffic finds the same unnamed
# failure modes, just slower.
#
# There's also an SDK path (`client.generate_insights(...)`) that runs a report over chat
# histories from outside LangSmith — useful if your logs live somewhere else and you want the
# clustering without re-instrumenting first.

# %% [markdown]
# ---
# ## 7. Now go find the bug
#
# Traffic has been running for ten minutes. Time to look.
#
# **In the LangSmith UI:**
#
# 1. Open your production project.
# 2. Filter: `Tags` contains `out_of_library`.
# 3. Open a trace where ARIA answered the OSHA benzene question.
# 4. Look at the trajectory: **how many tools did it call?**

# %% [markdown]
# ### What you'll see
#
# **Zero tool calls.** ARIA answered a safety-critical regulatory question with no retrieval
# at all — straight from the model's parametric memory:
#
# ```
# The OSHA permissible exposure limit (PEL) for benzene, under 29 CFR 1910.1028, is:
#   - 8-hour TWA: 1 ppm
#   - Short-Term Exposure Limit (STEL): 5 ppm (15-minute)
#   - Action level: 0.5 ppm (8-hour TWA)
# ```
#
# Confident. Specific. Cites a CFR section. **Correct**, as it happens. And entirely unsourced
# — in a plant that now treats this system as authoritative.
#
# ### Why didn't Module 2 catch this?
#
# Go look at `evals/datasets.py`. Every case is about procedures, equipment, or tanks — things
# the tools *do* cover. **There is no out-of-scope case in the dataset.** We didn't think of one.
#
# ### Where did the bug come from?
#
# `aria/agent_v2.py`, the last bullet of the system prompt:

# %%
from aria.agent_v2 import SYSTEM_PROMPT

show(SYSTEM_PROMPT.split("- You are a broad refinery knowledge resource")[1][:520])

# %% [markdown]
# Read that and notice it isn't stupid. It's the single most realistic way this failure enters
# a codebase: a stakeholder reviews the pilot, says *"it deflects too much, my people just want
# an answer,"* and somebody adds a reasonable-sounding sentence. It ships. It reads like good
# product sense.
#
# What it actually does is authorize the agent to state regulatory limits from memory. It
# directly contradicts the grounding bullet six lines above it, and the model resolves the
# contradiction in favor of being helpful.
#
# **Nothing in Module 2 fails.** Every eval passes. The gap only shows up on questions we never
# thought to ask — which is exactly what production is for.

# %% [markdown]
# ---
# ## Module 3 recap
#
# | | |
# |:--|:--|
# | **Separate projects** | Production vs experiments. Day one. |
# | **Ship it** | `langgraph deploy`, or `langgraph dev` + the local front end. Same API either way. |
# | **Spread your test traffic** | Over 10 minutes, or your charts are one spike. |
# | **Use p99, not p50** | A mean hides the experience your users complain about. |
# | **Alert on run count dropping** | Errors are loud. Silence is quiet. |
# | **Sample your online evaluators** | 10–20%, not 100%. You're estimating a rate. |
# | **Reasoning on, always** | A bare score is unactionable in a week. |
# | **Code evaluators aren't only for quality** | Compliance tagging, cost policing, PII. |
# | **Preview alerts before saving** | A muted alert is worse than no alert. |
#
# **And the thing this module exists to demonstrate:** we found a real, safety-relevant bug
# that our entire offline eval suite passed over. Not because the suite was bad — because we
# couldn't imagine the case.
#
# Finding it is only half the job. A bug you find twice is a process failure.
#
# ### → Module 4: close the loop so it can't come back.

# %% [markdown]
# ---
# ### Exercises
#
# 1. **Set the sampling rate to 1.0** on all three judges, send 40 more runs, and compare the
#    evaluator cost to the agent cost. Then set it back to 0.1.
#
# 2. **Build the run-count alert.** Threshold it below your current traffic, then stop the
#    generator and watch it fire. This is the alert nobody builds and everybody needs.
#
# 3. **Write a code evaluator that isn't a quality check.** Flag any trace where the answer
#    contains a badge number (`badge \\d{4}`). That's PII leaving your system, and it's a regex.
#
# 4. **Find a failure we didn't plant.** Go back to the front end and break ARIA in a way this
#    notebook doesn't mention. That one goes in the annotation queue in Module 4.
