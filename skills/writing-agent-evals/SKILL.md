---
name: writing-agent-evals
description: Build or extend an evaluation suite for an LLM agent. Covers the five test levels from harness assertions to simulated users, code and judge evaluators, dataset design with failure injection, run cadence, reading model comparisons, and closing the loop from production traces back into regression tests.
---

# Writing agent evals

## When to use

- The user wants evals, a test suite, or regression tests for an agent
- An agent works in demos and fails unpredictably in use
- Someone asks "can we ship the cheaper model?" and there is no measurement
- A production failure needs to become a permanent test

## Order of operations

Work top to bottom. Each step is cheap and makes the next one meaningful.

1. Separate application logic from the agent, and unit test it
2. Make tool results total and informative
3. Decide whether a row is a trace or a thread
4. Write Level 0 (harness assertions, free, no model)
5. Write the dataset before more evaluators
6. Add Level 1 smoke, then Level 2 scripted
7. Add code evaluators, then judges only where a regex cannot reach
8. Wire the production loop

## Step 1: separate application logic

A wrong answer has four candidate causes: bad context, a tool that errored or misled, a
misbehaving harness, or bad model reasoning. Only the last is non-deterministic and expensive.
Make the other three testable in milliseconds with no API key.

```
app/               no LLM import anywhere in it
  repository.py      data access, validation, business rules
agent/             prompt, middleware, model, tool wiring
```

Target state: `pytest tests/` runs the application with no API key, so "the tools are correct"
is a claim with a green run behind it. A protocol boundary (MCP or HTTP) makes the separation
impossible to violate and independently deployable, at the cost of a process hop.

Performance note: over stdio, every MCP tool call spawns a fresh server process. Measure before
putting one inside a loop; use the in-process path there.

## Step 2: make tools total

Every input gets a defined, informative response. Never return bare `{}` or `null` for "not
found": a model reads that as "does not exist" and states so confidently.

An error should name the fix:

```python
# Bad
return {}
# Good
return {"error": "no account matches 'acme'",
        "hint": "accounts are looked up by slug, not display name",
        "did_you_mean": ["acme-corp", "acme-eu"]}
```

Also verify per-argument descriptions actually reach the model. They are not generated from
docstrings by default (LangChain needs `@tool(parse_docstring=True)`; FastMCP needs
`Annotated[..., Field(description=...)]`). Check, do not assume:

```python
print(tool.args_schema.model_json_schema()["properties"])
```

## Step 3: trace or thread

| | Scope | Use when |
| :-- | :-- | :-- |
| **Trace** | One user prompt and the agent's reply, including all internal loops and tool calls | Default. Start here. |
| **Thread** | A sequence of back-and-forths | The property needs cross-turn context |

Start at the trace level: a failing trace gives a bounded thing to fix, with tool calls attached.
Use threads for properties that only exist across turns (repeated itself, lost a constraint set
three turns ago, user had to ask twice).

Threads have no natural end. Pick an inactivity cutoff, around 10 minutes, and accept it is
arbitrary.

## Step 4: the five levels

The axis is how much of the world is real, not how many components are involved.

| Level | Name | What is real | Assert | Cost |
| :-: | :-- | :-- | :-- | :-- |
| 0 | Harness | Nothing. Fake model. | Middleware behavior, assembled prompt, tool schemas, middleware order, limits | free |
| 1 | Smoke | The model. Tools stubbed. | It responded, format, which tool it reached for | ~free |
| 2 | Scripted | Tool responses from the dataset | Behavior given a specific world, including failures | cheap |
| 3 | Stateful | Real mutable state, real side effects | The world actually changed | medium |
| 4 | Simulated | A second LLM plays the user | Behavior over a trajectory, under a persona | expensive |

**Push every property as far down the ladder as it will go.** At Level 0 it costs nothing and
never flakes; at Level 4 the same property costs a dollar and returns a probability.

### Level 0 is the highest value per second

Two kinds, and most codebases have neither.

Middleware units: call the hook directly with a fabricated call and assert. No agent, no model.

Assembled-context assertions: invoke the agent once against a fake model that records what it
received, then assert on the real assembled request.

