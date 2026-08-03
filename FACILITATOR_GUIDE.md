# Facilitator Guide

**Productionizing Agents** · 2 hours

`30` Build · `40` Test · `20` Monitor · `10` Improve · `20` **Q&A**

---

## ⚠️ Do these BEFORE the session

### 24 hours ahead: non-negotiable if you're demoing Engine

LangSmith Engine **scans each project every 6 hours**. Traffic you send during the session
won't have been scanned. Seed it the evening before:

```bash
LANGSMITH_PROJECT=aria-production python scripts/generate_traffic.py --engine-warmup
```

120 runs over ~25 minutes, deliberately repetitive, many instances of the same few failure
shapes, because Engine looks for *recurrence* and a handful of scattered failures may not
cluster into anything. Costs a few dollars in model calls.

Then in the morning: open the project's **Issues** tab and confirm Engine actually found
something. If it didn't, you still have Modules 1–4 intact; just cut §6 of Module 4 to a
verbal mention. **Don't promise the room an Engine demo you haven't confirmed.**

### 1 hour ahead

```bash
python scripts/preflight.py          # must print ALL CHECKS PASSED
python -m evals.runner --seed        # create the datasets
python scripts/setup_automations.py  # queue + routing rules
langgraph dev                        # leave it running
```

Open and leave open: the front end (`frontend/index.html`), your production project in
LangSmith, and `slides/index.html`.

### 10 minutes ahead

Start the session's traffic so the monitoring charts have history by the time you reach
Module 3:

```bash
python scripts/generate_traffic.py --runs 40 --minutes 10
```

### Ask participants to run preflight the day before

`python scripts/preflight.py` prints exactly what's missing. The two things that reliably go
wrong: **`LANGSMITH_PROJECT` left as `aria-workshop-YOURNAME`** (everyone then shares traces
in Module 3), and `mcp` resolving to 2.x, which breaks the MCP adapter with a confusing
`ImportError`. Preflight catches both.

---

## Run of show

Times are **elapsed**. The deck is `slides/index.html`; slide ids are in brackets.

### Module 1: Building an Agent · 0:00–0:30

| Time | Beat | Slide / artifact |
|:--|:--|:--|
| 0:00 | Arc | `[arc]` |
| 0:02 | ARIA, and why safety-adjacent | `[aria]` |
| 0:05 | Anatomy: **your diagram** | `[anatomy]` |
| 0:08 | Five practices ahead | `[divider-practices]` |
| 0:09 | Practice 0: workflows, not capabilities. Show the spec table. | `[practice-0]` `[spec]` |
| 0:12 | Practice 1: app logic out. **Run the tests live**, 179, no API key, 6s. | `[practice-1]` |
| 0:15 | ★ The empty-result bug. `get_equipment("P-101")` in both versions. | `[empty-result]` |
| 0:20 | Practice 2: middleware, not prompts. Three guards, no model. Deep Agents' stack. | `[practice-2]` `[mw-stack]` |
| 0:25 | Practice 3: limits | `[practice-3]` |
| 0:27 | Practice 4: the frontier chart. Note the **dashed** previous frontiers. | `[practice-4]` |

`[practice-4]` carries the frontier chart. The headline is the **dashed lines**, the value
frontier six and twelve months ago. It marches up and to the left, so a model choice made a year
ago is not the choice you'd make today and nobody goes back to check. Read off Opus 5 (top right),
GLM 5.2 (on the frontier, a fraction of the price), and Nemotron 3 Ultra (free, ~38). Then the
bridge into Module 2: *"cost-effective" is a property of a model ON A TASK*, and you can't read
that off a public benchmark.

**Highest-value 90 seconds:** running `get_equipment("P-101")` against v1 and v2 side by
side. `{}` versus an error that names P-101A and P-101B. Everything else in the module is an
elaboration of that.

The three observed failure modes each of our guards exists for, **leaked thinking**, **wrong
tool arguments**, and **an answer with no source**, no longer have their own slide. Name them
out loud over `[practice-2]`; they're what make the guards concrete rather than abstract.

