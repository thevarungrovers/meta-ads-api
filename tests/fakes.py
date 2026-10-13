"""Offline stand-ins for the Graph API.

Everything in the test suite runs with no network. These fakes answer at the
``requests.Session`` boundary rather than higher up, so the real
:class:`~metaads_reporting.graph.GraphTransport` -- its credential handling,
its error mapping, its paging, its redaction -- is exercised for real. A fake
that replaced the transport would test the tests.
"""

from __future__ import annotations

import json
from typing import Any


class FakeResponse:
    def __init__(
        self,
        payload: Any,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}
        self.content = body if body is not None else json.dumps(payload).encode()

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> Any:
        if self._payload is _NOT_JSON:
            raise ValueError("No JSON object could be decoded")
        return self._payload


_NOT_JSON = object()


def html_response(status: int = 502) -> FakeResponse:
    return FakeResponse(_NOT_JSON, status=status, body=b"<html>oops</html>")


def error_response(
    code: int, *, subcode: int | None = None, status: int = 400, message: str = "nope"
) -> FakeResponse:
    error: dict[str, Any] = {
        "message": message,
        "type": "OAuthException",
        "code": code,
        "fbtrace_id": "TRACE123",
    }
    if subcode is not None:
        error["error_subcode"] = subcode
    return FakeResponse({"error": error}, status=status)


class FakeSession:
    """A ``requests.Session`` that replays scripted responses.

    Records every call, so a test can assert on the METHOD -- which is the
    read/write boundary on the Graph API and therefore the thing most worth
    asserting on.
    """

    def __init__(self, responses: list[FakeResponse] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def request(self, method, url, params=None, data=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": dict(params or {}),
                "data": dict(data or {}),
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError(
                f"FakeSession ran out of scripted responses at call "
                f"{len(self.calls)}: {method} {url}"
            )
        return self.responses.pop(0)

    @property
    def methods(self) -> list[str]:
        return [c["method"] for c in self.calls]


def campaign(
    node_id="120200000000001",
    name="Brand - Prospecting",
    status="ACTIVE",
    daily_budget="25000",
    **extra,
) -> dict[str, Any]:
    """A campaign object shaped the way the Graph API returns one.

    Money is a STRING of minor units, because that is what Meta actually sends
    -- not an int, and not major units. A fake that returned 250.00 would hide
    the bug this whole package is shaped around.
    """
    row = {
        "id": node_id,
        "name": name,
        "status": status,
        "effective_status": status,
        "objective": "OUTCOME_SALES",
        "buying_type": "AUCTION",
        "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        "account_id": "1234567890",
    }
    if daily_budget is not None:
        row["daily_budget"] = daily_budget
    row.update(extra)
    return row


def ad_set(
    node_id="120200000000002",
    name="Broad 25-54",
    status="ACTIVE",
    daily_budget="5000",
    bid_strategy="LOWEST_COST_WITHOUT_CAP",
    bid_amount=None,
    campaign_id="120200000000001",
    **extra,
) -> dict[str, Any]:
    row = {
        "id": node_id,
        "name": name,
        "status": status,
        "effective_status": status,
        "campaign_id": campaign_id,
        "bid_strategy": bid_strategy,
        "optimization_goal": "OFFSITE_CONVERSIONS",
        "billing_event": "IMPRESSIONS",
    }
    if daily_budget is not None:
        row["daily_budget"] = daily_budget
    if bid_amount is not None:
        row["bid_amount"] = bid_amount
    row.update(extra)
    return row


def ad(
    node_id="120200000000003",
    name="Creative A",
    status="ACTIVE",
    effective_status=None,
    **extra,
) -> dict[str, Any]:
    row = {
        "id": node_id,
        "name": name,
        "status": status,
        "effective_status": effective_status or status,
        "adset_id": "120200000000002",
        "campaign_id": "120200000000001",
    }
    row.update(extra)
    return row


def account(currency="CAD", **extra) -> dict[str, Any]:
    row = {
        "id": "act_1234567890",
        "account_id": "1234567890",
        "name": "Test Account",
        "currency": currency,
        "timezone_name": "America/Toronto",
        "account_status": 1,
        "amount_spent": "123456",
        "balance": "0",
    }
    row.update(extra)
    return row


def page(items, next_url: str | None = None) -> FakeResponse:
    payload: dict[str, Any] = {"data": list(items)}
    if next_url:
        payload["paging"] = {"next": next_url}
    return FakeResponse(payload)


def insights_row(**extra) -> dict[str, Any]:
    """An insights row. Money is a decimal STRING in MAJOR units here.

    The opposite of :func:`campaign`, deliberately: this is the asymmetry the
    whole currency module exists for, and the fakes have to carry it or no test
    can catch getting it backwards.
    """
    row = {
        "spend": "123.45",
        "impressions": "10000",
        "clicks": "250",
        "reach": "8000",
        "frequency": "1.25",
        "ctr": "2.5",
        "cpc": "0.4938",
        "date_start": "2026-10-01",
        "date_stop": "2026-10-07",
    }
    row.update(extra)
    return row
