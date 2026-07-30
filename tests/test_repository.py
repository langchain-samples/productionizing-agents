"""Tests for ARIA's application layer.

Read the assertions, not just the names. The point of this file in the workshop is that
it is *boring*: no API key, no model, no flakiness, no `pytest.approx` on a similarity
score. It runs in well under a second and it either passes or it does not.

That is the bar for the layer underneath your agent. When an agent misbehaves you want to
be able to say "the tools are correct" and have a green test run behind you, so that the
only remaining variable is the model. Debugging a non-deterministic agent on top of
under-tested tools means chasing two coupled failure modes at once, and you will spend
most of your time proving which one you are looking at.

    pytest tests/ -q
"""

from __future__ import annotations

import pytest

from aria_mcp.repository import (
    MAX_SEARCH_RESULTS,
    ARIALookupError,
    Repository,
    get_repository,
)


@pytest.fixture(scope="module")
def repo() -> Repository:
    return get_repository()


# --------------------------------------------------------------------- fixtures sanity


def test_fixture_data_loads(repo: Repository) -> None:
    assert len(repo.procedures) == 8
    assert len(repo.equipment) == 10
    assert len(repo.tanks) == 4


def test_every_procedure_has_provenance(repo: Repository) -> None:
    """Groundedness is unevaluable if the data has nothing to be grounded in."""
    for proc in repo.procedures:
        assert proc["id"]
        assert proc["revision"]
        assert proc["effective_date"]
        assert proc["body"].strip()


def test_every_referenced_procedure_exists(repo: Repository) -> None:
    """Referential integrity between the two fixtures.

    Equipment records point at procedure ids. If a pointer dangles, the agent will chase
    it, get an error, and either loop or invent. Catch it here, in CI, for free.
    """
    known = {p["id"] for p in repo.procedures}
    for item in repo.equipment:
        for proc_id in item["applicable_procedures"]:
            assert proc_id in known, f"{item['tag']} references unknown {proc_id}"


# --------------------------------------------------------------------- search


def test_search_by_topic_ranks_the_right_procedure_first(repo: Repository) -> None:
    results = repo.search_procedures("confined space entry vessel")
    assert results[0]["id"] == "SOP-CSE-003"


def test_search_by_equipment_tag_finds_everything_governing_that_asset(repo: Repository) -> None:
    ids = {r["id"] for r in repo.search_procedures("P-101A")}
    assert {"SOP-LOTO-014", "SOP-MECH-108"} <= ids


def test_search_is_deterministic(repo: Repository) -> None:
    """Same query, same order, every time. Otherwise your agent tests flake for reasons
    that have nothing to do with the model."""
    first = [r["id"] for r in repo.search_procedures("isolation pump")]
    for _ in range(5):
        assert [r["id"] for r in repo.search_procedures("isolation pump")] == first


def test_search_respects_limit(repo: Repository) -> None:
    assert len(repo.search_procedures("permit", limit=2)) == 2


def test_search_returns_empty_for_a_genuine_miss(repo: Repository) -> None:
    """An honest empty result. Distinct from an error — and the tool docstring tells the
    agent to say 'the library has nothing on this' rather than improvise."""
    assert repo.search_procedures("submarine periscope calibration") == []


@pytest.mark.parametrize("bad_query", ["", "  ", "ab"])
def test_search_rejects_too_short_queries(repo: Repository, bad_query: str) -> None:
    with pytest.raises(ARIALookupError, match="at least 3 characters"):
        repo.search_procedures(bad_query)


def test_search_rejects_stopword_only_queries(repo: Repository) -> None:
    with pytest.raises(ARIALookupError, match="only common words"):
        repo.search_procedures("what is the")


@pytest.mark.parametrize("bad_limit", [0, -1, MAX_SEARCH_RESULTS + 1, 100])
def test_search_rejects_out_of_range_limits(repo: Repository, bad_limit: int) -> None:
    with pytest.raises(ARIALookupError, match="limit must be between"):
        repo.search_procedures("lockout", limit=bad_limit)


def test_search_bounds_body_size(repo: Repository) -> None:
    """Tool output must not grow without bound. This is the test that stops a future
    10-page procedure from silently eating the context window."""
    from aria_mcp.repository import MAX_BODY_CHARS

    for result in repo.search_procedures("lockout"):
        assert len(result["body"]) <= MAX_BODY_CHARS + 200  # + truncation notice


def test_search_results_carry_a_ready_made_citation(repo: Repository) -> None:
    result = repo.search_procedures("hot work")[0]
    assert result["citation"] == "SOP-HW-021 Rev 5 (effective 2024-11-18)"


# --------------------------------------------------------------------- get_procedure


def test_get_procedure_returns_full_untruncated_text(repo: Repository) -> None:
    proc = repo.get_procedure("SOP-MECH-108")
    assert proc["truncated"] is False
    assert "barrier fluid" in proc["body"]


def test_get_procedure_is_case_insensitive(repo: Repository) -> None:
    assert repo.get_procedure("sop-cse-003")["id"] == "SOP-CSE-003"


def test_get_procedure_surfaces_revision_notes(repo: Repository) -> None:
    """SOP-CSE-003 Rev 12 tightened blinding requirements. An agent citing Rev 11 would
    be giving advice that is now unsafe, so the note has to reach the model."""
    proc = repo.get_procedure("SOP-CSE-003")
    assert "double-block-and-bleed" in proc["notes"]


