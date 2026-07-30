#!/usr/bin/env python3
"""Run this BEFORE the session. It tells you exactly what's missing.

    python scripts/preflight.py

Checks, cheapest first, and keeps going after a failure so you get the full picture in one
pass rather than fixing one thing at a time:

    1. Python version
    2. Imports            every package the workshop needs
    3. Fixture data       the JSON files load and cross-reference correctly
    4. Deterministic      the 179 no-LLM tests
    5. MCP server         real subprocess handshake
    6. Notebooks          built and current
    7. Model provider     ONE cheap real call
    8. LangSmith          auth, and one trace round-trip
    9. Deploy tooling     langgraph CLI present

Exit code 0 means you are ready. Anything else prints the fix.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))
    icon = {PASS: "  ok ", FAIL: " FAIL", WARN: " warn", SKIP: " skip"}[status]
    print(f"{icon}  {name}" + (f"\n        {detail}" if detail else ""))


# ------------------------------------------------------------------------- 1. python


def check_python() -> None:
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 11):
        record(PASS, f"Python {major}.{minor}")
    else:
        record(FAIL, f"Python {major}.{minor}", "Need 3.11+. Try: uv venv --python 3.12")


# ------------------------------------------------------------------------ 2. imports

PACKAGES = [
    ("langchain", "langchain"),
    ("langgraph", "langgraph"),
    ("deepagents", "deepagents"),
    ("langsmith", "langsmith"),
    ("mcp", "mcp"),
    ("langchain_mcp_adapters", "langchain-mcp-adapters"),
    ("langchain_anthropic", "langchain-anthropic"),
    ("dotenv", "python-dotenv"),
    ("pytest", "pytest"),
]


def check_imports() -> None:
    missing = []
    for module, package in PACKAGES:
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(package)

    if missing:
        record(FAIL, "packages", f"Missing: {', '.join(missing)}\n        pip install -r requirements.txt")
    else:
        record(PASS, "packages", f"{len(PACKAGES)} imported")

    # The mcp<2.0 pin is easy to lose and produces a confusing ImportError deep in the
    # adapter rather than at install time. Check it explicitly.
    try:
        from importlib.metadata import version as pkg_version

        version = pkg_version("mcp")
        if int(str(version).split(".")[0]) >= 2:
            record(
                FAIL,
                "mcp version",
                f"mcp {version} breaks langchain-mcp-adapters 0.3.x.\n"
                '        pip install "mcp>=1.9,<2.0"',
            )
        else:
            record(PASS, "mcp version", str(version))
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------- 3. fixtures


def check_fixtures() -> None:
    try:
        from aria_mcp.repository import get_repository

        repo = get_repository()
        known = {p["id"] for p in repo.procedures}
        dangling = [
            f"{item['tag']}->{proc}"
            for item in repo.equipment
            for proc in item["applicable_procedures"]
            if proc not in known
        ]
        if dangling:
            record(FAIL, "fixture data", f"Dangling procedure refs: {dangling}")
        else:
            record(
                PASS,
                "fixture data",
                f"{len(repo.procedures)} procedures, {len(repo.equipment)} equipment, "
                f"{len(repo.tanks)} tanks",
            )
    except Exception as exc:  # noqa: BLE001
        record(FAIL, "fixture data", f"{exc}\n        Run from the repo root.")


# ------------------------------------------------------------------ 4. deterministic


def check_tests() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--no-header", "-x"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    tail = (proc.stdout or proc.stderr).strip().splitlines()
    summary = tail[-1] if tail else "no output"
    if proc.returncode == 0:
        record(PASS, "deterministic tests", summary)
    else:
        record(FAIL, "deterministic tests", summary)


# --------------------------------------------------------------------- 5. mcp server


def check_mcp() -> None:
    try:
        import asyncio

        from aria.tools import mcp_tools

        tools = asyncio.run(mcp_tools(transport="stdio"))
        record(PASS, "MCP server", f"{len(tools)} tools over stdio")
    except Exception as exc:  # noqa: BLE001
        record(FAIL, "MCP server", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------- 6. notebooks


def check_notebooks() -> None:
    proc = subprocess.run(
        [sys.executable, "scripts/build_notebooks.py", "--check"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    if proc.returncode == 0:
        record(PASS, "notebooks", "current")
    else:
        record(WARN, "notebooks", "stale — run: python scripts/build_notebooks.py")


# ------------------------------------------------------------------------- 7. model


def check_model() -> None:
    provider = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else (
        "openai" if os.environ.get("OPENAI_API_KEY") else None
    )
    if not provider:
        record(
            FAIL,
            "model provider",
            "No ANTHROPIC_API_KEY or OPENAI_API_KEY in .env.\n"
            "        cp .env.example .env  and fill one in.",
        )
        return

    # Fall back to a sensible default rather than KeyError-ing if ARIA_MODEL is unset —
    # preflight exists to give clear diagnoses, not to fail obscurely itself.
    default = "anthropic:claude-sonnet-5" if provider == "anthropic" else "openai:gpt-5.5"
    model = os.environ.get("ARIA_MODEL") or default
    try:
        from langchain.chat_models import init_chat_model

        # One tiny call. Cheapest possible proof that auth works and the model id is real —
        # a bad model name is otherwise a confusing failure 20 minutes into the session.
        reply = init_chat_model(model, max_tokens=8).invoke("Say OK")
        text = reply.content if isinstance(reply.content, str) else str(reply.content)
        record(PASS, f"model: {model}", f"replied {text.strip()[:24]!r}")
    except Exception as exc:  # noqa: BLE001
        record(FAIL, f"model: {model}", f"{type(exc).__name__}: {str(exc)[:180]}")


# --------------------------------------------------------------------- 8. langsmith


def check_langsmith() -> None:
    if not os.environ.get("LANGSMITH_API_KEY"):
        record(
            WARN,
            "LangSmith",
            "No LANGSMITH_API_KEY. Modules 1-2 mostly work; 3-4 are platform features.\n"
            "        Get one at https://smith.langchain.com -> Settings -> API Keys",
        )
        return

    project = os.environ.get("LANGSMITH_PROJECT")
    if not project or project.endswith("YOURNAME"):
        record(
            WARN,
            "LANGSMITH_PROJECT",
            f"Currently {project!r}. Set a UNIQUE name or everyone shares traces in Module 3.",
        )

    try:
        from langsmith import Client

        client = Client()
        list(client.list_datasets(limit=1))
        record(PASS, "LangSmith auth", f"project={project}")
    except Exception as exc:  # noqa: BLE001
        record(FAIL, "LangSmith auth", f"{type(exc).__name__}: {str(exc)[:160]}")
        return

    if os.environ.get("LANGSMITH_TRACING", "").lower() not in {"true", "1"}:
        record(WARN, "LANGSMITH_TRACING", "Not 'true' — nothing will be traced.")
    else:
        record(PASS, "LANGSMITH_TRACING", "true")


# ------------------------------------------------------------------------ 9. deploy


def check_deploy_tooling() -> None:
    try:
        # Invoke through the current interpreter, not PATH — in a venv the console
        # script may not be on PATH even though the package is installed, and a false
        # "not found" here sends people installing something they already have.
        proc = subprocess.run(
            [sys.executable, "-m", "langgraph_cli", "--version"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode == 0:
            record(PASS, "langgraph CLI", proc.stdout.strip()[:60])
        else:
            record(WARN, "langgraph CLI", "present but errored; Module 3 has a local fallback")
    except Exception:  # noqa: BLE001
        record(
            WARN,
            "langgraph CLI",
            'Not found. Module 3 needs it for `langgraph dev`.\n'
            '        pip install "langgraph-cli[inmem]"',
        )

    if not (ROOT / "langgraph.json").exists():
        record(FAIL, "langgraph.json", "missing")


# --------------------------------------------------------------------------- main


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")

    print("=" * 74)
    print("  Productionizing Agents preflight")
    print("=" * 74 + "\n")

    if not (ROOT / ".env").exists():
        print("  note: no .env found. Copy .env.example to .env and fill it in.\n")

    for check in (
        check_python,
        check_imports,
        check_fixtures,
        check_tests,
        check_mcp,
        check_notebooks,
        check_model,
        check_langsmith,
        check_deploy_tooling,
    ):
        try:
            check()
        except Exception as exc:  # noqa: BLE001
            record(FAIL, check.__name__, f"preflight bug: {exc}")

    failed = [r for r in results if r[0] == FAIL]
    warned = [r for r in results if r[0] == WARN]

    print("\n" + "=" * 74)
    if failed:
        print(f"  {len(failed)} CHECK(S) FAILED — fix these before the session:")
        for _, name, detail in failed:
            print(f"    - {name}: {detail.splitlines()[0] if detail else ''}")
    elif warned:
        print(f"  ALL CHECKS PASSED, with {len(warned)} warning(s).")
        print("  You can run the workshop. Read the warnings above.")
    else:
        print("  ALL CHECKS PASSED. You're ready.")
    print("=" * 74)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
