"""Whole-account performance, one row per day."""

from __future__ import annotations

from . import Report

ACCOUNT = Report(
    name="account",
    description="Daily account totals: spend, impressions, clicks, reach.",
    level="account",
    fields=(
        "spend",
        "impressions",
        "clicks",
        "reach",
        "frequency",
        "ctr",
        "cpc",
        "cpm",
        "actions",
    ),
    time_increment=1,
)