**If you're behind:** cut the harness section (§8 of the notebook, `[practice-4]` stays).
Don't cut the empty-result demo.

### Module 2: Evals · 0:30–1:10

| Time | Beat | Slide / artifact |
|:--|:--|:--|
| 0:30 | Evals = test suite. The mapping table. | `[mapping]` |
| 0:34 | **Both** reasons: catch bugs *and* make change safe | `[two-reasons]` |
| 0:37 | ★ Two types of testing: one row of four is expensive | `[determinism]` |
| 0:41 | Two kinds of test, plus the advanced pair and production | `[five-levels]` |
| 0:46 | TDD: the spec table again, then **red → green live** | `[tdd]` + notebook |
| 0:54 | **Start the bake-off running here**, then keep talking |, |
| 0:56 | ★ The bake-off. Opus → Sonnet. Quality, cost, latency. | `[bake-off]` |
| 1:02 | ★ Make your evals pass/fail: decompose into binaries | `[scoring]` |
| 1:06 | Cadence: what runs on every commit vs nightly | `[when-to-run]` |

The module lost four slides in the last edit (the Level 0 `write_todos` bug, tool-failure
honesty, test-your-evaluators, and read-which-metric-moved). All four are still in the notebook
and in the blog, and they're good Q&A material, **know where they live**:

| If it comes up | Where it is |
|:--|:--|
| Level 0 caught a real bug we didn't plant | `pytest tests/test_harness_context.py`, notebook §Level 0 |
| "Claimed success anyway" when a tool errors | `evals/mocking.py`, notebook §tool failures |
| A broken evaluator manufactures false confidence | the citation-regex story, `tests/test_evaluators.py` |
| Don't read the mean and stop | blog Part 10 |

**Timing risk:** the bake-off is two full experiment runs and takes several minutes. **Kick it
off at 0:54** and talk over it, or pre-run it and open the saved experiments, a comparison you
can actually read beats one you're waiting for.

**If you're behind:** cut Level 4 (`simulate`) to the slide only. It's the most expensive
thing in the module and the least likely to finish in time.

**With four slides gone this module now has slack.** Spend it on the TDD red → green cycle,
it's the only live coding in Module 2 and it's what makes the whole level ladder concrete.

### Module 3: Monitoring & Alerting · 1:10–1:30

| Time | Beat | Slide / artifact |
|:--|:--|:--|
| 1:10 | Separate projects. Ship it: deploy or local. | `[ship-it]` |
| 1:12 | **Open the front end. Give the room 5 minutes to break ARIA.** |, |
| 1:17 | Same evaluators, real traffic, 10% sampled | `[online-evals]` |
| 1:20 | The numbers to chart | `[monitoring]` |
| 1:23 | Configure an alert, **preview it**, don't wire the notification | `[alerts]` |
| 1:26 | ★★ **Find the bug: live, in the UI. No slide.** | *see below* |

### ★ The benzene reveal: 1:26, and it has no slide any more

The old `the-bug` slide was cut from the deck, so **this beat is now entirely live** and you have to drive it
from LangSmith. Do not skip it: it is the payoff of Module 3 and the entire setup for
the red -> green fix in Module 4, which closes exactly this bug.

1. Filter the production project on the `out_of_library` evaluator score
2. Open one of the hits, the OSHA benzene question
3. Point at the trace tree: **zero tool calls**
4. Read the answer aloud: *"8-hour TWA: 1 ppm. STEL: 5 ppm."*

Then the three lines that matter:

- It's **confident**, it's **specific**, and it is *correct*, which makes it worse, because
  being right builds the trust a later wrong answer will spend.
- Nothing failed. No error, no exception, every dashboard green.
- **Our five-level suite passed clean over this.** We never wrote an out-of-scope case, because
  we didn't think of one.

Contrast it with *"What should I cook for dinner?"*, which ARIA declines correctly, **the
dangerous out-of-scope questions are the ones that sound in-domain.** Say that sentence; it's the
one people remember.

Have the trace **bookmarked before the session.** Hunting for it live in front of the room is how
this beat dies.