```python
ctx = capture_context()
assert LOAD_BEARING_SENTENCE in ctx.system_prompt   # not truncated or replaced
assert EXPECTED_TOOLS <= set(ctx.tool_names)    # every tool arrived
assert not [t for t, d in ctx.tool_descriptions.items() if not d.strip()]
assert ctx.approx_tokens < 12_000               # context bloat is a cost regression
```

Assert harness-injected tools too, so a framework upgrade that changes the set is something a
test reports rather than something behavior reveals.

If the fake model is stateful (an iterator of canned replies), make it inexhaustible or
stateless. A tracer that serializes the model will drain an iterator, and the agent's own call
then fails with a confusing error far from the cause.

### Level 2: put the mock world in the dataset

Both halves of a row take arbitrary JSON the platform never interprets, so one row carries the
world and the expectations:

```json
{
  "inputs": {
    "question": "What's the level in tank 43?",
    "mock_tools": {
      "get_tank_status": {"tag": "T-043", "level_ft": 12.1, "atg_status": "suspect"}
    }
  },
  "reference_outputs": {"intent": "relay the data-quality warning, do not state the level as fact"}
}
```

Put `mock_tools` in `inputs`, not `reference_outputs`. The target function receives `inputs`
only, and the mock world is an input to the run, not a claim about correctness.

**Test what happens when tools fail.** Inject errors, empty results, and exceptions. The failure
that matters is the agent claiming success anyway, which is trust-breaking and invisible in
error logs and dashboards.

## Step 5: evaluators

### Code first, every time

Most of what you want to assert is exactly checkable:

```
"did it respond at all"              ->  len(text) > 0
"did it call get_tank_status"        ->  name in trajectory
"did it cite a source with version"  ->  regex
"did it stay under 6 tool calls"     ->  len(trajectory) <= 6
```

Reach for a judge only when the property is genuinely semantic: task accomplished, grounded,
honestly reported the failure.

Write evaluators against output the agent actually produced, never output you imagined.

Return the trajectory from the target function, not just the final string. If the target returns
only text, every evaluator you can write is a text evaluator.

### Binary judges, not scales

Ten pass/fail judges beat one judge scoring 1 to 10. A single holistic number is unactionable and
you cannot tell whether a move from 6 to 7 is the agent or the grader's mood.

Decompose into claims that are verifiably true or false. If a scale is unavoidable, anchor every
point.

### Five that apply to almost any agent

| Key | Kind | Checks |
| :-- | :-- | :-- |
| `accomplished` | judge | Did the agent do what was asked |
| `perceived_error` | judge | Could this have looked like a failure, or did the user show frustration |
| `user_delighted` | judge | Did the user express actual happiness |
| `tool_error` | code | Did a tool error, including the model calling it wrong |
| `ai_slop` | code | Em dashes and giveaway vocabulary; split per-marker once it fires |

`perceived_error` and `user_delighted` need the *next* user message, so they are thread-level.
Wire `tool_error` first: free, fires on real bugs, and a spike usually means a deploy.

### Three rules for judges

1. **Pin the judge model.** Set it in config and leave it alone, including while swapping the
   agent's model. Changing the grader mid-comparison invalidates the comparison.
2. **Make it output a reason.** Structured output with a `reasoning` field, always.
3. **Align it against human labels.** Label ~20 examples, balanced pass and fail, iterate the
   prompt until it agrees. Group the misaligned cases; two or three failure modes explain most of
   the gap. Add more labels before celebrating: 100% agreement on 20 examples is overfitting.

Until aligned, treat absolute numbers with suspicion and relative numbers (A vs B, same judge) as
useful.

Handle judge output that fails to parse. A judge whose structured output is malformed yields a
null score, which silently drops out of averages.

An evaluator may not return nothing. Some platforms raise on any falsy result, so return an
explicit not-applicable verdict rather than an empty list.

### Assertions let non-engineers write tests

Plain-English claims carried on the example, adjudicated by a judge, one feedback score per
claim:

```json
{"assertions": [
  {"key": "must_not_state_unsourced_limit",
   "comment": "The response does not state a numeric limit that did not come from a tool result."},
  {"key": "must_route_to_a_human",
   "comment": "The response directs the user to the governing standard or a qualified person."}
]}
```

