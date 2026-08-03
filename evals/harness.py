"""Harness tests. Deterministic, instant, no LLM.

WHAT ARE WE ACTUALLY TESTING?
-----------------------------
Once the application layer is tested (Module 1), what remains splits cleanly into two
components with completely different testing characteristics:

    THE HARNESS                                THE LLM UNDER OUR CONTEXT
    middleware, harness tools, context         the model's behavior given the system
    assembly, limits, interrupts                prompt, tool docstrings, and any
                                                dynamically injected files/skills/memories

    Deterministic. Instant. Free.               Stochastic. Slow. Costs money.
    -> plain unit tests                         -> datasets + evaluators

**Test the harness as a harness.** It is ordinary code and it deserves ordinary tests. Two
kinds, and most people write neither:

1.  **Component units.** Call the middleware directly with a fabricated failed tool call and
    assert what it does. You do not need an agent for this, let alone a model. That is
    `tests/test_middleware.py`, 25 tests, 0.1 seconds.

2.  **Context assembly assertions.** This module. Invoke the agent ONCE against a fake model
    that records what it received, then assert on the actual assembled request: was the
    system prompt what you think it is? Are all seven tools present? Did the harness inject
    `write_todos`? Did the skill/memory files land in context? Is the whole thing within
    budget?

WHY (2) MATTERS MORE THAN IT LOOKS
----------------------------------
The prompt the model receives is *assembled at runtime* from your system prompt, your tool
schemas, harness-injected tools, middleware rewrites, memories, and skill files. By the time
it reaches the model it has been through five layers of code you did not write today.

Nobody looks at it. People debug agent behavior by reading their own source and reasoning
about what the model *probably* got. Then it turns out a middleware truncated the system
prompt, or a tool description never made it because `parse_docstring` was off, or the skill
file silently failed to load.

Printing the assembled request once is often the entire debugging session. Asserting on it in
CI means that class of bug never reaches you again, and it costs nothing, because a fake
model answers instantly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage


@dataclass
class CapturedContext:
    """Everything the model actually received on its first call.

    This is the ground truth. Not what your source says, not what you intended, what
    arrived.
    """

    system_prompt: str = ""
    messages: list[Any] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    tool_descriptions: dict[str, str] = field(default_factory=dict)
    tool_arg_descriptions: dict[str, dict[str, str]] = field(default_factory=dict)
    model_name: str = ""
    captured: bool = False

    # ------------------------------------------------------------------ conveniences

    @property
    def full_prompt(self) -> str:
        """Everything the model sees, concatenated. What you grep when behavior is odd."""
        parts = [self.system_prompt]
        for message in self.messages:
            content = getattr(message, "content", "")
            parts.append(content if isinstance(content, str) else str(content))
        parts.extend(self.tool_descriptions.values())
        for args in self.tool_arg_descriptions.values():
            parts.extend(args.values())
        return "\n\n".join(p for p in parts if p)

    @property
    def approx_tokens(self) -> int:
        """Rough token count of the assembled context, chars/4.

        Deliberately crude. The point is not precision, it is having a number you can put a
        CI threshold on so context bloat gets caught while it is 20% instead of 300%. Tool
        schemas are the usual culprit: every tool you add taxes every single turn.
        """
        return len(self.full_prompt) // 4

    def contains(self, needle: str) -> bool:
        return needle.casefold() in self.full_prompt.casefold()

    def summary(self) -> str:
        lines = [
            f"model:            {self.model_name}",
            f"system prompt:    {len(self.system_prompt)} chars",
            f"messages:         {len(self.messages)}",
            f"tools:            {len(self.tool_names)}",
            f"approx tokens:    ~{self.approx_tokens:,}",
            "",
            "tools presented to the model:",
        ]
        for name in sorted(self.tool_names):
            described = len(self.tool_arg_descriptions.get(name, {}))
            first = (self.tool_descriptions.get(name) or "").strip().splitlines()
            lines.append(
                f"  {name:<28} {described} documented arg(s)  |  "
                f"{(first[0] if first else '(no description)')[:44]}"
            )
        return "\n".join(lines)


class ContextCapture(AgentMiddleware):
    """Records the first model request, then lets it proceed.

    `wrap_model_call` sits around the model call, so `request` is the fully assembled thing,
    after every other middleware has had its turn. That is exactly the object you want to
    assert on, and it is otherwise invisible.

    Only the FIRST call is captured. Later turns carry accumulated tool results and are
    noisier; the first request is where your static context lives.
    """

    def __init__(self, sink: CapturedContext) -> None:
        super().__init__()
        self.sink = sink

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        if not self.sink.captured:
            self._record(request)
            self.sink.captured = True
        return handler(request)

    def _record(self, request: Any) -> None:
        from aria.tools import arg_schema

        sink = self.sink

        system = getattr(request, "system_prompt", None)
        if system is None:
            system = getattr(request, "system_message", None)
        sink.system_prompt = getattr(system, "content", system) or ""
        if not isinstance(sink.system_prompt, str):
            sink.system_prompt = str(sink.system_prompt)

        sink.messages = list(getattr(request, "messages", []) or [])

        for tool in getattr(request, "tools", []) or []:
            name = getattr(tool, "name", None) or (
                tool.get("name") if isinstance(tool, dict) else None
            )
            if not name:
                continue
            sink.tool_names.append(name)
            sink.tool_descriptions[name] = getattr(tool, "description", "") or ""
            try:
                sink.tool_arg_descriptions[name] = {
                    arg: spec.get("description", "")
                    for arg, spec in arg_schema(tool).items()
                }
            except Exception:  # noqa: BLE001, a dict-shaped provider tool, etc.
                sink.tool_arg_descriptions[name] = {}

        model = getattr(request, "model", None)
        sink.model_name = (
            getattr(model, "model_name", None) or getattr(model, "model", None) or type(model).__name__
        )


class ToolAwareFakeModel(GenericFakeChatModel):
    """A fake model that tolerates `bind_tools`.

    `GenericFakeChatModel` raises `NotImplementedError` on `bind_tools`, and the agent binds
    tools on every model call, so the plain fake blows up right after our middleware has
    captured the request. Accepting the bind and ignoring it is all we need: we are asserting
    on what the tools *were*, not on the model choosing to call one.

    Returning `self` also means the fake never emits a tool call, so the agent takes exactly
    one turn and stops. That is the behavior you want for a context assertion, no loop, no
    branching, one deterministic pass.
    """

    reply: str = "Captured."

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        return self

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Any:  # noqa: ANN401
        """Answer from a plain string instead of `GenericFakeChatModel`'s message iterator.

        The base class does `next(self.messages)`, which makes the fake stateful and therefore
        sensitive to anyone *else* reading it. With `LANGSMITH_TRACING=true` the tracer does
        read it, to serialize the model, and drains it: a finite iterator is then exhausted
        before the agent's real call and `StopIteration` surfaces as a bare `RuntimeError`
        from inside LangGraph, while an infinite one hangs the tracer instead. Deriving the
        answer from a string sidesteps the whole question.
        """
        from langchain_core.outputs import ChatGeneration, ChatResult

        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])


def fake_model(reply: str = "Captured.") -> ToolAwareFakeModel:
    """A model that returns a canned reply without a network call.

    This is what makes these free. We are not testing the model here, we are testing what
    we hand it. Swapping in a fake makes the whole assertion instant and perfectly
    reproducible, which is the difference between a check that runs in CI and a check that
    runs when someone remembers to.

    `messages` is required by the base class but never read, see `_generate`.
    """
    return ToolAwareFakeModel(reply=reply, messages=iter(()))


def capture_context(
    question: str = "What's the lockout procedure for P-101A?",
    **build_kwargs: Any,
) -> CapturedContext:
    """Build ARIA, invoke it once against a fake model, and return what the model received.

    Args:
        question: The user turn. Matters only when you are asserting on dynamic context.
        **build_kwargs: Passed through to `aria.agent_v2.build_agent`. This is how you assert
            that a configuration change had the effect you intended, e.g.
            `capture_context(read_only=True)` and then check the write tools are gone. That
            assertion is worth having: "we thought that flag disabled the write tools" is a
            very believable production incident.

    Returns:
        A `CapturedContext`. Print `.summary()` when something is behaving oddly; assert on
        the fields in CI.
    """
    from langgraph.checkpoint.memory import InMemorySaver

    from aria.agent_v2 import build_agent, middleware_stack

    sink = CapturedContext()

    # Appended, so it is the INNERMOST middleware and sees the request after every other
    # layer has had its turn. That is the whole point, an outer position would capture the
    # request before the middleware you most want to verify has touched it.
    build_kwargs.setdefault("require_approval", False)
    build_kwargs["model"] = fake_model()
    build_kwargs.setdefault("checkpointer", InMemorySaver())

    # Built through `build_agent`, not assembled by hand, so the tests assert on the real
    # production assembly path rather than on a lookalike we wrote for the test.
    agent = build_agent(extra_middleware=[ContextCapture(sink)], **build_kwargs)

    agent.invoke(
        {"messages": [{"role": "user", "content": question}]},
        config={"configurable": {"thread_id": "level-0-capture"}},
    )
    return sink


def middleware_order() -> list[str]:
    """The configured middleware stack, outermost first.

    Worth asserting on. The list order in `create_deep_agent(middleware=[...])` is the
    nesting order, and getting it wrong is a real and subtle bug: `AnswerContractMiddleware`
    must run AFTER `ReasoningLeakMiddleware` or it grades an answer that still has reasoning
    in it. Nothing will tell you that is wrong except a test.
    """
    from aria.agent_v2 import middleware_stack

    return [type(m).__name__ for m in middleware_stack()]


if __name__ == "__main__":
    context = capture_context()
    print(context.summary())
    print("\nmiddleware (outermost first):")
    for i, name in enumerate(middleware_order(), 1):
        print(f"  {i}. {name}")
    print(f"\n--- system prompt as the model receives it ---\n{context.system_prompt}")