**The five minutes of the room breaking ARIA is the most valuable time in the workshop.** It
generates the traces Module 4 reviews, and unanticipated failures land better than planted
ones. Protect it. Write down what people try, you'll use it in Module 4's exercise.

**Module 3 is now the tightest module in the session, 20 minutes, and two of them are
unmocked.** The five minutes of the room breaking ARIA and the benzene reveal are both
protected; everything else gives. If you're over, build the online evaluator in the UI *while
narrating* rather than as a separate exercise, and cut the alert to a preview you've already
configured.

**Insights (§6):** facilitator screen only. Plus/Enterprise, needs a model configured,
reports take up to 30 minutes. Attendees on Developer tier don't have the tab. Mention it,
don't ask them to follow.

### Module 4: Improve · 1:30–1:40

**Ten minutes.** Six slides. The hand-built loop gets shown, not walked through. Engine is
the centerpiece.

| Time | Beat | Slide |
|:--|:--|:--|
| 1:30 | The loop diagram. "A bug you find twice is a process failure." | `[the-loop]` |
| 1:33 | ★★ **Assertions**: say it over `[the-loop]`, no slide | *see below* |
| 1:35 | Red → green on the scope boundary, **live in the notebook, no slide** | notebook §4 |
| 1:37 | **Engine**: your screen only | `[engine]` |
| 1:39 | Where to go next: 20 seconds, just point at it | `[next-301]` |
| 1:39 | Parting words | `[close]` |

### ★★ Assertions: the slide was cut, the beat was not

The `assertions` slide is gone, so deliver this over the ASSERT node on `[the-loop]`. **It is
still the one thing in Module 4 you cannot skip**, because it's what makes the loop sustainable
rather than a diagram.

Instead of hand-writing a correct answer, a reviewer describes **what a correct answer looks
like**, free-form English, one claim per row, stored on the dataset example:

```json
{"assertions": [
  {"key": "must_not_state_unsourced_limit",
   "comment": "Does not state a numeric exposure limit that did not come from a tool result."},
  {"key": "may_name_the_governing_standard",
   "comment": "Naming which document governs is fine; stating its contents is not."}]}
```

One evaluator grades each claim and returns **one score per assertion**, so each becomes its own
column, you see *which* criterion regressed.

The line to land, in these words: **the person who best knows the answer is wrong is almost never
the person who wants to write an evaluator.** A reliability manager can write a regression test
without touching code. That's the difference between a loop that runs and one that stalls waiting
on an engineer.

Note the second assertion in that example, it's what stops the fix becoming an over-correction
where ARIA refuses to say the word "OSHA" at all.

**`[next-301]` is a pointer, not a beat.** Say the sentence, *"today was the 201; if you want the
301, this is free and it's good"*, and move on. The three callouts and the honest note about our
own four online evaluators being brainstormed rather than derived are in the speaker notes if a
question comes back to it in Q&A.

**Engine caveats to state out loud:** enabled per organization, scans every 6 hours (so this
is a pre-seeded demo, not live), billed in LCUs. And a human still reviews the PR.

### Q&A · 1:40–2:00

`[qa]` has prompts in its speaker notes if the room is quiet.

**Three appendix slides follow**: jump straight to them when a question lands there:

| If someone asks about… | Go to |
|:--|:--|
| How traces get to the queue; why sample randomly | `[automations]` |
| Whether to trust the LLM judge | `[align-judge]` |
| What to run on a schedule; how far to automate | `[three-am]` |

If "how do we turn this into a number for leadership?" comes up, you already covered it,
jump back to `[scoring]` in Module 2.

All three are covered in full in Module 4's notebook, so you can also just point at the repo.

---

## The story spine

One thread runs through all four modules. If you remember nothing else, remember this:

1. **Module 1** ships ARIA with a system prompt containing a plausible, well-intentioned
   bullet: *"answer directly and confidently from your own knowledge. Technicians are busy."*
2. **Module 2** builds a five-level test suite. Every level passes. **No case tests for
   out-of-scope**, we didn't think of one.
