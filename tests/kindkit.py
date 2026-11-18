"""A test rig for change kinds, on the simulator. Not a test module.

The engine is shared, so each kind's tests only need to show what is specific
to it: the preview renders before/after, Meta's validate_only is asked and
nothing is applied, a guardrail FAIL blocks, a stale value is refused, and the
inverse round-trips (a create's inverse is a pause).
"""

from __future__ import annotations

from metaads_mcp.guardrails import DEFAULTS, Limits
from tests.harness import build_tools


def rig(monkeypatch, sim, **limit_overrides):
    limits = Limits(**{**DEFAULTS, **limit_overrides})
    registrar, state = build_tools(sim, limits=limits, monkeypatch=monkeypatch)
    return registrar.tools, state


def preview_ok(tools, kind_name, sim, /, **args):
    """Preview, assert it is clean and that only validate_only reached Meta."""
    applied_before = len(sim.applied) + len(sim.created)
    preview = tools[f"preview_{kind_name}"](**args)
    assert not preview.blocked, preview.checks
    assert any(c.startswith(("OK   meta_validate_only", "OK  meta_validate_only"))
               or "meta_validate_only" in c for c in preview.checks), preview.checks
    assert len(sim.applied) + len(sim.created) == applied_before, "a preview changed something"
    return preview


def apply_and_revert(tools, kind_name, preview, /):
    result = tools[f"apply_{kind_name}"](preview_token=preview.preview_token)
    undo = tools["revert_change"](entry_id=result.entry_id)
    return result, undo


def failed(preview, check_name: str) -> bool:
    return any(line.startswith("FAIL") and f" {check_name}:" in line for line in preview.checks)
