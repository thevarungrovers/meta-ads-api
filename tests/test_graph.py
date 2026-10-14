"""The transport: credentials, errors, paging, retry, and what must not be logged."""

from __future__ import annotations

import json

import pytest
import requests

from metaads_reporting.graph import (
    MAX_WRITE_RETRIES,
    RATE_LIMIT_BACKOFF_SECONDS,
    READ_METHODS,
    WRITE_RATE_LIMIT_WINDOW_SECONDS,
    backoff_delay,
    WRITE_METHODS,
    GraphAPIError,
    GraphError,
    GraphTransport,
    RateLimit,
    ReadOnlyViolation,
    _redact,
    appsecret_proof,
    assert_read_method,
)
from tests.fakes import FakeResponse, FakeSession, error_response, html_response, page


def transport(session, **kwargs):
    return GraphTransport(
        access_token="TOK",
        api_version="v23.0",
        base_url="https://graph.facebook.com",
        session=session,
        **kwargs,
    )


# --- the read/write boundary ------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "get", " Head "])
def test_read_methods_pass(method):
    assert assert_read_method(method) in READ_METHODS


@pytest.mark.parametrize("method", ["POST", "DELETE", "PUT", "PATCH", "", "nonsense"])
def test_everything_else_is_refused(method):
    with pytest.raises(ReadOnlyViolation):
        assert_read_method(method)


def test_the_two_method_sets_do_not_overlap():
    """If they did, the guard would admit a write."""
    assert not READ_METHODS & WRITE_METHODS


# --- URLs and credentials ---------------------------------------------------


def test_a_path_gets_the_version_a_full_url_does_not():
    t = transport(FakeSession())
    assert t.url("act_1/campaigns") == "https://graph.facebook.com/v23.0/act_1/campaigns"
    # A paging cursor arrives as a complete URL and must be followed verbatim.
    assert t.url("https://graph.facebook.com/v23.0/x?after=CURSOR") == (
        "https://graph.facebook.com/v23.0/x?after=CURSOR"
    )


def test_a_read_puts_credentials_in_the_query():
    session = FakeSession([FakeResponse({"id": "1"})])
    transport(session).get("me")
    assert session.calls[0]["params"]["access_token"] == "TOK"
    assert session.calls[0]["data"] == {}


def test_a_write_puts_credentials_in_the_body_not_the_url():
    """A budget in a query string is logged by every proxy in between."""
    session = FakeSession([FakeResponse({"success": True})])
    transport(session).request("POST", "123", data={"daily_budget": 5000})
    call = session.calls[0]
    assert call["params"] is None or call["params"] == {}
    assert call["data"]["access_token"] == "TOK"
    assert call["data"]["daily_budget"] == 5000


def test_appsecret_proof_is_sent_only_when_a_secret_is_configured():
    session = FakeSession([FakeResponse({"id": "1"})])
    transport(session).get("me")
    assert "appsecret_proof" not in session.calls[0]["params"]

    session = FakeSession([FakeResponse({"id": "1"})])
    transport(session, app_secret="SEKRIT").get("me")
    assert session.calls[0]["params"]["appsecret_proof"] == appsecret_proof(
        "TOK", "SEKRIT"
    )


def test_appsecret_proof_is_a_stable_hmac():
    assert appsecret_proof("a", "b") == appsecret_proof("a", "b")
    assert appsecret_proof("a", "b") != appsecret_proof("a", "c")


# --- what must never be logged ----------------------------------------------


def test_redaction_removes_every_credential():
    cleaned = _redact(
        {
            "access_token": "TOK",
            "appsecret_proof": "PROOF",
            "app_secret": "SEKRIT",
            "client_secret": "NOPE",
            "fields": "id,name",
        }
    )
    assert cleaned["fields"] == "id,name"
    for key in ("access_token", "appsecret_proof", "app_secret", "client_secret"):
        assert cleaned[key] == "<redacted>"


def test_an_error_names_the_path_and_never_the_query_string():
    """The query string is where the token is."""
    session = FakeSession([error_response(100)])
    with pytest.raises(GraphAPIError) as caught:
        transport(session).get("act_1/campaigns", fields="id")
    message = str(caught.value)
    assert "/v23.0/act_1/campaigns" in message
    assert "TOK" not in message
    assert "access_token" not in message


# --- error mapping ----------------------------------------------------------


def test_a_dead_token_is_identified_as_one_with_the_subcode_explained():
    session = FakeSession([error_response(190, subcode=460)])
    with pytest.raises(GraphAPIError) as caught:
        transport(session).get("me")
    error = caught.value
    assert error.is_token_problem
    assert error.code == 190 and error.subcode == 460
    assert "changed their password" in str(error)
    assert "SYSTEM USER" in str(error)


