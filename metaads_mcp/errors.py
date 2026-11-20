"""The one place an exception becomes a message the agent can read.

The MCP runtime carries a ToolError's message through to the caller as
"Error executing tool <name>: <message>", and replaces EVERY other exception
with a bare "Error executing tool <name>". An exception not converted here
therefore tells the agent nothing: it cannot see that the token expired or that
Meta asked it to wait, and will most likely send the same call again.

This used to be two copies (tools_read and tools_write) that disagreed about
which exceptions counted, and the read side never saw a Graph error at all --
the read client re-wrapped every one as a bare RuntimeError. Now there is one
mapper and it converts everything:

  * Meta failures get a PREFIX naming their class, so an agent can branch on
    it without parsing prose:

      AUTH_EXPIRED    code 190 -- the token is dead; the fix is a new token
      PERMISSION      10, 200-299 -- a scope or an account assignment missing
      RATE_LIMITED    retry_after=<seconds> -- wait that long, then retry
      NOT_FOUND       100/33, 803 -- no such object, or not visible to this token
      POLICY          an ad-review / policy refusal (1487xxx subcodes, 368)
      TOO_MUCH_DATA   insights code 1 "reduce the amount of data" -- narrow it
      INVALID         any other 100 -- the request itself is wrong
      TRANSIENT       codes 1/2 and 5xx -- try again later

  * deliberate refusals (a guardrail, a stale preview, the kill switch) pass
    through with their own wording, which names what to do;
  * anything else is a bug and says INTERNAL with its type, credentials
    redacted, rather than vanishing.
"""

from __future__ import annotations

import functools

from mcp.server.mcpserver.exceptions import ToolError

from metaads_reporting.redaction import redact_text

AUTH_EXPIRED = "AUTH_EXPIRED"
PERMISSION = "PERMISSION"
RATE_LIMITED = "RATE_LIMITED"
NOT_FOUND = "NOT_FOUND"
POLICY = "POLICY"
TOO_MUCH_DATA = "TOO_MUCH_DATA"
INVALID = "INVALID"
TRANSIENT = "TRANSIENT"
INTERNAL = "INTERNAL"

#: Waited out by default when Meta gives no estimate for a rate limit. Meta's
#: write limit on a campaign node is one call per 30 seconds.
DEFAULT_RETRY_AFTER = 60



def redact(text: str) -> str:
    """Credentials out of a message, the way graph._redact keeps them out of logs.

    >>> redact("GET /v23.0/me?access_token=EAAB123&fields=id")
    'GET /v23.0/me?access_token=<redacted>&fields=id'
    """
    return redact_text(text)


def _graph_error(exc: BaseException):
    """The Graph error behind ``exc``, following the cause chain."""
    seen = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "code", None) is not None or getattr(current, "status", None):
            if hasattr(current, "is_rate_limit"):
                return current
        current = current.__cause__ or current.__context__
    return None


def classify(exc: BaseException) -> str | None:
    """The prefix for a Meta failure, or None when ``exc`` is not one."""
    graph = _graph_error(exc)
    if graph is None:
        return None
    code, subcode, status = graph.code, graph.subcode, graph.status
    text = str(graph).lower()
    if code == 190 and "page access token" in text:
        return PERMISSION  # the token works; this call wants a Page's token
    if code == 190:
        return AUTH_EXPIRED
    if graph.is_rate_limit:
        return RATE_LIMITED
    if code in (10, 294) or (code is not None and 200 <= code <= 299):
        return PERMISSION
    if (code == 100 and subcode == 33) or code == 803:
        return NOT_FOUND
    if code == 368 or (subcode is not None and 1487000 <= subcode <= 1487999):
        return POLICY
    if code == 1 and "reduce the amount of data" in text:
        return TOO_MUCH_DATA
    if code == 100:
        return INVALID
    if code in (1, 2) or (status is not None and status >= 500) or code is None:
        return TRANSIENT
    return INVALID


def retry_after(exc: BaseException) -> int:
    graph = _graph_error(exc)
    value = getattr(graph, "retry_after", None) if graph is not None else None
    try:
        return max(1, int(value)) if value else DEFAULT_RETRY_AFTER
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER


def message(exc: BaseException) -> str:
    """The text a ToolError carries for ``exc``."""
    from metaads_mcp.guardrails import WritesDisabled
    from metaads_mcp.previews import PreviewExpired, PreviewStale
    from metaads_reporting.write.lookup import NotFound

    # Our own refusals first: their wording names the fix, and a stale preview
    # that happens to wrap a Graph error is still a stale preview.
    if isinstance(exc, (PreviewExpired, PreviewStale, WritesDisabled)):
        return redact(str(exc))
    prefix = classify(exc)
    if prefix is not None:
        graph = _graph_error(exc)
        text = redact(str(exc).strip())
        if prefix == AUTH_EXPIRED:
            hint = graph.explain() if hasattr(graph, "explain") else ""
            if hint and hint not in text:
                text = f"{text}\n  -> {hint}"
        if prefix == RATE_LIMITED:
            return f"{prefix} retry_after={retry_after(exc)}: {text}"
        if prefix == INVALID and getattr(graph, "user_message", None) and graph.user_message not in text:
            text = f"{text}\n  {graph.user_message}"
        return f"{prefix}: {text}"
    if isinstance(exc, NotFound):
        return f"{NOT_FOUND}: {redact(str(exc))}"
    if isinstance(exc, LookupError):
        # KeyError's str() is its repr, quotes and all.
        text = exc.args[0] if exc.args else str(exc)
        return f"{NOT_FOUND}: {redact(str(text))}"
    if isinstance(exc, (ValueError, RuntimeError)):
        # Argument refusals, guardrails, MutationError, CustomerDataRefused,
        # CurrencyError: each carries a message written for the reader.
        return redact(str(exc))
    return f"{INTERNAL} {type(exc).__name__}: {redact(str(exc))}"


def refusals_reach_the_caller(fn):
    """Re-raise anything a tool raises as ToolError, so its message survives."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - converted, never swallowed
            raise ToolError(message(exc)) from exc

    return wrapper
