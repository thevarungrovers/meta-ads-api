"""Finding an entity by name, because that is what people can actually see.

Meta object IDs are long numbers that do appear in the Ads Manager URL, but
nobody reads them off a screen and nobody remembers one. Names are on every
row. So names are the primary way in here, with three behaviours that make that
safe:

* **An exact match wins outright.** "Retargeting" is a substring of
  "Retargeting - Broad", so without this rule the more specific campaign could
  shadow the one actually named.
* **Otherwise, case-insensitive substring.**
* **Ambiguity is never resolved silently.** Several matches raise
  :class:`AmbiguousMatch`, which carries the candidates so the caller can print
  them. Picking one for the user is how the wrong campaign gets paused.

THE FINAL MATCH HAPPENS IN PYTHON, case-folded. Meta's ``filtering``
``CONTAIN`` is used as a PRE-filter (:func:`edge_search`), because fetching
one page and filtering it afterwards silently missed every match past the
first page on an account with more objects than the limit. CONTAIN is not
documented as case-insensitive, so when the pre-filtered request finds nothing
the search falls back to walking the edge unfiltered (bounded) and matching
here -- a half-remembered capitalisation must not read as "no such campaign".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .client import MutationError


@dataclass(frozen=True)
class Candidate:
    """One possible match, enough to tell it apart from the others."""

    id: str
    name: str
    status: str
    context: str = ""

    def render(self) -> str:
        line = f"  {self.id:<20} {self.name}"
        extras = [part for part in (self.status, self.context) if part]
        if extras:
            line += f"  [{', '.join(extras)}]"
        return line


class NotFound(MutationError):
    """Nothing matched the search."""


def is_not_found(exc: BaseException) -> bool:
    """Whether a Graph error means "no such object (that this token can see)".

    Code 100 with subcode 33 is Meta's answer for both a missing object and a
    missing permission on it; code 803 is "some of the aliases you requested do
    not exist". Anything else -- a rate limit, an expired token -- is not.
    """
    code = getattr(exc, "code", None)
    subcode = getattr(exc, "subcode", None)
    return (code == 100 and subcode == 33) or code == 803


class AmbiguousMatch(MutationError):
    """Several things matched, and choosing between them is not ours to do."""

    def __init__(self, entity: str, needle: str, candidates: Sequence[Candidate]):
        self.entity = entity
        self.needle = needle
        self.candidates = list(candidates)
        listing = "\n".join(c.render() for c in self.candidates)
        super().__init__(
            f"{len(self.candidates)} {entity}s match {needle!r}:\n{listing}\n"
            f"Narrow the search, or pass the id directly."
        )


def match_by_name(
    rows: Sequence[dict[str, Any]],
    needle: str,
    *,
    entity: str,
    to_candidate,
) -> dict[str, Any]:
    """The single row whose ``name`` matches ``needle``.

    Exact first (case-insensitive), then substring. Raises rather than guessing.
    """
    if not needle.strip():
        raise MutationError("Search text is empty.")
    target = needle.strip().casefold()

    exact = [r for r in rows if (r.get("name") or "").strip().casefold() == target]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        # An exact match that is itself ambiguous is still ambiguous -- Meta
        # does not require names to be unique, and duplicated campaign names
        # are extremely common after a bulk duplicate in Ads Manager.
        raise AmbiguousMatch(entity, needle, [to_candidate(r) for r in exact])

    partial = [r for r in rows if target in (r.get("name") or "").casefold()]
    if not partial:
        raise NotFound(
            f"No {entity} matching {needle!r}. Names are matched exactly first, "
            f"then as a case-insensitive substring."
        )
    if len(partial) > 1:
        raise AmbiguousMatch(entity, needle, [to_candidate(r) for r in partial])
    return partial[0]


def edge_search(
    client,
    parent: str,
    edge: str,
    fields: tuple[str, ...],
    *,
    needle: str | None,
    limit: int,
    statuses: list[str],
    max_pages: int = 10,
) -> tuple[list[dict[str, Any]], bool]:
    """(rows, truncated) for an edge, narrowed by name and effective_status.

    Asks for ``limit + 1`` so ``truncated`` is a fact, not a guess: True means
    there are more matches than were returned.
    """
    import json

    base = {"effective_status": json.dumps(statuses)}

    def fetch(extra: dict[str, Any], want: int) -> list[dict[str, Any]]:
        return client.read.edge(
            parent, edge, fields,
            limit=min(max(want, 25), 500),
            max_items=want,
            max_pages=max_pages,
            **base, **extra,
        )

    if not needle:
        rows = fetch({}, limit + 1)
        return rows[:limit], len(rows) > limit

    target = needle.casefold()
    filtered = fetch(
        {"filtering": json.dumps([{"field": "name", "operator": "CONTAIN", "value": needle}])},
        limit + 1,
    )
    rows = [r for r in filtered if target in (r.get("name") or "").casefold()]
    if not rows:
        # CONTAIN may be case-sensitive: walk unfiltered, bounded, and fold here.
        walked = fetch({}, 200 * max_pages)
        rows = [r for r in walked if target in (r.get("name") or "").casefold()]
        if len(walked) >= 200 * max_pages:
            return rows[:limit], True
    return rows[:limit], len(rows) > limit