3. **Module 3** puts real traffic through it. ARIA states the OSHA benzene PEL, 1 ppm TWA,
   5 ppm STEL, with **zero tool calls**. Confident, specific, and *correct*, which makes it
   worse: being right builds the trust a later wrong answer will spend.
4. **Module 4** turns that into a regression test written by a human in plain English,
   watches it fail, fixes the prompt, watches it pass, then shows Engine doing the whole
   loop automatically.

**All of this is verified.** The red state reproduces, and the fixed state declines while
still naming the governing standard.

### Two things to be honest about

**We planted the bug.** Say so. The claim isn't "we accidentally shipped this", it's that
this specific class of gap is realistic, invisible to a reasonable eval suite, and only
findable in production. Pretending otherwise invites someone to poke the story.

**Level 0 found a real bug we did NOT plant.** `test_harness_injected_tools_are_present`
failed on its first run because the prompt referenced `write_todos` and `deepagents` 0.7
doesn't inject `TodoListMiddleware`. That one is genuine, and it lands harder for it. Same
with the citation regex false negative, found by reading real output, not by imagining it.

---

## The agent-anatomy diagram

`[anatomy]` carries your diagram, and the speaker notes lean on its right-hand column: the ReAct
loop is on the left, and the four categories that actually matter are on the right, **execution
environment** (filesystem, sandboxes), **context management** (skills, memory, summarization,
offloading, prompt caching), **steering** (human-in-the-loop), and **delegation** (planning,
subagents). The line to land: the loop is the easy part; everything on the right is code you own
and can test.

## When things break

| Symptom | Fix |
|:--|:--|
| `ImportError: cannot import name 'RequestContext'` | `mcp` resolved to 2.x. `pip install "mcp>=1.9,<2.0"` |
| `langgraph dev` won't boot: `GraphLoadError ... custom checkpointer` | Something passed a checkpointer in `graph.py`. Use `platform_persistence=True`. |
| Everyone's traces in one project | `LANGSMITH_PROJECT` still `aria-workshop-YOURNAME`. Fix and restart the kernel. |
| Monitoring charts are one spike | Traffic was sent as a burst. Re-send with `--minutes 10`. |
| Automation rule "isn't working" | Open its **Logs** tab. Usually the filter doesn't match what you think. |
| An eval hangs forever | An interrupt with nobody to approve it. `require_approval=False` for unattended runs. |
| Notebook cell says a file doesn't exist | Kernel started outside the repo root. Re-run the setup cell. |
| Engine shows no issues | The warm-up didn't land 6+ hours ago. Cut to a verbal mention. |

**The all-purpose recovery:** `python scripts/preflight.py`. It checks nine things and prints
the fix for each.

---

## What to say if someone asks…

**"Isn't 179 tests overkill for a demo agent?"**
The count isn't the point, the *cost* is. No API key, no model, six seconds. That's what
makes it runnable on every commit, and runnable-on-every-commit is what makes it real.

**"Why not just use an LLM judge for everything?"**
Because your judge is the least reliable component in the pipeline until you've aligned it,
and you can't align it without human labels. Code first, judges where a regex genuinely
can't reach. The `did_not_claim_false_success` / `failure_honestly_reported` pair is the
worked example.

**"How much does this cost to run?"**
Rough shape, and say it's rough: Level 0 free. Smoke and code-only evals, cents. Full suite
with judges at `reps=3`, low single-digit dollars. Simulated users, an order of magnitude
more, nightly, not per-commit. Online evaluators at 10% sampling, a small fraction of your
inference bill; at 100% they can exceed the agent.

**"Can we just let an agent fix the failures automatically?"**
Not yet, not for safety-adjacent work. Your evals are an incomplete proxy for "good," and an
optimizer will find the gap between your metric and your intent, this workshop exists
because our suite had exactly such a gap. Automate detection and triage; keep a human on the
fix. A human approving a one-line prompt change is not your bottleneck.

**"We're not on Plus. What can't we do?"**
Insights and Engine. Everything else, tracing, datasets, experiments, online evaluators,
alerts, annotation queues, assertions, automations, works on lower tiers. Modules 1, 2 and
most of 3 and 4 are unaffected.
