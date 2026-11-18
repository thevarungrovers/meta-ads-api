"""Asynchronous insights: the one POST a READ needs.

A big report (an ad-level daily year, a breakdown across every ad) times out
as a synchronous GET. Meta's answer is an async report run: POST the same
parameters to ``<node>/insights``, get a ``report_run_id``, poll it, then read
its rows with GET. Creating the run is a POST, so it cannot live on the
read-only client -- it lives here, in the write package, and it can post to
exactly ONE kind of path: ``<node>/insights``. A report run changes nothing in
the account; ``tests/test_readonly_guard.py`` pins that this module posts to no
other edge.
"""

from __future__ import annotations

import re
from typing import Any

#: A Graph node id or an act_ path, and nothing else -- no slashes, no query.
_NODE = re.compile(r"^(act_)?\d+$")


def insights_path(node: str) -> str:
    """``<node>/insights``, refusing anything that is not a bare node id.

    >>> insights_path("act_1234567890")
    'act_1234567890/insights'
    >>> insights_path("123/adsets")
    Traceback (most recent call last):
    ...
    ValueError: ...
    """
    node = str(node).strip()
    if not _NODE.match(node):
        raise ValueError(
            f"{node!r} is not a node id; an async report run is created only on "
            "<node>/insights."
        )
    return f"{node}/insights"


def start(read_client, node: str, params: dict[str, Any]) -> str:
    """Create a report run for ``params`` on ``node``. Returns its id."""
    path = insights_path(node)
    response = read_client.transport.request("POST", path, data=dict(params))
    run_id = response.get("report_run_id")
    if not run_id:
        raise ValueError(f"Meta did not return a report_run_id: {response!r}")
    return str(run_id)


def status(read_client, run_id: str) -> dict[str, Any]:
    """Where a run is: async_status and async_percent_completion. A GET."""
    return read_client.node(
        str(run_id), ("id", "async_status", "async_percent_completion", "date_start", "date_stop")
    )


def rows(read_client, run_id: str, *, max_items: int) -> list[dict[str, Any]]:
    """A finished run's rows. A GET."""
    return list(read_client._paged(f"{run_id}/insights", max_items=max_items, limit=500))