def test_a_permission_error_names_both_halves():
    """Scope and asset assignment give the same error; the fix must say so."""
    session = FakeSession([error_response(200)])
    with pytest.raises(GraphAPIError) as caught:
        transport(session).get("act_1")
    assert "ads_management" in str(caught.value)
    assert "Business Settings" in str(caught.value)


def test_the_fbtrace_id_survives_into_the_message():
    session = FakeSession([error_response(100)])
    with pytest.raises(GraphAPIError) as caught:
        transport(session).get("x")
    assert "TRACE123" in str(caught.value)
    assert caught.value.fbtrace_id == "TRACE123"


def test_error_user_msg_is_surfaced_because_it_is_usually_the_useful_part():
    response = FakeResponse(
        {
            "error": {
                "message": "Invalid parameter",
                "code": 100,
                "error_user_title": "Budget too low",
                "error_user_msg": "The minimum daily budget is $1.00",
            }
        },
        status=400,
    )
    with pytest.raises(GraphAPIError) as caught:
        transport(FakeSession([response])).get("x")
    assert "minimum daily budget" in str(caught.value)
    assert "Budget too low" in str(caught.value)


def test_a_non_json_body_says_so_rather_than_raising_a_decode_error():
    with pytest.raises(GraphAPIError) as caught:
        transport(FakeSession([html_response()])).get("x")
    assert "non-JSON body" in str(caught.value)


def test_a_200_carrying_an_error_object_is_still_an_error():
    """Meta does this. `response.ok` alone is not enough."""
    response = FakeResponse({"error": {"message": "nope", "code": 100}}, status=200)
    with pytest.raises(GraphAPIError):
        transport(FakeSession([response])).get("x")


# --- retry ------------------------------------------------------------------


def test_a_rate_limited_read_is_retried(monkeypatch):
    monkeypatch.setattr("metaads_reporting.graph._sleep_backoff", lambda *a, **k: None)
    session = FakeSession([error_response(4, status=400), FakeResponse({"id": "1"})])
    assert transport(session).get("me") == {"id": "1"}
    assert len(session.calls) == 2


def test_a_permanent_error_is_not_retried(monkeypatch):
    monkeypatch.setattr("metaads_reporting.graph._sleep_backoff", lambda *a, **k: None)
    session = FakeSession([error_response(100)])
    with pytest.raises(GraphAPIError):
        transport(session).get("me")
    assert len(session.calls) == 1


# --- which writes may be repeated, and which may never be ------------------
#
# Found live on 2026-10-13: an apply succeeded and the revert issued straight
# after it was refused with code 613 -- "1 call per 30 seconds" on a campaign
# node. The blanket "writes never retry" rule turned that into a fatal error and
# left the account in the changed state. The safety net failed at exactly the
# moment it was needed.
#
# The rule was too coarse. A rate limit is an UNAMBIGUOUS refusal -- Meta did
# nothing -- so repeating it is safe. A timeout or a 5xx is ambiguous in the
# dangerous direction and must still never be repeated.


def test_a_write_IS_retried_after_a_rate_limit(monkeypatch):
    """The live failure. Meta refused it outright, so nothing was applied."""
    monkeypatch.setattr("metaads_reporting.graph._sleep_backoff", lambda *a, **k: None)
    session = FakeSession(
        [error_response(613, subcode=4841018, status=400), FakeResponse({"success": True})]
    )
    assert transport(session).request("POST", "123", data={"status": "PAUSED"}) == {
        "success": True
    }
    assert len(session.calls) == 2


def test_a_write_is_NOT_retried_after_a_transport_failure(monkeypatch):
    """The request may have reached Meta with the answer lost on the way back.
    Repeating it makes a second change nobody asked for."""
    monkeypatch.setattr("metaads_reporting.graph._sleep_backoff", lambda *a, **k: None)

    class Dead:
        def __init__(self):
            self.calls = []

        def request(self, *args, **kwargs):
            self.calls.append(args)
            raise requests.ConnectionError("boom")

    session = Dead()
    with pytest.raises(GraphAPIError):
        transport(session).request("POST", "123", data={"status": "PAUSED"})
    assert len(session.calls) == 1, "a write was retried after a transport error"


def test_a_write_is_NOT_retried_after_a_5xx(monkeypatch):
    """A 500 can mean the write landed and the response was lost."""
    monkeypatch.setattr("metaads_reporting.graph._sleep_backoff", lambda *a, **k: None)
    session = FakeSession(
        [FakeResponse({"error": {"message": "x"}}, status=500), FakeResponse({"success": True})]
    )
    with pytest.raises(GraphAPIError):
        transport(session).request("POST", "123", data={"status": "PAUSED"})
    assert len(session.calls) == 1


