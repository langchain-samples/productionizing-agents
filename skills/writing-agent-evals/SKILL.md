---
name: writing-agent-evals
description: Build or extend an evaluation suite for an LLM agent. Verifiable rules covering TDD for agents, separating deterministic code from the model, code and judge evaluators, scripted and stateful mocking, LangSmith wiring, production sampling, and closing the loop from live traces into regression tests.
---

# Writing agent evals

Rules, in order. Each one is checkable: you can look at a repo and say whether it holds.

## The one line for your CLAUDE.md

A skill only loads once something has already triggered it. This has to fire *before* the agent
starts writing code, so it belongs in always-on context:

> Treat agents like software and always do TDD. Before adding any capability or fixing any bug,
> add a sample to the eval dataset first, prove it red, then make it green. A sample looks like
> `{inputs: {prompt, mock_tools}, reference_outputs: {must_call, must_not_call, assertions:
> [{key, comment}]}, metadata: {case}}`. Evaluators are the one thing a human must review, so
> say clearly which ones you wrote and why.

That last clause is not optional. Rule 9 says a human reviews every evaluator, and a standing
instruction to write them is exactly the thing that quietly erodes it.

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

### What one row looks like

```json
{
  "inputs": {
    "prompt": "why can't i see both pages when I try to connect to Marky",
    "mock_tools": {
      "web_search": {"results": []},
      "raise_support": "we raised a support message for you"
    }
  },
  "reference_outputs": {
    "tool_calls": {
      "must_call": ["web_search", "raise_support"],
      "must_not_call": []
    },
    "assertions": [
      {"key": "must_not_hallucinate",
       "comment": "The search returned no results, so the response must not state how to fix it."},
      {"key": "must_say_it_escalated",
       "comment": "The response tells the user a support request was raised."}
    ]
  },
  "metadata": {"case": "search_empty_escalates", "tier": "non-negotiable"}
}
```

The mechanically checkable half and the semantic half are separate on purpose: `tool_calls` is a
code evaluator, `assertions` costs a model call. That is rule 6 expressed in the data.

- `must_call` means "appeared at least once, any order". Add `order: [...]` only when the
  sequence is the property being tested.
- `must_not_call` is the highest-value one and the one people forget. It is how you assert
  restraint on a destructive tool.
- `assertions` is a **list** of `{key, comment}`, not an object. The annotation queue emits that
  shape, and rule 15 depends on domain experts adding rows through it.
- `metadata.tier` gives rule 13 somewhere to read the tier from. Without it you cannot tell a
  safety regression from an efficiency one while looking at the table.

**Make the mock and the assertion agree.** A mock that returns a confident, specific answer
cannot be paired with "the agent should not have answered": an agent that uses the result is
following its tool output, and the judge gets a brief that argues with the fixture. Decide which
you are testing, a tool that returned *nothing* or a tool that returned something *irrelevant*,
and write the mock to match. The second is the harder failure mode, because agents will
confidently repurpose an off-topic result.

**`raise` is the only reserved word.** Everything else is returned to the model as-is, which
includes an error payload: a tool that hands back `{"error": "503"}` is returning data, and the
model reads it and can recover. Throwing is the case that needs its own form, because it takes a
different code path and the model never sees a result at all.

| Mock value | Behavior |
| :-- | :-- |
| any value | returned as the tool result, error payloads included |
| `{"raise": "..."}` | throws instead of returning |
| `[a, b, ...]` | successive calls get successive responses |

One sharp edge falls out of that last row: a bare list means *sequence*, so a tool whose real
return value is a list needs nesting, `[[a, b]]`, to mean "return this list every time".

So a failing tool is not a different row shape, just a different mock value. One tool succeeds,
the next fails, and the expectations say what the agent owes you when it does:

```json
"mock_tools": {
  "get_equipment":     {"tag": "P-101A"},
  "create_work_order": {"error": "503 Service Unavailable", "recoverable": true}
},
"reference_outputs": {
  "must_not_mention": ["work order has been created"],
  "assertions": [{"key": "must_say_it_failed",
                  "comment": "States the work order was not created, and what to do next."}]
}
```

Names only is not enough. A tool called correctly with the wrong arguments is a large bug class
and invisible in a trajectory check, so assert on arguments where they matter.

## Evaluators

**5. Evals return boolean pass/fail.** Ten binary judges beat one judge scoring 1 to 10. If a
scale is truly unavoidable, anchor every point.

**6. Use code evals for as much as possible.** Reach for an LLM only when code genuinely cannot
catch it.

**7. Grade samples by hand before you write a judge.** This matters more than writing the
evaluators. Take real outputs, rate each one Y/N on the single property you care about, and
write down *why*. Those labels are the ground truth every judge is measured against, and there
is no substitute for producing them yourself: without them you have no way to know whether a
judge is right, only whether it is confident.

**8. Label at least twice what you tune on, and hold the second half back.** Then align the
judge by hill-climbing its prompt: give a model the current prompt, your labels, and the cases
where it disagreed with you, and let it revise until it matches. Your written reasons are what
make this work, because they tell it *why* it was wrong rather than just that it was.

The split is the part people skip. A prompt tuned until it agrees on the examples it was tuned
on tells you nothing, so score it on the held-out half and report that number. 100% agreement on
the training half is overfitting, not success.

**9. Write your evaluators yourself.** Everything else is an implementation detail; evals are how
you make every decision from here on. A coding agent may take the first stab, but a human reviews
every one.

**10. Pin the judge model** in config and leave it alone, including while swapping the agent's
model. Require a `reasoning` field on its output. Until it is aligned, treat its absolute numbers
with suspicion and its relative numbers as useful.

## Run

**11. Run with `repetitions=3` while developing.** Two things to read from it. A case scoring 0.67
is flaky, not broken, and those need different fixes. And if the *evaluator* doesn't return the
same result each time, tighten the eval prompt.

**12. Hold three things fixed across any comparison:** the dataset, the evaluators, and the judge
model. Change one and you can no longer attribute a difference to the agent.

**13. Don't read the mean and stop.** Read which evaluator moved. `tool_budget 0.92 -> 0.83` is
probably fine; `groundedness 0.98 -> 0.89` is hallucination. Same delta, opposite decisions. Tier
your metrics into non-negotiable / important / efficiency *before* the run, while it's still easy
to be honest. Use p99, not p50, for latency.

## LangSmith wiring

**14.** Store prompts in PromptHub and have evaluators reference them, so the grader's prompt is
versioned rather than buried in code. Link a rule/automation from the eval to the **dataset** for
offline, and another to the **project** for online.

## Production

**15. You won't know all the failure modes before you ship.** Release internally or to beta
first. Pipe **5%** of live traces to an annotation queue and have domain experts leave
`assertions` in plain English. Run an assertions judge against them.

**16. Sample online evals by cost.** Code evals on **100%** of production traffic, they're free.
LLM evals on **~10%** (100% while you're still testing). Set up automation to route every errored
trace, whether a code error or a judge-flagged failure, to the annotation queue for review.
