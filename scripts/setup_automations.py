#!/usr/bin/env python3
"""Wire the closed loop: production traces -> human review -> dataset.

    python scripts/setup_automations.py                 # create queue + rules
    python scripts/setup_automations.py --list          # show what exists
    python scripts/setup_automations.py --dry-run       # print, change nothing

WHAT THIS BUILDS
----------------
    1. An annotation queue        "aria-triage"
    2. A rule: bad traces         -> the queue      (feedback says something went wrong)
    3. A rule: 5% spot check      -> the queue      (because your evaluators miss things)
    4. A dataset                  "aria-regressions" (where reviewed cases land)

WHY TWO RULES AND NOT ONE
-------------------------
The first rule routes traces your online evaluators already flagged. That is the obvious one,
and it is not sufficient, it can only ever surface failure modes you thought to write an
evaluator for. It has exactly the same blind spot as your offline suite.

The second rule samples 5% of *everything*, unconditionally. That is your only channel for
finding the failure mode nobody has named yet. It feels wasteful right up until it surfaces
something, and then it is the most valuable rule you have.

Budget your reviewers' attention deliberately: mostly flagged traces, with a steady trickle of
random ones. If you only ever review what your filters catch, your filters define the limit of
what you can learn.

A NOTE ON RULE ORDERING
-----------------------
Automation rules poll independently. If you want a webhook to include an evaluator's score,
the evaluator must have already run, and nothing guarantees that ordering. The fix is to put
a filter on the downstream rule so it only matches traces that already carry the score, which
makes the dependency explicit instead of hoping. Within a single rule, actions run in a fixed
order: annotation queue, dataset, webhook, online evaluator, code evaluator, alert.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

QUEUE_NAME = "aria-triage"
DATASET_NAME = "aria-regressions"


def _client():
    from langsmith import Client

    return Client()


def _session_id(client: Any, project: str) -> str:
    return str(client.read_project(project_name=project).id)


# --------------------------------------------------------------------------- create


def ensure_dataset(client: Any, *, dry_run: bool) -> Any:
    """The dataset reviewed traces get promoted into.

    Separate from the Module 2 datasets on purpose. `aria-regressions` has a different
    provenance, every row is something that actually happened in production, and that makes
    it the most valuable dataset you own. Cases you invented are guesses about what users do;
    these are evidence.
    """
    from langsmith.utils import LangSmithNotFoundError

    try:
        dataset = client.read_dataset(dataset_name=DATASET_NAME)
        print(f"  = dataset {DATASET_NAME} (exists, {dataset.example_count or 0} examples)")
        return dataset
    except (LangSmithNotFoundError, Exception):  # noqa: BLE001
        pass

    if dry_run:
        print(f"  + would create dataset {DATASET_NAME}")
        return None

    dataset = client.create_dataset(
        dataset_name=DATASET_NAME,
        description=(
            "Production failures, promoted from the aria-triage annotation queue. Every row "
            "is a thing that actually happened. Assertions written by a human reviewer."
        ),
    )
    print(f"  + dataset {DATASET_NAME}")
    return dataset


def ensure_queue(client: Any, *, dry_run: bool) -> Any:
    """A single-run annotation queue.

    Single-run (not pairwise) matters: **assertions are only available on single-run queues.**
    Assertions are the whole point, they are how a reviewer turns "it shouldn't have done
    that" into a regression test without writing any code.
    """
    existing = next(
        (q for q in client.list_annotation_queues() if q.name == QUEUE_NAME), None
    )
    if existing:
        print(f"  = queue {QUEUE_NAME} (exists)")
        return existing

    if dry_run:
        print(f"  + would create queue {QUEUE_NAME}")
        return None

    queue = client.create_annotation_queue(
        name=QUEUE_NAME,
        description=(
            "Triage for ARIA production traces. Flagged failures plus a 5% random sample. "
            "Write assertions describing what a correct answer looks like, then send to the "
            "aria-regressions dataset."
        ),
    )
    print(f"  + queue {QUEUE_NAME}")
    return queue


def ensure_rules(client: Any, project: str, queue: Any, *, dry_run: bool) -> None:
    """The two routing rules."""
    if queue is None:
        print("  ! no queue, skipping rules")
        return

    session_id = _session_id(client, project)
    existing = {r.display_name for r in client.list_run_rules(session_id=session_id)}

    rules: list[dict[str, Any]] = [
        {
            "display_name": "aria: flagged failures -> triage",
            # Filter syntax mirrors the trace filter builder in the UI. Build it there first,
            # then copy it here, hand-writing these is a waste of an afternoon.
            #
            # Any of: the out_of_scope judge fired, the agent told the user something broke,
            # or the run raised.
            "filter": (
                'or(eq(feedback_key, "out_of_scope"), '
                'eq(feedback_key, "perceived_error"), '
                'eq(is_root, true))'
            ),
            "sampling_rate": 1.0,
        },
        {
            "display_name": "aria: 5% random spot check -> triage",
            # No filter. This is the only channel that can surface a failure mode nobody has
            # named yet, which is precisely the failure mode that hurts.
            "filter": None,
            "sampling_rate": 0.05,
        },
    ]

    for rule in rules:
        if rule["display_name"] in existing:
            print(f"  = rule {rule['display_name']!r}")
            continue
        if dry_run:
            print(f"  + would create rule {rule['display_name']!r} @ {rule['sampling_rate']}")
            continue

        try:
            client.create_run_rule(
                display_name=rule["display_name"],
                session_id=session_id,
                filter=rule["filter"],
                sampling_rate=rule["sampling_rate"],
                add_to_annotation_queue_id=str(queue.id),
            )
            print(f"  + rule {rule['display_name']!r} @ {rule['sampling_rate']}")
        except Exception as exc:  # noqa: BLE001
            # Filter grammar and SDK signatures move around; the UI is authoritative. Fail
            # loud with the fallback rather than pretending it worked.
            print(f"  ! rule {rule['display_name']!r} failed: {exc}")
            print("    Create it in the UI: project -> Automations -> + New Automation")


# ----------------------------------------------------------------------------- list


def list_state(client: Any, project: str) -> None:
    print(f"\nproject: {project}\n")

    print("annotation queues:")
    for queue in client.list_annotation_queues():
        print(f"  {queue.name}")

    print("\nautomation rules:")
    try:
        session_id = _session_id(client, project)
        for rule in client.list_run_rules(session_id=session_id):
            target = getattr(rule, "add_to_annotation_queue_name", None) or getattr(
                rule, "add_to_dataset_name", None
            ) or "(webhook/evaluator)"
            print(f"  {rule.display_name}  @{rule.sampling_rate}  -> {target}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! {exc}")

    print("\ndatasets:")
    for dataset in client.list_datasets():
        if dataset.name.startswith("aria"):
            print(f"  {dataset.name}  ({dataset.example_count or 0} examples)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", default=os.environ.get("LANGSMITH_PROJECT"))
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    project = args.project or os.environ.get("LANGSMITH_PROJECT")

    if not os.environ.get("LANGSMITH_API_KEY"):
        raise SystemExit("LANGSMITH_API_KEY is not set.")
    if not project:
        raise SystemExit("No project. Pass --project or set LANGSMITH_PROJECT.")

    client = _client()

    if args.list:
        list_state(client, project)
        return 0

    print(f"Wiring the loop for {project!r}{' (dry run)' if args.dry_run else ''}:\n")
    ensure_dataset(client, dry_run=args.dry_run)
    queue = ensure_queue(client, dry_run=args.dry_run)
    ensure_rules(client, project, queue, dry_run=args.dry_run)

    print(
        "\nNext:"
        f"\n  1. Send traffic:   python scripts/generate_traffic.py --runs 40 --minutes 10"
        f"\n  2. Review:         LangSmith -> Annotation Queues -> {QUEUE_NAME}"
        f"\n  3. Write assertions on a bad trace, then Add to Dataset & Next"
        f"\n  4. Run the regression:  python -m evals.runner --level regressions"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