def test_a_write_is_NOT_retried_after_an_ambiguous_transient_code(monkeypatch):
    """Codes 1 and 2 can be returned after the request was already processed."""
    monkeypatch.setattr("metaads_reporting.graph._sleep_backoff", lambda *a, **k: None)
    for code in (1, 2):
        session = FakeSession([error_response(code, status=400), FakeResponse({"success": True})])
        with pytest.raises(GraphAPIError):
            transport(session).request("POST", "123", data={"status": "PAUSED"})
        assert len(session.calls) == 1, f"code {code} was retried on a write"


def test_a_read_still_retries_everything_transient(monkeypatch):
    """A duplicate read costs only time, so the read budget stays wide."""
    monkeypatch.setattr("metaads_reporting.graph._sleep_backoff", lambda *a, **k: None)
    for failure in (
        error_response(2, status=400),
        FakeResponse({"error": {"message": "x"}}, status=500),
    ):
        session = FakeSession([failure, FakeResponse({"id": "1"})])
        assert transport(session).get("me") == {"id": "1"}
        assert len(session.calls) == 2


def test_write_rate_limit_retries_are_bounded(monkeypatch):
    monkeypatch.setattr("metaads_reporting.graph._sleep_backoff", lambda *a, **k: None)
    session = FakeSession([error_response(613, status=400) for _ in range(10)])
    with pytest.raises(GraphAPIError):
        transport(session).request("POST", "123", data={"status": "PAUSED"})
    assert len(session.calls) == MAX_WRITE_RETRIES + 1


def test_a_permanent_write_error_is_not_retried(monkeypatch):
    monkeypatch.setattr("metaads_reporting.graph._sleep_backoff", lambda *a, **k: None)
    session = FakeSession([error_response(100, status=400)])
    with pytest.raises(GraphAPIError):
        transport(session).request("POST", "123", data={"status": "PAUSED"})
    assert len(session.calls) == 1


def test_a_rate_limit_waits_longer_than_the_window_it_broke():
    """Meta's write limit on a campaign node is 1 call / 30s. An exponential
    backoff starting at 1s spends every retry inside the same window."""
    rate = GraphAPIError("x", status=400, code=613)
    first = backoff_delay(0, rate)
    second = backoff_delay(1, rate)
    ordinary = backoff_delay(0, GraphAPIError("x", status=400, code=100))

    assert first > WRITE_RATE_LIMIT_WINDOW_SECONDS, first
    assert second > first, "backoff did not grow"
    assert ordinary < WRITE_RATE_LIMIT_WINDOW_SECONDS, "non-rate-limit waited too long"


def test_jitter_can_only_lengthen_a_rate_limit_wait():
    """So a jittered wait can never dip back under the window."""
    rate = GraphAPIError("x", status=400, code=613)
    delays = [backoff_delay(0, rate) for _ in range(500)]
    assert min(delays) >= RATE_LIMIT_BACKOFF_SECONDS
    assert min(delays) > WRITE_RATE_LIMIT_WINDOW_SECONDS
    assert len(set(delays)) > 1, "no jitter at all -- callers would retry in lockstep"


def test_is_rate_limit_distinguishes_refusal_from_ambiguity():
    assert GraphAPIError("x", status=400, code=613).is_rate_limit
    assert GraphAPIError("x", status=429).is_rate_limit
    assert GraphAPIError("x", status=400, code=80004).is_rate_limit
    assert not GraphAPIError("x", status=500).is_rate_limit
    assert not GraphAPIError("x", status=400, code=2).is_rate_limit
    assert not GraphAPIError("x", status=400, code=100).is_rate_limit


def test_retries_are_bounded(monkeypatch):
    monkeypatch.setattr("metaads_reporting.graph._sleep_backoff", lambda *a, **k: None)
    session = FakeSession([error_response(4, status=400) for _ in range(10)])
    with pytest.raises(GraphAPIError):
        transport(session).get("me")
    assert len(session.calls) == 5  # 1 attempt + MAX_READ_RETRIES


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_http_statuses(monkeypatch, status):
    monkeypatch.setattr("metaads_reporting.graph._sleep_backoff", lambda *a, **k: None)
    session = FakeSession(
        [FakeResponse({"error": {"message": "x"}}, status=status), FakeResponse({"id": "1"})]
    )
    assert transport(session).get("me") == {"id": "1"}


# --- paging -----------------------------------------------------------------


def test_paging_follows_next_and_yields_every_item():
    session = FakeSession(
        [
            page([{"id": "1"}, {"id": "2"}], next_url="https://graph.facebook.com/next1"),
            page([{"id": "3"}]),
        ]
    )
    items = list(transport(session).paged("act_1/campaigns"))
    assert [i["id"] for i in items] == ["1", "2", "3"]


