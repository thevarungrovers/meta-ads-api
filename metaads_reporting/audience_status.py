"""Can an audience be used yet? Meta's two status codes, read honestly.

A custom audience carries two statuses: ``delivery_status`` (can ads deliver
to it) and ``operation_status`` (what Meta is doing with it). Each is
``{code, description}``. Only codes actually observed are mapped; an unknown
code is passed through VERBATIM with ``usable: None`` rather than guessed at --
a guessed "ready" is how an ad set gets pointed at an empty audience.

Observed:
  delivery 200   ready for use
  delivery 300   too small to deliver to
  operation 200  normal
  operation 441  still populating ("We're finding people...") -- Meta says it
                 can already run, so it is usable with a warning
"""

from __future__ import annotations

from typing import Any

READY = "ready"
POPULATING = "populating"
TOO_SMALL = "too small"


def _status(value: Any) -> tuple[int | None, str]:
    if isinstance(value, dict):
        code = value.get("code")
        try:
            code = int(code) if code is not None else None
        except (TypeError, ValueError):
            code = None
        return code, str(value.get("description") or "")
    return None, ""


def usability(row: dict[str, Any]) -> dict[str, Any]:
    """{usable: True/False/None, state, reason}.

    >>> usability({"delivery_status": {"code": 200}, "operation_status": {"code": 200}})["usable"]
    True
    >>> usability({"delivery_status": {"code": 300, "description": "too small"}})["usable"]
    False
    >>> usability({"operation_status": {"code": 999, "description": "odd"}})["usable"] is None
    True
    """
    d_code, d_text = _status(row.get("delivery_status"))
    o_code, o_text = _status(row.get("operation_status"))
    if d_code == 300:
        return {"usable": False, "state": TOO_SMALL, "reason": d_text or "too small to deliver to"}
    if d_code not in (None, 200):
        return {"usable": None, "state": f"delivery {d_code}", "reason": d_text or "unknown code"}
    if o_code == 441:
        return {"usable": True, "state": POPULATING,
                "reason": o_text or "still populating; Meta says it can already run"}
    if o_code not in (None, 200):
        return {"usable": None, "state": f"operation {o_code}", "reason": o_text or "unknown code"}
    if d_code is None and o_code is None:
        return {"usable": None, "state": "unknown", "reason": "Meta reported no status"}
    return {"usable": True, "state": READY, "reason": d_text or o_text or "ready"}


def approx_size(row: dict[str, Any]) -> str | None:
    low = row.get("approximate_count_lower_bound")
    high = row.get("approximate_count_upper_bound")
    if low is None and high is None:
        return None
    return str(low) if low == high else f"{low}-{high}"
