"""ARIA — the agent.

This package contains the agent and nothing else: prompt, middleware, tool wiring, model
selection. All plant data access lives in `aria_mcp`, behind a tested API.

    agent_v1.py    The version everybody ships first. Module 1 dissects it.
    agent_v2.py    The version you would deploy. Modules 2-4 use this one.
    middleware.py  Deterministic guards for observed model failure modes.
    tools.py       Two transports (in-process and MCP) over the same application.
"""
