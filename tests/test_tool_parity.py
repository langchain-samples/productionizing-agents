"""The two tool transports must not drift.

`aria/tools.py` exposes the same five capabilities twice: in-process for notebooks and
evals, over MCP for production. That is a deliberate and useful arrangement, and it is also
exactly the kind of duplication that rots. Someone adds an argument to the MCP server, the
local adapter keeps the old signature, and now your evals measure a tool surface that no
longer exists in production. Your experiment results become confidently wrong.

This test is the thing that makes the duplication safe. It is slower than the rest of the
suite because it spawns the real server over stdio — worth it, and still under a second.

    pytest tests/test_tool_parity.py -q
"""

from __future__ import annotations

import pytest

from aria.tools import (
    describe_surface,
    local_tools,
    mcp_tools,
    missing_arg_descriptions,
    tool_surface,
)


# ------------------------------------------------------- every argument is documented
#
# These two are the regression guard for a gotcha that silently degrades tool calling:
# per-argument descriptions are NOT generated from your docstring by default. LangChain
# needs `@tool(parse_docstring=True)`; FastMCP needs `Annotated[..., Field(description=...)]`.
# Get it wrong and the model sees `{"title": "Tag", "type": "string"}` for an argument you
# wrote a paragraph about. It presents as "the model keeps passing the wrong format", which
# sends you looking at the model instead of the schema.


def test_every_local_argument_has_a_description() -> None:
    assert missing_arg_descriptions(local_tools()) == []


def test_every_mcp_argument_has_a_description() -> None:
    import asyncio

    assert missing_arg_descriptions(asyncio.run(mcp_tools(transport="stdio"))) == []


def test_argument_descriptions_match_across_transports() -> None:
    """The descriptions are duplicated between `aria/tools.py` docstrings and
    `aria_mcp/server.py` constants. Duplication guarded by a test is fine; duplication
    guarded by hope is how the two transports start telling the model different things."""
    import asyncio

    local = describe_surface(local_tools())
    remote = describe_surface(asyncio.run(mcp_tools(transport="stdio")))

    for name in sorted(local):
        for arg, description in local[name].items():
            assert arg in remote[name], f"{name}.{arg} missing from the MCP surface"
            # Compare on collapsed whitespace: the docstring wraps at 88 columns and the
            # constant wraps differently, which is cosmetic and not worth failing over.
            assert " ".join(description.split()) == " ".join(
                remote[name][arg].split()
            ), f"{name}.{arg} description drifted between transports"


@pytest.fixture(scope="module")
def local_surface() -> dict[str, list[str]]:
    return tool_surface(local_tools())


@pytest.fixture(scope="module")
def mcp_surface() -> dict[str, list[str]]:
    import asyncio

    return asyncio.run(_load())


async def _load() -> dict[str, list[str]]:
    return tool_surface(await mcp_tools(transport="stdio"))


def test_same_tool_names(local_surface, mcp_surface) -> None:
    assert sorted(local_surface) == sorted(mcp_surface)


def test_same_arguments_for_every_tool(local_surface, mcp_surface) -> None:
    for name in sorted(local_surface):
        assert local_surface[name] == mcp_surface[name], f"{name} signature drifted"


def test_the_expected_five_tools_exist(local_surface) -> None:
    """Pin the surface. If someone adds a sixth tool, this test failing is the prompt to
    go update the eval dataset and the trajectory evaluator too — which is the actual
    work, and the part that gets forgotten."""
    assert sorted(local_surface) == [
        "complete_work_order",
        "create_work_order",
        "get_equipment",
        "get_procedure",
        "get_tank_status",
        "list_equipment",
        "list_work_orders",
        "request_equipment_shutdown",
        "search_procedures",
    ]


def test_read_and_write_tools_are_partitioned() -> None:
    """The read/write split is load-bearing, not cosmetic: it is what lets Module 2 run
    read-only evals without any chance of filing work orders, and what lets `interrupt_on`
    be configured by category instead of by enumerating names in four places."""
    from aria.tools import DESTRUCTIVE_TOOLS, LOCAL_TOOLS, READ_TOOLS, WRITE_TOOLS

    assert not {t.name for t in READ_TOOLS} & {t.name for t in WRITE_TOOLS}
    assert len(LOCAL_TOOLS) == len(READ_TOOLS) + len(WRITE_TOOLS)
    assert set(DESTRUCTIVE_TOOLS) <= {t.name for t in WRITE_TOOLS}


def test_read_only_toolset_excludes_writes() -> None:
    from aria.tools import local_tools

    names = {t.name for t in local_tools(include_writes=False)}
    assert "create_work_order" not in names
    assert "request_equipment_shutdown" not in names
    assert "search_procedures" in names


@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("get_equipment", {"tag": "P-101"}),
        ("get_tank_status", {"tag": "T-999"}),
        ("get_procedure", {"procedure_id": "SOP-NOPE-000"}),
    ],
)
def test_error_shape_matches_across_transports(tool_name, args) -> None:
    """Both transports must surface the same recovery hint.

    This is the one that actually catches bugs. The local adapter returns a dict; MCP
    returns content blocks containing serialized JSON. If the error text does not survive
    that round trip, the agent loses its ability to recover from a bad tag — and you would
    only notice in production, because your evals run on the local path.
    """
    import asyncio
    import json

    local = next(t for t in local_tools() if t.name == tool_name)
    local_result = local.invoke(args)
    assert "error" in local_result

    async def over_mcp() -> str:
        tools = await mcp_tools(transport="stdio")
        remote = next(t for t in tools if t.name == tool_name)
        return await remote.ainvoke(args)

    raw = asyncio.run(over_mcp())
    text = json.dumps(raw) if not isinstance(raw, str) else raw
    assert "error" in text

    # The distinctive part of the recovery hint has to be present in both.
    hint = local_result["error"].split(".")[0]
    salient = max(hint.split(), key=len)
    assert salient in text
