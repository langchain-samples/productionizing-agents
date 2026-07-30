# Setup

**Do this the day before the session, not on the morning of.** Ten minutes if it goes well.

---

## 1. Install

Python **3.11+**. `uv` is faster; `pip` is fine.

```bash
git clone <this repo> && cd agent-evals-workshop

uv venv --python 3.12 && source .venv/bin/activate
uv pip install -r requirements.txt
```

<details>
<summary>Plain pip / venv</summary>

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```
</details>

## 2. Configure

```bash
cp .env.example .env
```

Then edit `.env`. You need **two** things:

| | |
| :-- | :-- |
| `LANGSMITH_API_KEY` | smith.langchain.com → Settings → API Keys |
| One model provider | `ANTHROPIC_API_KEY` (recommended) or `OPENAI_API_KEY` |

⚠️ **Set `LANGSMITH_PROJECT` to something unique** — your name or initials. If everyone leaves
the default you'll all be looking at each other's traces in Module 3, and the monitoring
charts become meaningless.

```bash
LANGSMITH_PROJECT=aria-jcoad     # not aria-workshop-YOURNAME
```

## 3. Build the notebooks

The notebooks are generated from `notebook_src/*.py`. Stdlib only, no jupytext needed.

```bash
python scripts/build_notebooks.py
```

## 4. Verify

```bash
python scripts/preflight.py
```

Nine checks, cheapest first, and it keeps going after a failure so you get the whole picture
in one pass. It prints the fix for anything that's wrong.

**`ALL CHECKS PASSED` means you're ready.** Warnings are usually fine — read them.

## 5. Go

```bash
jupyter lab notebooks/
```

---

## Known gotchas

### `ImportError: cannot import name 'RequestContext' from 'mcp.shared.context'`

`mcp` resolved to 2.x, which removed a symbol `langchain-mcp-adapters` 0.3.x still imports.
`requirements.txt` pins `mcp>=1.9,<2.0`; if you installed something else:

```bash
pip install "mcp>=1.9,<2.0"
```

Preflight checks this explicitly, because the error surfaces deep inside the adapter and
looks like your code.

### `langgraph dev` exits immediately with `GraphLoadError`

If it mentions a custom checkpointer: the LangGraph platform supplies its own and **refuses
to load a graph that passes one**. `aria/graph.py` uses
`build_agent(require_approval=True, platform_persistence=True)` instead. Don't add a
checkpointer there.

### Port 2024 already in use

Another LangGraph project is running. Either stop it, or:

```bash
langgraph dev --port 2199
```

…and put that port in the front end's **server** box.

### The front end can't reach the server

In order of likelihood: the server isn't running; the URL is wrong; the graph name doesn't
match `langgraph.json` (it should be `aria`); or you're pointed at a deployment without
pasting an API key. The error bubble in the page lists all four.

If your browser blocks `file://` requests:

```bash
python -m http.server 8080 --directory frontend
```

### Notebook cells say a file doesn't exist

The kernel started outside the repo root. Re-run the setup cell at the top — it does the
`chdir` and `sys.path` work.

---

## No LangSmith access

You can still do most of Modules 1 and 2:

| | Works? |
| :-- | :-- |
| Module 1 — everything except viewing traces | ✅ |
| Module 2 — Level 0 (harness), the middleware and evaluator unit tests | ✅ |
| Module 2 — Levels 1–2 (need datasets and experiments) | ❌ |
| Module 2 — Level 3 pytest (runs; results just aren't recorded) | ⚠️ |
| Modules 3 and 4 | ❌ platform features |

Set `LANGSMITH_TRACING=false` and the agent runs normally. `pytest tests/` — all 179 — needs
no key at all. For Modules 3 and 4, **pair up with someone who has a key.**

## No model provider key

Every deterministic test still runs, which is more than it sounds:

```bash
pytest tests/ -q                            # 179 tests, no key, ~6 seconds
python -m evals.harness                     # print the assembled prompt
python scripts/build_notebooks.py
```

You'll be reading rather than running for the model-backed cells. Pair up.

---

## What each piece needs

| | LangSmith | Model key | Plan |
| :-- | :--: | :--: | :-- |
| `pytest tests/` (179) | — | — | any |
| Level 0 harness / context assertions | — | — | any |
| Agent invocation | optional | ✅ | any |
| MCP server | — | — | any |
| Datasets, experiments, Levels 1–4 | ✅ | ✅ | any |
| Online evaluators, alerts, automations | ✅ | ✅ | any |
| Annotation queues + assertions | ✅ | — | any |
| `langgraph deploy` | ✅ | ✅ | **Plus+** |
| `langgraph dev` (the fallback) | optional | ✅ | any |
| Insights | ✅ | ✅ | **Plus+** |
| Engine | ✅ | — | **Plus+**, org-enabled |

The two Plus-only features (Insights, Engine) are facilitator demos in the notebooks, not
participant exercises — so a Developer-tier account isn't a blocker for the session.