A reviewer writes these while looking at a bad trace. That is the whole loop in one motion, and
it is the difference between a loop that runs and one that stalls waiting on an engineer.

## Step 6: running

**Repetitions.** `num_repetitions=3` re-runs target and evaluators. A score of `0.67` says flaky,
not broken, and those need different fixes (flaky is usually an underspecified prompt; broken is
usually a missing capability). Use 1 while iterating, 3+ for any decision you act on.

**Async.** Agent evals are almost entirely IO wait. 12 examples at `max_concurrency=8` is about
ten seconds versus about a minute.

**Cadence.**

| Situation | Run | Trigger |
| :-- | :-- | :-- |
| Rapid iteration | Level 0 + smoke | Manually, every change |
| Every commit | Level 0 + code evaluators only | CI, no judge calls |
| Prompt or model change | Full suite with judges, reps=3 | CI on changed paths, or a PR label |
| Nightly | Everything, including simulated users | Scheduled |
| Pre-release | Everything, reps=5 | Manual gate |

**Wait for completion before reading aggregates.** Async eval APIs typically return once runs are
queued. Reading stats immediately yields zeros and nulls, and nothing errors, so the comparison
silently compares nothing.

## Step 7: reading results

Hold three things fixed across a comparison: the dataset, the evaluators, and the judge model.

**Do not read the mean and stop.** Read which evaluator moved:

```
tool budget       0.92 -> 0.83     probably fine, it thrashes more
groundedness      0.98 -> 0.89     stop, that is hallucination
```

Same delta, opposite decisions. Tier metrics *before* running the comparison, while it is still
easy to be honest:

| Tier | A regression here means |
| :-- | :-- |
| Non-negotiable | Do not ship. Safety and truthfulness. |
| Important | Investigate before shipping. |
| Efficiency | Trade freely against cost. |

Use **p99, not p50**, for latency. A p50 of 3s with a p99 of 40s is a bad experience a mean hides.

## Step 8: close the loop

Every case in the suite is one someone thought of. Users find the rest.

1. **Online evaluators** on live traffic. They must be reference-free: input, output, trajectory
   only. Sample at **10 to 20%, not 100%**; you are estimating a rate, and three judges at full
   sampling can cost more than the agent.
2. **Two automation rules**, and the second matters most:

| Rule | Sampling | Why |
| :-- | :-- | :-- |
| Traces your evaluators flagged | 100% | Obvious, and has the same blind spot as the offline suite |
| Everything, unfiltered | 5% | The only channel for the failure nobody has named |

3. **A human writes assertions** on the bad trace, which become a dataset row.
4. **Run it and watch it fail before fixing anything.** A regression test you have not seen fail
   is not a test. A surprising fraction of hand-written assertions pass trivially against the
   broken agent because they describe something it already did.
5. **Fix, watch green.**

## Anti-patterns

- Building only the LLM-behavior half of the suite, then wondering why it is slow, flaky and expensive
- One judge returning a 1 to 10 quality score
- Judges for properties a regex could check
- Datasets written after the agent, describing what it already does
- Reading the mean and shipping
- Comparing models while also changing the judge, the dataset, or the evaluators
- Shipping a regression test that has never been red
- Reviewing only what your filters flag, which makes the filters the ceiling on what you can learn
- Asserting the agent *said* it did something, rather than that the world changed

## Verification checklist

- [ ] Application logic has tests that run with no API key
- [ ] Tool errors name the fix; no bare `{}` for "not found"
- [ ] Per-argument descriptions verified present in the schema the model receives
- [ ] Level 0 asserts prompt, tools, arg descriptions, middleware order, context budget
- [ ] Dataset includes injected tool failures
- [ ] Code evaluators outnumber judges
- [ ] Judge model pinned in config and outputs reasoning
- [ ] Judge aligned against ~20 human labels
- [ ] Metrics tiered before the first comparison
- [ ] Online evaluators sampled, not at 100%
- [ ] An unfiltered random-sample review rule exists
- [ ] Every regression test has been observed failing
