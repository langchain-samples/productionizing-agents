"""The ARIA application layer: plain, validated, deterministic access to plant data.

Design rules this module follows, and why each one matters when an agent is the caller:

1.  **An unknown identifier is an error, not an empty result.** An agent that receives
    `[]` will usually conclude "there is no such procedure" and tell the user so,
    confidently. An agent that receives "Unknown equipment tag 'P-101'. Valid tags
    starting with P-101: P-101A, P-101B" will retry correctly. Empty results are the
    single most common way a technically-correct API produces a lying agent.

2.  **Errors carry the fix.** Every error message names what was wrong AND what to do
    about it. The agent is the consumer of your error strings; write them for that reader.

3.  **Output is bounded.** `search_procedures` caps results and truncates bodies. An
    unbounded tool response is a context-window bug waiting for your largest customer.

4.  **Output is deterministic.** Stable sort order everywhere. Non-deterministic tool
    output makes an already non-deterministic system untestable.

5.  **Provenance travels with the data.** Every procedure result carries its id, revision,
    and effective date, so the agent can cite and so a reader can verify. You cannot
    evaluate groundedness if your tools do not return something to be grounded in.

6.  **No LLM in here.** Nothing in this module imports langchain. It is testable at
    microsecond speed with exact assertions, which is what makes the agent debuggable:
    when something goes wrong you get to say "the tools are correct" and mean it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

DATA_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "data"

# Bounds. Tool output that grows with your data is a latent incident.
MAX_SEARCH_RESULTS: Final[int] = 5
MAX_BODY_CHARS: Final[int] = 4000
MIN_QUERY_CHARS: Final[int] = 3

_TAG_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Z]{1,2}-\d{3}[A-Z]?$")

_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "before", "by", "can", "do", "does",
        "for", "from", "how", "i", "if", "in", "is", "it", "me", "of", "on", "or",
        "should", "that", "the", "then", "to", "we", "what", "when", "where", "which",
        "who", "why", "with", "you",
    }
)


class ARIALookupError(ValueError):
    """Raised when a caller asks for something that does not exist or is malformed.

    Carries a message written for an agent: what was wrong, and what to try instead.
    """


@dataclass(frozen=True, slots=True)
class Repository:
    """In-memory view of the plant reference data.

    Frozen and hashable so it can be cached and so no caller can mutate shared state
    out from under another. In a real deployment the constructor would take a DB
    connection or an API client; the method signatures would not change.
    """

    procedures: tuple[dict[str, Any], ...]
    equipment: tuple[dict[str, Any], ...]
    tanks: tuple[dict[str, Any], ...]

    # ---------------------------------------------------------------- procedures

    def search_procedures(self, query: str, limit: int = MAX_SEARCH_RESULTS) -> list[dict[str, Any]]:
        """Keyword search over the procedure library.

        Returns at most `limit` procedures, best match first, each with a truncated body
        and full provenance. Returns `[]` only when the query is well-formed but genuinely
        matches nothing, which is a real answer, unlike a malformed query.

        Raises:
            ARIALookupError: if the query is too short to be meaningful, or `limit` is
                outside 1..MAX_SEARCH_RESULTS.
        """
        query = (query or "").strip()
        if len(query) < MIN_QUERY_CHARS:
            raise ARIALookupError(
                f"Search query must be at least {MIN_QUERY_CHARS} characters; got "
                f"{len(query)!r}. Pass a topic such as 'confined space entry' or an "
                f"equipment tag such as 'P-101A'."
            )
        if not 1 <= limit <= MAX_SEARCH_RESULTS:
            raise ARIALookupError(
                f"limit must be between 1 and {MAX_SEARCH_RESULTS}; got {limit}."
            )

        terms = self._tokenize(query)
        if not terms:
            raise ARIALookupError(
                f"Search query {query!r} contains only common words. Include a specific "
                f"topic ('lockout', 'hot work', 'H2S') or an equipment tag."
            )

        scored: list[tuple[int, int, dict[str, Any]]] = []
        for index, proc in enumerate(self.procedures):
            score = self._score(proc, terms)
            if score > 0:
                # index as a tiebreaker keeps ordering stable and total
                scored.append((score, -index, proc))

        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
        return [self._as_result(proc) for _, _, proc in scored[:limit]]

    def get_procedure(self, procedure_id: str) -> dict[str, Any]:
        """Fetch one procedure in full by id, e.g. 'SOP-CSE-003'.

        Raises:
            ARIALookupError: if no such procedure exists. The message lists the valid ids,
                because a hallucinated id is the most likely reason you are here.
        """
        wanted = (procedure_id or "").strip().upper()
        for proc in self.procedures:
            if proc["id"].upper() == wanted:
                return self._as_result(proc, truncate=False)

        valid = ", ".join(sorted(p["id"] for p in self.procedures))
        raise ARIALookupError(
            f"No procedure with id {procedure_id!r}. Do not guess procedure ids. "
            f"The complete list is: {valid}. "
            f"Use search_procedures to find the right one by topic."
        )

    # ---------------------------------------------------------------- equipment

    def get_equipment(self, tag: str) -> dict[str, Any]:
        """Fetch the equipment record for a tag, e.g. 'P-101A'.

        Raises:
            ARIALookupError: if the tag is malformed or unknown. For unknown-but-well-formed
                tags the message suggests near matches, which is what lets an agent recover
                from 'P-101' -> 'P-101A' without a human in the loop.
        """
        wanted = (tag or "").strip().upper()
        if not wanted:
            raise ARIALookupError(
                "Equipment tag is required. Tags look like 'P-101A' (pump), 'V-205' "
                "(vessel), 'C-401' (column), 'K-501' (compressor)."
            )
        if not _TAG_PATTERN.match(wanted):
            raise ARIALookupError(
                f"{tag!r} is not a valid equipment tag. Expected a letter prefix, a "
                f"hyphen, three digits, and an optional trailing letter, for example "
                f"'P-101A' or 'V-205'. Did the user mean a tank? Tanks use get_tank_status."
            )

        for item in self.equipment:
            if item["tag"] == wanted:
                return dict(item)

        prefix = wanted.rstrip("ABCDEFGH") or wanted[:5]
        near = sorted(i["tag"] for i in self.equipment if i["tag"].startswith(prefix))
        hint = (
            f" Tags starting with {prefix!r}: {', '.join(near)}."
            if near
            else f" Known tags: {', '.join(sorted(i['tag'] for i in self.equipment))}."
        )
        raise ARIALookupError(f"No equipment with tag {wanted!r}.{hint}")

    def list_equipment(self, unit: str | None = None) -> list[dict[str, Any]]:
        """List equipment tags and descriptions, optionally filtered to one unit.

        Deliberately returns a summary, not full records: this is the tool an agent uses
        to orient itself, and it should not cost 8k tokens to do so.

        Raises:
            ARIALookupError: if `unit` is given but matches no known unit.
        """
        items = self.equipment
        if unit:
            wanted = unit.strip().casefold()
            items = tuple(i for i in items if i["unit"].casefold() == wanted)
            if not items:
                units = ", ".join(sorted({i["unit"] for i in self.equipment}))
                raise ARIALookupError(
                    f"No unit named {unit!r}. Known units: {units}."
                )

        return [
            {
                "tag": i["tag"],
                "description": i["description"],
                "unit": i["unit"],
                "status": i["status"],
                "criticality": i["criticality"],
            }
            for i in sorted(items, key=lambda i: i["tag"])
        ]

    # ---------------------------------------------------------------- tanks

    def get_tank_status(self, tag: str) -> dict[str, Any]:
        """Current level, temperature, gauging history, and inspection dates for a tank.

        The returned record includes `data_quality_warnings`: a list of strings describing
        anything a competent operator would want flagged (suspect gauge, receipt in
        progress, proximity to the high-high alarm). We compute these here, in tested
        code, rather than hoping the model notices, see Module 1 on determinism.

        Raises:
            ARIALookupError: if the tag is not a known tank.
        """
        wanted = (tag or "").strip().upper()
        for tank in self.tanks:
            if tank["tag"] == wanted:
                record = dict(tank)
                record["data_quality_warnings"] = self._tank_warnings(tank)
                return record

        valid = ", ".join(sorted(t["tag"] for t in self.tanks))
        raise ARIALookupError(
            f"No tank with tag {wanted!r}. Known tanks: {valid}. "
            f"Rotating equipment and vessels are in get_equipment, not here."
        )

    @staticmethod
    def _tank_warnings(tank: dict[str, Any]) -> list[str]:
        """Deterministic data-quality flags. Order is fixed so tests can assert on it."""
        warnings: list[str] = []

        if tank.get("atg_status") != "healthy":
            warnings.append(
                f"Automatic Tank Gauging is {tank.get('atg_status')}. Per SOP-OPS-055 "
                f"the reading requires manual verification before it is relied upon."
            )
        if tank.get("receipt_in_progress"):
            warnings.append(
                "A receipt is in progress. SOP-OPS-055 prohibits manual gauging within "
                "30 minutes of the start of a receipt; the level is actively changing."
            )

        level = tank.get("level_ft")
        alarm = tank.get("high_high_alarm_ft")
        if isinstance(level, (int, float)) and isinstance(alarm, (int, float)):
            margin = alarm - level
            if margin <= 1.0:
                warnings.append(
                    f"Level is {margin:.1f} ft below the high-high alarm setpoint of "
                    f"{alarm} ft. Escalate to the Movements group before any receipt."
                )

        return warnings

    # ---------------------------------------------------------------- internals

    @staticmethod
    def _tokenize(query: str) -> list[str]:
        raw = re.findall(r"[a-z0-9\-]+", query.casefold())
        return [t for t in raw if t not in _STOPWORDS and len(t) > 1]

    @staticmethod
    def _score(proc: dict[str, Any], terms: list[str]) -> int:
        """Weighted keyword score. Crude on purpose, it is exactly reproducible.

        A real deployment would use a vector store here. The lesson is unchanged: the
        retrieval layer is application code with its own tests and its own quality bar,
        not something to tune by reading agent transcripts.
        """
        title = proc["title"].casefold()
        applies = " ".join(proc.get("applies_to", [])).casefold()
        category = proc.get("category", "").casefold()
        body = proc["body"].casefold()
        proc_id = proc["id"].casefold()

        score = 0
        for term in terms:
            if term in proc_id:
                score += 12
            if term in applies:
                score += 8
            if term in title:
                score += 5
            if term in category:
                score += 3
            if term in body:
                score += 1
        return score

    @staticmethod
    def _as_result(proc: dict[str, Any], *, truncate: bool = True) -> dict[str, Any]:
        body = proc["body"]
        truncated = truncate and len(body) > MAX_BODY_CHARS
        if truncated:
            body = body[:MAX_BODY_CHARS] + (
                f"\n\n[...truncated. Call get_procedure('{proc['id']}') for the full text.]"
            )

        result = {
            "id": proc["id"],
            "title": proc["title"],
            "revision": proc["revision"],
            "effective_date": proc["effective_date"],
            "category": proc["category"],
            "applies_to": list(proc.get("applies_to", [])),
            "body": body,
            "truncated": truncated,
            # Precomputed so the agent has a correct citation to copy rather than one
            # to assemble. Never make the model do string formatting you can do for it.
            "citation": f"{proc['id']} Rev {proc['revision']} (effective {proc['effective_date']})",
        }
        if proc.get("notes"):
            result["notes"] = proc["notes"]
        return result


def _load(name: str) -> tuple[dict[str, Any], ...]:
    path = DATA_DIR / name
    if not path.exists():
        raise FileNotFoundError(
            f"Missing fixture {path}. Run this from the workshop repo root, or set "
            f"aria_mcp.repository.DATA_DIR."
        )
    with path.open(encoding="utf-8") as handle:
        return tuple(json.load(handle))


@lru_cache(maxsize=1)
def get_repository() -> Repository:
    """Load the repository once per process."""
    return Repository(
        procedures=_load("procedures.json"),
        equipment=_load("equipment.json"),
        tanks=_load("tank_readings.json"),
    )
