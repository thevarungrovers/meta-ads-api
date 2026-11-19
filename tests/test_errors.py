"""Every failure reaches the agent, prefixed with its class.

Fake Graph errors for each class, through a READ tool and a WRITE tool. Before
the mapper, a read saw none of this: the read client re-wrapped every Graph
error as a bare RuntimeError, which the MCP runtime turned into "Error
executing tool find_campaigns" and nothing else.
"""

from __future__ import annotations

import inspect
import typing

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from metaads_mcp import errors
from tests.fakes import FakeResponse, error_response
from tests.harness import build_tools
from tests.test_mcp_write_path import CAMPAIGN_ID, make_sim

CASES = [
    (dict(code=190, subcode=463), "AUTH_EXPIRED"),
    # The token is fine; the call wants a different KIND of token.
    (dict(code=190, message="(#190) This method must be called with a Page Access Token"),
     "PERMISSION"),
    (dict(code=10), "PERMISSION"),
    (dict(code=200), "PERMISSION"),
    (dict(code=17), "RATE_LIMITED retry_after="),
    (dict(code=613, subcode=4841018), "RATE_LIMITED retry_after="),
    (dict(code=80004), "RATE_LIMITED retry_after="),
    (dict(code=100, subcode=33), "NOT_FOUND"),
    (dict(code=1, message="Please reduce the amount of data you're asking for"), "TOO_MUCH_DATA"),
    (dict(code=100, subcode=1487390, message="Ad rejected"), "POLICY"),
    (dict(code=100, message="Invalid parameter"), "INVALID"),
]


def failing_sim(code, subcode=None, message="nope"):
    sim = make_sim()
    original = sim.request

    def router(method, url, params=None, data=None, files=None, timeout=None):
        if "/act_" in url and url.rstrip("/").endswith("act_1234567890"):
            return original(method, url, params, data, files, timeout)  # currency
        return error_response(code, subcode=subcode, message=message)

    sim.request = router
    return sim


@pytest.mark.parametrize("error, prefix", CASES)
def test_a_read_tool_names_the_class(monkeypatch, error, prefix):
    registrar, _ = build_tools(failing_sim(**error), monkeypatch=monkeypatch)
    with pytest.raises(ToolError) as caught:
        registrar.tools["find_campaigns"]()
    assert str(caught.value).startswith(prefix), str(caught.value)


@pytest.mark.parametrize("error, prefix", CASES)
def test_a_write_tool_names_the_class(monkeypatch, error, prefix):
    registrar, _ = build_tools(failing_sim(**error), monkeypatch=monkeypatch)
    with pytest.raises(ToolError) as caught:
        registrar.tools["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    assert str(caught.value).startswith(prefix), str(caught.value)


def test_a_rate_limit_is_not_reported_as_a_missing_object(monkeypatch):
    """campaigns.find used to turn EVERY exception into NotFound."""
    registrar, _ = build_tools(failing_sim(code=17), monkeypatch=monkeypatch)
    with pytest.raises(ToolError) as caught:
        registrar.tools["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    assert "this token can see" not in str(caught.value)


def test_an_expired_token_says_how_to_fix_it(monkeypatch):
    registrar, _ = build_tools(failing_sim(code=190, subcode=463), monkeypatch=monkeypatch)
    with pytest.raises(ToolError) as caught:
        registrar.tools["find_ads"]()
    assert "expired" in str(caught.value).lower()


def test_a_bug_says_internal_and_never_leaks_a_token():
    @errors.refusals_reach_the_caller
    def broken():
        raise TypeError("boom at https://graph.facebook.com/v23.0/me?access_token=EAABsecret")

    with pytest.raises(ToolError) as caught:
        broken()
    text = str(caught.value)
    assert text.startswith("INTERNAL TypeError")
    assert "EAABsecret" not in text


def _dummy(annotation):
    base = typing.get_origin(annotation)
    if base is typing.Annotated:
        annotation = typing.get_args(annotation)[0]
    if annotation is int or annotation == (int | None):
        return 1
    if annotation is bool:
        return False
    if annotation is float:
        return 0.01
    if typing.get_origin(annotation) in (list,) or annotation is list:
        return ["1"]
    if typing.get_origin(annotation) is dict or annotation is dict:
        return {"age_min": 25}
    return "1"


def test_no_tool_ever_raises_a_bare_exception(monkeypatch):
    """Every registered tool, every transport call failing: ToolError or a value."""

    class Down:
        def request(self, *a, **kw):
            return FakeResponse({"error": {"message": "down", "code": 2}}, status=500)

    registrar, _ = build_tools(Down(), monkeypatch=monkeypatch)
    for name, fn in sorted(registrar.tools.items()):
        signature = inspect.signature(fn)
        hints = typing.get_type_hints(fn, include_extras=True)
        args = {
            p.name: _dummy(hints.get(p.name, str))
            for p in signature.parameters.values()
            if p.default is inspect.Parameter.empty
        }
        try:
            fn(**args)
        except ToolError:
            continue
        except Exception as exc:  # pragma: no cover - the failure being tested for
            pytest.fail(f"{name} raised a bare {type(exc).__name__}: {exc}")


def test_the_ad_library_gate_names_its_own_fix():
    from metaads_reporting.graph import GraphAPIError

    err = GraphAPIError(status=400, code=10, subcode=2332002,
                        message="Application does not have permission for this action")
    assert "www.facebook.com/ads/library/api" in err.explain()
    assert "assigned to this ad account" not in err.explain()


def test_a_page_token_190_does_not_blame_the_token():
    from metaads_reporting.graph import GraphAPIError

    err = GraphAPIError(status=400, code=190,
                        message="(#190) This method must be called with a Page Access Token")
    assert "token is fine" in err.explain()
