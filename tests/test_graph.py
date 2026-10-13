"""The transport: credentials, errors, paging, retry, and what must not be logged."""

from __future__ import annotations

import json

import pytest

from metaads_reporting.graph import (
    READ_METHODS,
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


def test_a_write_is_never_retried(monkeypatch):
    """A POST that timed out may already have landed. Repeating it is a second change.

    This is the asymmetry the whole no-retry-on-write rule exists for, and it is
    the reason an intent with no outcome in the ledger means "go and look".
    """
    monkeypatch.setattr("metaads_reporting.graph._sleep_backoff", lambda *a, **k: None)
    session = FakeSession([error_response(4, status=400)])
    with pytest.raises(GraphAPIError):
        transport(session).request("POST", "123", data={"status": "PAUSED"})
    assert len(session.calls) == 1, "a write was retried"


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
