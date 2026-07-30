"""ARIA's eval suite — the test suite for a non-deterministic component.

The mapping to ordinary testing, which is the most useful thing to hold in your head:

    dataset     the parameterized cases          ~ fixtures / @pytest.mark.parametrize
    evaluator   an assertion                     ~ assert (code) or a fuzzy assert (judge)
    experiment  one run of the suite             ~ a pytest invocation, but recorded

Four levels, named for how much of the world is real rather than borrowing
unit/integration/e2e — because every agent test involves the whole agent, so "how many
components" is not the interesting axis:

    1  SMOKE      no tools, or stubs that always succeed.  datasets.py:SMOKE_EXAMPLES
    2  SCRIPTED   tool responses supplied by the dataset.  datasets.py:SCRIPTED_EXAMPLES
    3  STATEFUL   real mutable state, real side effects.   test_stateful.py
    4  SIMULATED  a second LLM playing the user, mult-turn. simulate.py

    mocking.py     clone a tool's contract, script its behavior
    evaluators.py  the assertions — code first, judges where a regex genuinely can't reach
    runner.py      aevaluate wiring and the model bake-off
"""
