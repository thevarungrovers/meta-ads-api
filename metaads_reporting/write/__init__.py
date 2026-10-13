"""Everything that can change a Meta ad account.

A separate subpackage so the import itself is a decision. The read side
(:mod:`metaads_reporting.client`) cannot reach any of this, and
``tests/test_readonly_guard.py`` asserts that nothing outside this package can
send an HTTP method that writes.

What lives here:

``client``    :class:`MutatingMetaAdsClient` -- the one write path, field-
              allowlisted, with Meta's own ``validate_only`` behind
              ``apply=False``.
``plan``      :class:`PlannedChange` -- a built-but-unsent write plus its diff.
``lookup``    name resolution, with ambiguity refused rather than guessed.
``campaigns`` / ``adsets`` / ``ads``  the per-entity plan builders.
``audit``     the mutation log.

There is no delete anywhere in it, and no way to set a status to DELETED or
ARCHIVED. Both are irreversible through the API, and PAUSED does what anyone
actually wants from them.
"""

from __future__ import annotations

from .client import (  # noqa: F401
    GuardrailViolation,
    MutatingMetaAdsClient,
    MutationError,
    MutationResult,
    check_status,
    check_writable,
)
from .plan import FieldChange, PlannedChange, execute  # noqa: F401

__all__ = [
    "MutatingMetaAdsClient",
    "MutationError",
    "MutationResult",
    "GuardrailViolation",
    "PlannedChange",
    "FieldChange",
    "execute",
    "check_status",
    "check_writable",
]
