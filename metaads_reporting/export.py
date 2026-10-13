"""Turning report rows into DataFrames and files.

Kept separate from the client so the fetch scripts stay thin, and so output
formatting can be tested without a client at all.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd


def to_dataframe(
    rows: Iterable[dict[str, Any]], columns: Sequence[str]
) -> pd.DataFrame:
    """Build a DataFrame with exactly ``columns``, in order.

    Nested values -- ``actions``, ``action_values``, ``purchase_roas`` are all
    LISTS of dicts -- are JSON-encoded rather than exploded. A list in a cell
    survives a DataFrame but not a CSV, where it stringifies into Python repr
    and becomes unparseable; one JSON column round-trips.

    An empty result still yields a correctly-typed empty frame rather than a
    frame with no columns -- a day with no delivery is a valid answer, and the
    header should survive it.
    """
    selected = list(columns)
    flattened = [
        {
            key: (
                json.dumps(value, ensure_ascii=False)
                if isinstance(value, (list, dict))
                else value
            )
            for key, value in row.items()
        }
        for row in rows
    ]
    return pd.DataFrame(flattened, columns=selected)


def output_path(
    output_dir: Path, report_name: str, account_id: str, *, suffix: str = "csv"
) -> Path:
    """A timestamped path under ``output_dir``, creating the directory.

    The account ID and a timestamp are in the filename so exports from
    different accounts or runs never overwrite each other.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return output_dir / f"{report_name}_{account_id}_{stamp}.{suffix}"


def write_csv(frame: pd.DataFrame, path: Path) -> Path:
    """Write ``frame`` to ``path`` as UTF-8 CSV with no index column."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8")
    return path


def expand_actions(
    rows: Iterable[dict[str, Any]], action_types: Sequence[str]
) -> list[dict[str, Any]]:
    """Lift named action types out of the ``actions`` list into columns.

    ``actions`` is a list of ``{"action_type": ..., "value": "12"}``, which is
    unreadable in a spreadsheet and impossible to sort on. This pulls the ones
    asked for into ``action.<type>`` columns.

    An absent action type becomes ``None``, not ``0``. "Nobody purchased" and
    "Meta did not report purchases for this row" look identical in a zero and
    are different facts.
    """
    from .fields import action_value

    out = []
    for row in rows:
        copy = dict(row)
        actions = row.get("actions")
        for action_type in action_types:
            value = action_value(actions, action_type)
            copy[f"action.{action_type}"] = None if value is None else str(value)
        out.append(copy)
    return out