def test_unknown_procedure_id_error_lists_valid_ids(repo: Repository) -> None:
    """The error message is an agent-recovery affordance, so assert on its content."""
    with pytest.raises(ARIALookupError) as exc_info:
        repo.get_procedure("SOP-FAKE-999")

    message = str(exc_info.value)
    assert "SOP-CSE-003" in message
    assert "do not guess" in message.casefold()


# --------------------------------------------------------------------- equipment


def test_get_equipment_returns_the_governing_procedures(repo: Repository) -> None:
    item = repo.get_equipment("P-101A")
    assert item["criticality"] == "A"
    assert "SOP-MECH-108" in item["applicable_procedures"]


def test_get_equipment_is_case_insensitive(repo: Repository) -> None:
    assert repo.get_equipment("p-101a")["tag"] == "P-101A"


def test_unknown_tag_error_suggests_near_matches(repo: Repository) -> None:
    """'P-101' is the single most likely thing a user or a model will say. The error has
    to point at P-101A and P-101B or the agent has no way to recover."""
    with pytest.raises(ARIALookupError) as exc_info:
        repo.get_equipment("P-101")

    message = str(exc_info.value)
    assert "P-101A" in message and "P-101B" in message


@pytest.mark.parametrize("bad_tag", ["pump 101", "P101A", "T-42-A", "the crude pump", "P-1A"])
def test_malformed_tags_are_rejected_with_the_expected_format(repo: Repository, bad_tag: str) -> None:
    with pytest.raises(ARIALookupError, match="not a valid equipment tag"):
        repo.get_equipment(bad_tag)


def test_empty_tag_is_rejected(repo: Repository) -> None:
    with pytest.raises(ARIALookupError, match="tag is required"):
        repo.get_equipment("")


def test_malformed_tag_error_redirects_tanks_to_the_right_tool(repo: Repository) -> None:
    with pytest.raises(ARIALookupError, match="get_tank_status"):
        repo.get_equipment("TANK 42")


def test_list_equipment_filters_by_unit_and_sorts_stably(repo: Repository) -> None:
    tags = [i["tag"] for i in repo.list_equipment("Crude Unit 1")]
    assert tags == sorted(tags)
    assert tags == ["C-401", "E-118", "P-101A", "P-101B"]


def test_list_equipment_unit_filter_is_case_insensitive(repo: Repository) -> None:
    assert repo.list_equipment("crude unit 1") == repo.list_equipment("Crude Unit 1")


def test_unknown_unit_error_lists_valid_units(repo: Repository) -> None:
    with pytest.raises(ARIALookupError) as exc_info:
        repo.list_equipment("Coker")
    assert "Reformer" in str(exc_info.value)


def test_list_equipment_returns_summaries_not_full_records(repo: Repository) -> None:
    """Orientation should be cheap. If this tool returned full records the agent would
    pay 8k tokens every time it wanted to resolve 'the crude pumps' to a tag."""
    assert set(repo.list_equipment()[0]) == {
        "tag", "description", "unit", "status", "criticality",
    }


# --------------------------------------------------------------------- tanks


def test_healthy_tank_has_no_warnings(repo: Repository) -> None:
    assert repo.get_tank_status("T-042")["data_quality_warnings"] == []


def test_suspect_gauge_and_active_receipt_both_warn(repo: Repository) -> None:
    """T-043 has a suspect ATG *and* a receipt in progress. Both must be flagged — an
    agent that reports one and drops the other is a partial-truth failure, which is
    harder to catch in review than an outright wrong answer."""
    warnings = repo.get_tank_status("T-043")["data_quality_warnings"]

    assert len(warnings) == 2
    assert "suspect" in warnings[0]
    assert "SOP-OPS-055" in warnings[0]
    assert "receipt is in progress" in warnings[1]


def test_level_near_high_high_alarm_warns_with_the_margin(repo: Repository) -> None:
    warnings = repo.get_tank_status("T-102")["data_quality_warnings"]
    assert any("0.6 ft below the high-high alarm" in w for w in warnings)


def test_warning_order_is_fixed(repo: Repository) -> None:
    """Assert the order, not just the set. Stable output means an LLM-judge evaluator
    scoring 'did it surface all warnings' does not drift run to run."""
    for _ in range(3):
        warnings = repo.get_tank_status("T-043")["data_quality_warnings"]
        assert warnings[0].startswith("Automatic Tank Gauging")


def test_unknown_tank_error_redirects_to_the_right_tool(repo: Repository) -> None:
    with pytest.raises(ARIALookupError) as exc_info:
        repo.get_tank_status("T-999")

    message = str(exc_info.value)
    assert "T-042" in message
    assert "get_equipment" in message


def test_asking_get_tank_status_for_a_pump_is_rejected(repo: Repository) -> None:
    with pytest.raises(ARIALookupError, match="get_equipment"):
        repo.get_tank_status("P-101A")


# --------------------------------------------------------------------- immutability


def test_repository_is_frozen(repo: Repository) -> None:
    """Shared cached state that callers can mutate is a bug you find in production at
    3am. Frozen dataclass, tuples not lists."""
    with pytest.raises(Exception):
        repo.procedures = ()  # type: ignore[misc]


def test_callers_cannot_mutate_the_underlying_records(repo: Repository) -> None:
    item = repo.get_equipment("P-101A")
    item["status"] = "ON FIRE"
    assert repo.get_equipment("P-101A")["status"] == "running"