def test_the_next_url_is_followed_verbatim():
    """Rebuilding it drops fields from page two onwards."""
    session = FakeSession(
        [page([{"id": "1"}], next_url="https://graph.facebook.com/v23.0/x?after=CUR"), page([])]
    )
    list(transport(session).paged("act_1/campaigns", fields="id,name"))
    assert session.calls[1]["url"] == "https://graph.facebook.com/v23.0/x?after=CUR"


def test_paging_is_bounded():
    """An unbounded walk is slow, burns the rate limit, and looks like a hang."""
    endless = [
        page([{"id": str(i)}], next_url="https://graph.facebook.com/n") for i in range(10)
    ]
    session = FakeSession(endless)
    with pytest.raises(GraphError) as caught:
        list(transport(session).paged("act_1/campaigns", max_pages=3))
    assert "Stopped after 3 pages" in str(caught.value)


def test_an_empty_result_is_not_an_error():
    session = FakeSession([page([])])
    assert list(transport(session).paged("act_1/campaigns")) == []


# --- rate limit headers -----------------------------------------------------


def test_rate_limit_headers_are_parsed_and_the_worst_is_surfaced():
    headers = {
        "X-App-Usage": json.dumps({"call_count": 12, "total_cputime": 3}),
        "X-Ad-Account-Usage": json.dumps({"acc_id_util_pct": 71.5}),
        "X-Business-Use-Case-Usage": json.dumps(
            {"123": [{"call_count": 88, "total_time": 5}]}
        ),
    }
    limit = RateLimit.from_headers(headers)
    assert limit.worst_percent == 88
    assert limit.describe()["worst_percent_used"] == 88.0


def test_missing_or_malformed_headers_do_not_blow_up():
    assert RateLimit.from_headers({}).worst_percent == 0
    assert RateLimit.from_headers({"X-App-Usage": "not json"}).worst_percent == 0
    assert RateLimit.from_headers(None).worst_percent == 0


def test_the_transport_remembers_the_last_rate_limit():
    session = FakeSession(
        [FakeResponse({"id": "1"}, headers={"X-Ad-Account-Usage": '{"acc_id_util_pct": 42}'})]
    )
    t = transport(session)
    t.get("me")
    assert t.last_rate_limit.worst_percent == 42


# --- misc -------------------------------------------------------------------


def test_an_unsupported_method_is_refused_before_a_request_is_built():
    session = FakeSession()
    with pytest.raises(GraphError):
        transport(session).request("PUT", "x")
    assert session.calls == []


def test_a_bare_list_body_is_wrapped_so_callers_always_see_a_dict():
    session = FakeSession([FakeResponse([{"id": "1"}])])
    assert transport(session).get("x") == {"data": [{"id": "1"}]}


# --- page size vs intentional cap vs runaway guard --------------------------
#
# Hit on a live account with 1500+ ads: asking for one page raised instead of
# returning one page, because max_pages was doing double duty as both "how many
# I want" and "I did not mean to walk the whole account".


def test_max_items_stops_cleanly_without_raising():
    """A deliberate cap is not an error -- stopping is what was asked for."""
    session = FakeSession(
        [page([{"id": "1"}, {"id": "2"}, {"id": "3"}], next_url="https://graph.facebook.com/n")]
    )
    items = list(transport(session).paged("act_1/ads", max_items=2))
    assert [i["id"] for i in items] == ["1", "2"]


def test_max_items_stops_across_a_page_boundary():
    session = FakeSession(
        [
            page([{"id": "1"}, {"id": "2"}], next_url="https://graph.facebook.com/n"),
            page([{"id": "3"}, {"id": "4"}], next_url="https://graph.facebook.com/n2"),
        ]
    )
    items = list(transport(session).paged("act_1/ads", max_items=3))
    assert [i["id"] for i in items] == ["1", "2", "3"]


def test_max_items_does_not_fetch_a_page_it_does_not_need():
    """Stopping mid-page must not cost another request."""
    session = FakeSession(
        [page([{"id": "1"}, {"id": "2"}], next_url="https://graph.facebook.com/n")]
    )
    list(transport(session).paged("act_1/ads", max_items=1))
    assert len(session.calls) == 1


def test_max_pages_still_raises_because_that_one_is_accidental():
    """The caller did NOT ask to stop here, so silence would hide the gap."""
    endless = [
        page([{"id": str(i)}], next_url="https://graph.facebook.com/n") for i in range(10)
    ]
    with pytest.raises(GraphError) as caught:
        list(transport(FakeSession(endless)).paged("act_1/ads", max_pages=2))
    assert "max_items" in str(caught.value), "the message should offer the clean cap"


def test_max_items_and_max_pages_can_coexist():
    """The cap wins when it is reached first, and nothing raises."""
    endless = [
        page([{"id": str(i)}], next_url="https://graph.facebook.com/n") for i in range(10)
    ]
    items = list(transport(FakeSession(endless)).paged("act_1/ads", max_items=3, max_pages=50))
    assert len(items) == 3
