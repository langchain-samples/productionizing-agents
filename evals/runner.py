"""Run ARIA's evals: target function, experiment runner, and the model comparison.

    python -m evals.runner --seed                       # create the datasets
    python -m evals.runner --compare                    # the money shot: model bake-off
    python -m evals.runner --level mocked --reps 3      # repeat for stochasticity

WHY `aevaluate` AND NOT `evaluate`
----------------------------------
`aevaluate` runs examples concurrently with real async concurrency, and an agent eval is
almost entirely IO wait on model calls. On a 12-example dataset with `max_concurrency=8` the
difference is roughly a minute versus roughly ten seconds. When your test suite is fast
enough to run on every prompt tweak, you run it on every prompt tweak, and that behavioral
change is worth more than the wall-clock saving.

WHY REPETITIONS
---------------
Your agent is non-deterministic. A single pass over 12 examples gives you 12 samples, and a
case that fails one time in three looks either fine or broken depending on the coin flip.
`num_repetitions=3` re-runs the target *and* the evaluators, so a score of 0.67 tells you
something a score of 0 or 1 cannot: this case is flaky, not broken.

Set it to 1 while iterating for speed, and 3+ for any decision you intend to act on.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from evals.datasets import (
    REGRESSIONS_DATASET,
    MOCKED_DATASET,
    TDD_DATASET,
    seed_datasets,
)
from evals.evaluators import ALL_EVALUATORS, CODE_EVALUATORS
from evals.mocking import CallRecorder, mocked_toolset

DEFAULT_MAX_CONCURRENCY = 8


# --------------------------------------------------------------------- target function


def make_target(model: str, *, exclude_tools: tuple[str, ...] = ()):
    """Build the async target function that `aevaluate` will call once per example.

    A closure over `model` so the same dataset and the same evaluators can be pointed at a
    different model with nothing else changing. That is the entire mechanism behind the
    model comparison at the bottom of this file, and behind Module 2's punchline.

    The returned function:
      1. reads the mock world from `inputs["mock_tools"]`,
      2. builds an ARIA whose tools return exactly those responses,
      3. runs it,
      4. returns the answer AND the trajectory.

    Returning the trajectory is what makes trajectory assertions possible. If your target
    returns only the final string, every evaluator you can write is a text evaluator, and
    you have thrown away the most testable part of an agent's behavior.
    """
    from aria.agent_v2 import build_agent

    async def target(inputs: dict) -> dict:
        recorder = CallRecorder()
        # `exclude_tools` exists so the TDD demo can reproduce the RED state on demand: run
        # the spec against an agent that genuinely lacks the capability, watch it fail, then
        # run it again with the tool present. A red you can reproduce is worth far more than
        # a red you remember.
        toolset = [
            t for t in mocked_toolset(inputs.get("mock_tools"), recorder)
            if t.name not in exclude_tools
        ]
        agent = build_agent(
            model=model,
            tools=toolset,
            checkpointer=InMemorySaver(),
            # Nothing is watching to approve an interrupt in an unattended eval, so the
            # agent would hang. HITL is tested deliberately in `test_stateful.py` instead
            # of being half-tested everywhere.
            require_approval=False,
        )

        try:
            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": inputs["question"]}]},
                config={"configurable": {"thread_id": "eval"}},
            )
            answer = result["messages"][-1].content
            if isinstance(answer, list):
                answer = "".join(
                    b.get("text", "")
                    for b in answer
                    if isinstance(b, dict) and b.get("type") == "text"
                )
        except Exception as exc:  # noqa: BLE001
            # A crash is a result, not an excuse to lose the run. Record it as an empty
            # answer plus the error so `responded` fails loudly and the trajectory is still
            # there to debug from. An exception that aborts the experiment costs you every
            # other example's data too.
            return {
                "answer": "",
                "tool_calls": recorder.to_json(),
                "error": f"{type(exc).__name__}: {exc}",
            }

        return {"answer": answer, "tool_calls": recorder.to_json()}

    return target


# ------------------------------------------------------------------------ experiments

LEVELS: dict[str, dict[str, Any]] = {
    "tdd": {
        # The spec, written before the capability existed. Run it against an agent WITHOUT
        # `complete_work_order` to see red, then with it to see green.
        "dataset": TDD_DATASET,
        "evaluators": ALL_EVALUATORS,
        "description": "The hand-crafted spec, workflows agreed before any code was written",
    },
    "mocked": {
        "dataset": MOCKED_DATASET,
        "evaluators": ALL_EVALUATORS,
        "description": "Behavior under mocked tool responses, failures included",
    },
    "regressions": {
        # Populated from the annotation queue, not by hand. Every row is a failure that
        # actually happened in production, carrying assertions a human wrote while looking at
        # it. This becomes the most valuable dataset you own, the others are guesses about
        # what users do; this one is evidence.
        "dataset": REGRESSIONS_DATASET,
        "evaluators": ALL_EVALUATORS,
        "description": "Production failures, promoted from human review. The regression gate.",
    },
    "mocked-cheap": {
        # Code evaluators only. Same dataset, no judge calls, so it is ~free and fast
        # enough for a pre-commit hook. This is the split you gate every PR on; the full
        # `mocked` split is the pre-release gate.
        "dataset": MOCKED_DATASET,
        "evaluators": CODE_EVALUATORS,
        "description": "Code assertions only, cheap enough for every commit",
    },
}


async def run_level(
    level: str,
    *,
    model: str | None = None,
    reps: int = 1,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    prefix: str | None = None,
    exclude_tools: tuple[str, ...] = (),
) -> Any:
    """Run one split and return the experiment results."""
    from langsmith import Client

    if level not in LEVELS:
        raise SystemExit(f"Unknown level {level!r}. Choose from: {', '.join(LEVELS)}")

    config = LEVELS[level]
    model = model or os.environ.get("ARIA_MODEL", "anthropic:claude-sonnet-5")
    client = Client()

    print(f"\n{'=' * 78}")
    print(f"{level}  |  {config['description']}")
    print(f"model: {model}   reps: {reps}   dataset: {config['dataset']}")
    print("=" * 78)

    if exclude_tools:
        print(f"REPRODUCING RED: agent built WITHOUT {list(exclude_tools)}")

    return await client.aevaluate(
        make_target(model, exclude_tools=exclude_tools),
        data=config["dataset"],
        evaluators=config["evaluators"],
        experiment_prefix=prefix or f"aria-{level}-{model.split(':')[-1]}",
        num_repetitions=reps,
        max_concurrency=max_concurrency,
        # Metadata is what makes an experiment findable six weeks later. Record every
        # variable you changed, future you will want to filter on exactly these.
        metadata={
            "level": level,
            "model": model,
            "reps": reps,
            "agent_version": "v2",
            "judge_model": os.environ.get("JUDGE_MODEL", "anthropic:claude-sonnet-5"),
            "excluded_tools": list(exclude_tools),
        },
    )


def _seconds(value: Any) -> float | None:
    """LangSmith reports latency as a `timedelta`, and the table formats floats."""
    if value is None:
        return None
    return value.total_seconds() if hasattr(value, "total_seconds") else float(value)


def experiment_stats(experiment_name: str) -> dict[str, Any]:
    """Pull cost, latency, and score aggregates for a finished experiment.

    This is the part people skip, and it is half the value of running evals at all. Pass/fail
    tells you whether the agent works. Cost and latency tell you whether you can afford to
    ship it, and they are what turn "should we use the cheaper model?" from an argument into
    a measurement.
    """
    from langsmith import Client

    client = Client()
    project = client.read_project(project_name=experiment_name, include_stats=True)
    feedback = project.feedback_stats or {}

    # `.get("avg", 0)` is not enough: LangSmith sends `"avg": null` for a key whose runs
    # haven't been scored yet, and the default only fires when the key is missing entirely.
    scores = {key: round((value or {}).get("avg") or 0, 3) for key, value in feedback.items()}
    if not scores:
        scores = _scores_from_feedback(client, experiment_name)

    return {
        "experiment": experiment_name,
        "runs": project.run_count,
        "error_rate": project.error_rate,
        "total_cost": float(project.total_cost or 0),
        "cost_per_run": float(project.total_cost or 0) / max(project.run_count or 1, 1),
        "latency_p50": _seconds(project.latency_p50),
        "latency_p99": _seconds(project.latency_p99),
        "total_tokens": project.total_tokens or 0,
        "scores": scores,
    }


def _scores_from_feedback(client: Any, experiment_name: str) -> dict[str, float]:
    """Average the feedback rows ourselves.

    `read_project(include_stats=True).feedback_stats` comes back `None` for an experiment that
    has only just finished, even when every score is already recorded against the runs. Read
    the rows directly rather than reporting a table of 0.000 for a run that graded fine.
    """
    runs = list(client.list_runs(project_name=experiment_name, is_root=True))
    if not runs:
        return {}

    collected: dict[str, list[float]] = {}
    for item in client.list_feedback(run_ids=[run.id for run in runs]):
        if item.score is None:      # a judge that errored, or a not-applicable verdict
            continue
        collected.setdefault(item.key, []).append(float(item.score))
    return {key: round(sum(v) / len(v), 3) for key, v in sorted(collected.items())}


def _mean(scores: dict[str, float]) -> float:
    return round(sum(scores.values()) / len(scores), 3) if scores else 0.0


def print_comparison(stats: list[dict[str, Any]]) -> None:
    """Side-by-side table. The artifact you take to whoever owns the budget."""
    if not stats:
        return

    baseline = stats[0]
    all_keys = sorted({k for s in stats for k in s["scores"]})

    print(f"\n{'=' * 96}")
    print("MODEL COMPARISON")
    print("=" * 96)
    header = f"{'metric':<34}" + "".join(f"{s['experiment'].split('-')[-2][:16]:>20}" for s in stats)
    print(header)
    print("-" * 96)

    def row(label: str, values: list[str]) -> None:
        print(f"{label:<34}" + "".join(f"{v:>20}" for v in values))

    row("mean score", [f"{_mean(s['scores']):.3f}" for s in stats])
    print("-" * 96)
    for key in all_keys:
        row(f"  {key}", [f"{s['scores'].get(key, float('nan')):.2f}" for s in stats])
    print("-" * 96)
    row("total cost (USD)", [f"${s['total_cost']:.4f}" for s in stats])
    row("cost per run (USD)", [f"${s['cost_per_run']:.4f}" for s in stats])
    row("latency p50 (s)", [f"{s['latency_p50']:.2f}" if s["latency_p50"] else "-" for s in stats])
    row("latency p99 (s)", [f"{s['latency_p99']:.2f}" if s["latency_p99"] else "-" for s in stats])
    row("total tokens", [f"{s['total_tokens']:,}" for s in stats])
    row("error rate", [f"{s['error_rate']:.1%}" if s["error_rate"] is not None else "-" for s in stats])

    print("-" * 96)
    for s in stats[1:]:
        cost_ratio = baseline["cost_per_run"] / s["cost_per_run"] if s["cost_per_run"] else 0
        score_delta = _mean(s["scores"]) - _mean(baseline["scores"])
        name = s["experiment"]
        print(f"\nvs baseline ({baseline['experiment']}):")
        print(f"  {name}")
        print(f"    cost:  {cost_ratio:.1f}x cheaper per run")
        print(f"    score: {score_delta:+.3f} mean")
        if score_delta >= -0.02:
            print("    → Quality held. This is a straightforward win; ship the cheaper model.")
        elif score_delta >= -0.10:
            print("    → Small regression. Look at WHICH evaluator dropped before deciding, ")
            print("      a 0.05 drop in `within_tool_budget` is very different from a 0.05")
            print("      drop in `grounded_in_tool_output`. Not all points are equal.")
        else:
            print("    → Real regression. Keep the expensive model, or route by request type.")
    print("=" * 96)


async def compare_models(
    models: list[str] | None = None,
    *,
    level: str = "mocked",
    reps: int = 1,
) -> list[dict[str, Any]]:
    """Run the same dataset and the same evaluators across several models.

    THE PUNCHLINE OF MODULE 2. Once you have a test suite, "can we use the cheaper model?"
    stops being a matter of taste. You run it and read the table.

    Note what is held fixed: the dataset, the evaluators, and the judge model. Change any of
    those between runs and the comparison is worthless, you can no longer attribute a score
    difference to the agent rather than the grader.
    """
    from aria.agent_v2 import DEFAULT_COMPARISON, MODEL_CANDIDATES

    models = models or [MODEL_CANDIDATES[key] for key in DEFAULT_COMPARISON]
    stats: list[dict[str, Any]] = []

    for model in models:
        results = await run_level(level, model=model, reps=reps)
        # The experiment has to finish before its aggregates exist. `aevaluate` returns as soon
        # as the runs are queued, so without this the table reads back zeros and nulls: every
        # score 0.000, no cost, no latency. Nothing errors, it just quietly compares nothing.
        if hasattr(results, "wait"):
            await results.wait()
        name = getattr(results, "experiment_name", None)
        if name:
            stats.append(experiment_stats(name))

    print_comparison(stats)
    return stats


# ----------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run ARIA's evals")
    parser.add_argument("--seed", action="store_true", help="Create/top up the datasets, then exit.")
    parser.add_argument("--overwrite", action="store_true", help="With --seed: rebuild from scratch.")
    parser.add_argument("--level", choices=sorted(LEVELS), help="Run one split.")
    parser.add_argument("--model", help="Override $ARIA_MODEL for this run.")
    parser.add_argument("--reps", type=int, default=1, help="num_repetitions. Use 3+ to act on.")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_MAX_CONCURRENCY)
    parser.add_argument("--compare", action="store_true", help="Model bake-off on --level.")
    parser.add_argument(
        "--exclude-tool",
        action="append",
        default=[],
        help="Build the agent without this tool. Use to reproduce the TDD red state.",
    )
    args = parser.parse_args(argv)

    if args.seed:
        seed_datasets(overwrite=args.overwrite)
        return 0

    if args.compare:
        asyncio.run(compare_models(level=args.level or "mocked", reps=args.reps))
        return 0

    if not args.level:
        parser.print_help()
        return 1

    asyncio.run(
        run_level(
            args.level,
            model=args.model,
            reps=args.reps,
            max_concurrency=args.concurrency,
            exclude_tools=tuple(args.exclude_tool),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
