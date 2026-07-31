---
name: writing-agent-evals
description: Build or extend an evaluation suite for an LLM agent. Verifiable rules covering TDD for agents, separating deterministic code from the model, code and judge evaluators, scripted and stateful mocking, LangSmith wiring, production sampling, and closing the loop from live traces into regression tests.
---

# Writing agent evals

Rules, in order. Each one is checkable: you can look at a repo and say whether it holds.

## Build

**1. Do TDD for agents.** Start every new feature by adding samples to the dataset: the request,
the expected response, and the state of the world. Write them before the capability exists, and
**watch them fail before you build anything.** A test you have not seen red is a hope, not a test.
A surprising number of hand-written assertions pass trivially against the broken agent, because
they describe something it already did.

**2. Keep the agent thin.** Application logic lives outside it, in a package with its own tests
that run with no API key. Prefer stock middleware over hand-rolled; when you do write your own,
unit test the hooks directly by calling them with a fabricated request.

## Separate the deterministic from the stochastic

**3. Bifurcate LLM from everything else, as much as you can.**

*Tools and harness* are deterministic, fast, and free. Test them as ordinary code.

*The harness includes the assembled prompt*, and almost nobody asserts on it. Invoke the agent
once against a fake model that records what it received, then assert: every tool present, every
argument described, load-bearing prompt sentences intact, middleware in the configured order,
context under a token ceiling. This is the highest value per second in the whole suite.

*For the LLM*, mock the tools and harness so you have guarantees about the world.

- **Non-stateful:** scripted mocks in the dataset, run under `aevaluate`.
  `inputs: {prompt: "...", mock_tools: {"data_search": "the world in 2023 was..."}}`.
  Put `mock_tools` in `inputs`, not `reference_outputs`, because the target function receives
  `inputs` only, and the mock world is an input, not a claim about correctness.
- **Truly stateful:** the pytest LangSmith integration, written as traditional unit tests. Still
  mock the tools, and use a `state={}` object for mutations. Assert on the state, not on the
  agent's claim that it changed something.
- **Multi-turn:** a few trajectory tests, either LLM-as-user simulation or hardcoded
  `prompt1, prompt2, prompt3`. Expect these to be flaky and rarely run. Keep them few.

**4. Test what happens when tools fail.** Inject errors, empty results, and exceptions. The
failure that matters is the agent claiming success anyway, which is invisible in error logs,
invisible on dashboards, and the one that breaks trust.

## Evaluators

**5. Evals return boolean pass/fail.** Ten binary judges beat one judge scoring 1 to 10. If a
scale is truly unavoidable, anchor every point.

**6. Use code evals for as much as possible.** Reach for an LLM only when code genuinely cannot
catch it.

**7. Write your evaluators yourself.** Everything else is an implementation detail; evals are how
you make every decision from here on. A coding agent may take the first stab, but a human reviews
every one.

**8. Validate the judge before you trust it.** Pin the judge model in config and leave it alone,
including while swapping the agent's model. Require a `reasoning` field. Align it against ~20
human labels, balanced pass and fail, and group the misaligned cases before editing the prompt.
Until aligned, treat its absolute numbers with suspicion and its relative numbers as useful.

## Run

**9. Run with `repetitions=3` while developing.** Two things to read from it. A case scoring 0.67
is flaky, not broken, and those need different fixes. And if the *evaluator* doesn't return the
same result each time, tighten the eval prompt.

**10. Hold three things fixed across any comparison:** the dataset, the evaluators, and the judge
model. Change one and you can no longer attribute a difference to the agent.

**11. Don't read the mean and stop.** Read which evaluator moved. `tool_budget 0.92 -> 0.83` is
probably fine; `groundedness 0.98 -> 0.89` is hallucination. Same delta, opposite decisions. Tier
your metrics into non-negotiable / important / efficiency *before* the run, while it's still easy
to be honest. Use p99, not p50, for latency.

## LangSmith wiring

**12.** Store prompts in PromptHub and have evaluators reference them, so the grader's prompt is
versioned rather than buried in code. Link a rule/automation from the eval to the **dataset** for
offline, and another to the **project** for online.

## Production

**13. You won't know all the failure modes before you ship.** Release internally or to beta
first. Pipe **5%** of live traces to an annotation queue and have domain experts leave
`assertions` in plain English. Run an assertions judge against them.

**14. Sample online evals by cost.** Code evals on **100%** of production traffic, they're free.
LLM evals on **~10%** (100% while you're still testing). Set up automation to route every errored
trace, whether a code error or a judge-flagged failure, to the annotation queue for review.
