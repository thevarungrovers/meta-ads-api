"""Everything that can change a Meta ad account.

A separate subpackage so the import itself is a decision. The read side
(:mod:`metaads_reporting.client`) cannot reach any of this, and
``tests/test_readonly_guard.py`` asserts that nothing outside this package can
send an HTTP method that writes.

What lives here:

``client``     :class:`MutatingMetaAdsClient` -- the one write path, field-
               allowlisted, with Meta's own ``validate_only`` behind
               ``apply=False``.
``plan``       :class:`PlannedChange` -- a built-but-unsent write plus its diff.
               Covers creates and updates; ``execute_chain`` sequences a create
               that depends on another.
``lookup``     name resolution, with ambiguity refused rather than guessed.
``campaigns`` / ``adsets`` / ``ads``  the per-entity plan builders.
``creatives``  reading a creative's shape and rebuilding it with edited text.
``media``      inspecting and uploading an image. Video upload is refused.
``audiences``  saved audiences and lookalikes. A customer-list audience is
               structurally refused -- it would upload personal data.
``audit``      the mutation log.

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
from .plan import (  # noqa: F401
    FieldChange,
    PlannedChange,
    execute,
    execute_chain,
)

__all__ = [
    "MutatingMetaAdsClient",
    "execute_chain",
    "MutationError",
    "MutationResult",
    "GuardrailViolation",
    "PlannedChange",
    "FieldChange",
    "execute",
    "check_status",
    "check_writable",
]
