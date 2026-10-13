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

MATCHING HAPPENS IN PYTHON, NOT IN THE API, and that is deliberate. Meta's
``filtering`` ``CONTAIN`` operator is not documented as case-insensitive and
does not behave like Google's ``LIKE``, so a half-remembered capitalisation
silently returns nothing -- which reads as "no such campaign" rather than "your
shift key". Fetching the account's objects and matching here costs one page and
is predictable.
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
