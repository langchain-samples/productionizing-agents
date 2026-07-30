# Productionizing Agents

**Evaluation, monitoring, and observability for agents you actually ship.**

Duration: 2 hours · Audience: engineers already building agents · Stack: Python + LangSmith

📖 **[Your Agent Needs a Test Suite](https://langchain-samples.github.io/productionizing-agents/docs/writing-agent-evals.html)**, the written guide, if you'd rather read than run.
🖥️ **[The deck](https://langchain-samples.github.io/productionizing-agents/slides/index.html)**, 38 slides, four modules.

---

## What you'll walk away with

By the end of this session you will have taken one agent from "works on my laptop" to
"I would let this face a customer," and you'll have the artifacts to prove it:

| Module | Topic | You will have built |
| :----- | :---- | :------------------ |
| **1** (30 min) | Agent development best practices & common pitfalls | An agent, built the way you'd build one today: application logic behind a tested MCP server, rules enforced by middleware instead of requested by the prompt, and spend limits |
| **2** (40 min) | Deploy cost-effective, reliable agents with evals | A five-level test suite (179 deterministic tests + datasets, judges, and simulated users), a live red→green TDD cycle, and a measured answer to "can we run this on a cheaper model?" |
| **3** (20 min) | Protect your agent's reputation with monitoring & alerting | A shipped agent behind a REST endpoint, online evaluators, alert rules, and a real safety-relevant bug that the eval suite passed over |
| **4** (10 min) | Continually improve your agent 24/7 with self-improving loops | Automations routing production failures to human review, assertions that turn a reviewer's English into a regression test, and an aligned judge |

The four modules are cumulative. Module 1 is the only one where we build. Module 2
evaluates what Module 1 produced. Module 3 monitors it in production. Module 4 feeds
production failures back into Module 2's dataset. That loop *is* the lesson.

Module 1's organizing idea, which the other three depend on:

> You are handed one irreducibly non-deterministic component. Everything you build around
> it should be as deterministic as you can make it.

Middleware, a tested tool surface, and call limits are all applications of that. Evals,
monitoring, and self-improvement are how you handle what's left over.

---

## The running example

Every module uses the same agent: **ARIA** (Asset Reliability Information Assistant), a
maintenance and HSE procedure assistant for a refinery. It answers questions like:

- *"What's the lockout/tagout procedure for P-101A before we pull the seal?"*
- *"Is nitrogen purge required before entering V-205?"*
- *"What's the current level in Tank 42 and when was it last inspected?"*

We picked this domain deliberately. It is **safety-adjacent**, which makes the stakes of
every lesson concrete: a hallucinated procedure isn't a bad demo, it's an incident. That
turns abstract ideas like "groundedness" and "refusal behavior" into requirements you can
actually write an evaluator for.

Everything runs against local fixture data in `data/`, no real refinery systems, no real
plant data, no network dependencies beyond your LLM provider and LangSmith.

---

## Quick start

```bash
git clone <this repo> && cd agent-evals-workshop

# 1. Install (uv recommended; pip works too)
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# then edit .env, you need a LangSmith key and one model provider key

# 3. Verify your setup (do this BEFORE the session)
python scripts/preflight.py

# 4. Build the notebooks
python scripts/build_notebooks.py

# 5. Go
jupyter lab notebooks/
```

`scripts/preflight.py` checks your keys, hits LangSmith, makes one cheap model call, and
tells you exactly what's missing. If it prints `ALL CHECKS PASSED` you're ready.

Full setup detail, including air-gapped / no-LangSmith fallbacks: **[SETUP.md](SETUP.md)**

---

## Repository layout

```
├── FACILITATOR_GUIDE.md    Minute-by-minute run of show. Read this if you're teaching.
├── SETUP.md                Prereqs, troubleshooting, offline fallbacks.
├── slides/index.html       The deck (reveal.js, just open it in a browser).
│
├── aria_mcp/               THE APPLICATION, no langchain import anywhere in it
│   ├── repository.py         Read side: data access, validation, business rules
│   ├── work_orders.py        Write side: work orders, shutdown requests
│   └── server.py             The same API over MCP (stdio or http)
│
├── aria/                   THE AGENT, prompt, middleware, model, tool wiring
│   ├── agent_v1.py           Deliberately flawed. Module 1 dissects it.
│   ├── agent_v2.py           Deep agent + middleware stack. Modules 2-4 use this.
│   ├── middleware.py         Custom guards for observed model failure modes
│   ├── tools.py              Two transports (in-process, MCP) over one application
│   └── graph.py              The deployable entry point (langgraph.json points here)
│
├── evals/                  THE TEST SUITE, five levels
│   ├── harness.py            Level 0: assert on the assembled prompt. No LLM.
│   ├── mocking.py            Clone a tool's contract, script its behavior
│   ├── evaluators.py         Code assertions + LLM judges + assertion grading
│   ├── datasets.py           Smoke, scripted, and the hand-written TDD spec
│   ├── runner.py             aevaluate wiring and the model bake-off
│   ├── test_stateful.py      Level 3: real state, pytest + LangSmith
│   └── simulate.py           Level 4: an LLM playing the user
│
├── tests/                  179 tests. No API key, no model, ~6 seconds.
│   ├── test_repository.py      The read surface (44)
│   ├── test_work_orders.py     The write surface
│   ├── test_middleware.py      The custom guards (25)
│   ├── test_harness_context.py Level 0, what the model actually receives
│   ├── test_evaluators.py      Tests for the tests
│   └── test_tool_parity.py     Stops the two transports drifting
│
├── notebooks/              What participants run (generated, see below)
│   ├── 01_building_an_agent.ipynb
│   ├── 02_evals.ipynb
│   ├── 03_monitoring.ipynb
│   └── 04_self_improving.ipynb
├── notebook_src/           Source for the above, jupytext percent format
│
├── frontend/index.html     Local chat UI + approval gate. No build step.
├── langgraph.json          `langgraph dev` / `langgraph deploy`
│
├── scripts/
│   ├── preflight.py          Run this first. Nine checks, prints the fix.
│   ├── build_notebooks.py    notebook_src/*.py -> notebooks/*.ipynb
│   ├── generate_traffic.py   Production traffic (+ --engine-warmup)
│   ├── setup_automations.py  Annotation queue + routing rules
│   └── build_blog.py         docs/*.md -> a self-contained styled HTML page
│
├── data/                   Fixture data: procedures, equipment, tank readings
└── .github/workflows/evals.yml   Three-tier eval gate in CI
```

### Why `notebook_src/` and a build step?

The notebooks are generated from plain `.py` files in `notebook_src/` (jupytext "percent"
format: `# %%` marks a code cell, `# %% [markdown]` a prose cell). Reason: `.ipynb` is
JSON with embedded outputs, which makes it miserable to review and worse to diff. The
`.py` sources are the thing you edit and version; the notebooks are a build artifact.

```bash
python scripts/build_notebooks.py           # build all
python scripts/build_notebooks.py 02        # rebuild just module 2
```

The builder is stdlib-only: no jupytext dependency required.

---

## Running the modules without LangSmith

Set `LANGSMITH_TRACING=false` and the agent runs normally. More usefully: **all 179 tests
need no LangSmith key and no model key at all**, and that includes the whole of Level 0,
the middleware unit tests and the assertions on the assembled prompt. Every notebook gates
its model-backed cells behind `HAS_LLM` / `HAS_LANGSMITH`, so nothing crashes; those cells
just print "Skipped."

Levels 1–4 need datasets and experiments, so they need a key. Modules 3 and 4 are inherently
platform features. Those participants should pair up, see
[SETUP.md](SETUP.md#no-langsmith-access) for the full capability matrix.

---

## For facilitators

Read **[FACILITATOR_GUIDE.md](FACILITATOR_GUIDE.md)**. It has the timing, the talking
points, the "if you're running behind, cut this" markers, and the pre-session checklist,
including the one thing you must do 24 hours ahead (seed production traffic so Modules 3
and 4 have real data to look at).
