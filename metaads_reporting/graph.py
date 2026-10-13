"""The Graph API transport: one HTTP call, one error model, one paging loop.

Shared by the read-only client and the mutating one, so there is exactly one
place where a token becomes a request and exactly one place where a Meta error
becomes something a human can act on.

**The read/write split is the HTTP method, and that is why this repo talks to
the Graph API directly rather than through the `facebook-business` SDK.** On
Meta, ``GET`` reads, ``POST`` creates *and updates*, and ``DELETE`` deletes --
so "can this code change the account?" is answerable by looking at one argument.
The SDK hangs ``.api_update()`` and ``.remote_delete()`` off every object it
hands back, which would put a write one attribute access away from every read
and leave nothing to grep for.

:func:`assert_read_method` is the check. :class:`GraphTransport` does not care
which method it is given; the GET-only guarantee lives in
:class:`~metaads_reporting.client.ReadOnlyMetaAdsClient`, which is the only
thing in this package outside ``write/`` allowed to construct a request at all.

ERRORS. Meta answers a failure with HTTP 400 and a JSON body far more useful
than the status: a numeric ``code``, an ``error_subcode``, and an
``fbtrace_id`` that Meta support can look up. :class:`GraphAPIError` keeps all
three, because "400 Bad Request" and "code 190 subcode 460, the token died when
somebody changed their password" are not the same problem and do not have the
same fix.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.parse import urlparse

import requests

from . import logdb

#: Methods that cannot change anything. The entire read-only guarantee of this
#: package reduces to: nothing outside ``write/`` passes anything else.
READ_METHODS = frozenset({"GET", "HEAD"})

#: Methods that can. POST covers BOTH create and update on the Graph API --
#: there is no PUT or PATCH -- so a POST is never "just an update".
WRITE_METHODS = frozenset({"POST", "DELETE"})

#: Everything :func:`assert_read_method` must refuse. The two write methods the
#: Graph API actually uses, plus the two REST verbs it does not -- a request
#: built with PUT or PATCH is a bug rather than a mutation, but the guard
#: refusing it is still the behaviour worth pinning. Exported so the connection
#: ladder can test the real set rather than a hardcoded copy of it that would
#: keep passing after somebody added a fifth.
NON_READ_METHODS = WRITE_METHODS | frozenset({"PUT", "PATCH"})

#: Error codes worth trying again. Meta returns these for transient conditions:
#:   1, 2     unknown / temporary service failure
#:   4, 17    application- and user-level rate limits
#:   32       page-level rate limit
#:   341      application limit reached
#:   613      calls-per-second limit
#:   80000+   the per-product Marketing API rate limits
RETRYABLE_CODES = frozenset({1, 2, 4, 17, 32, 341, 613})
RETRYABLE_CODE_RANGE = range(80000, 80100)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

#: A read is cheap to repeat; a write is not. The transport retries anyway --
#: but ONLY for GET, because a POST that timed out may well have landed, and
#: repeating it would make a second change nobody asked for. See `_retries_for`.
MAX_READ_RETRIES = 4
MAX_WRITE_RETRIES = 0

DEFAULT_TIMEOUT = 60.0

#: Hard ceiling on how many pages one call will follow. Without it a mistyped
#: filter walks an entire account one page at a time, which is slow, expensive
#: against the rate limit, and indistinguishable from a hang.
DEFAULT_MAX_PAGES = 50


class GraphError(RuntimeError):
    """Base for every failure this module raises."""


class ReadOnlyViolation(GraphError):
    """Something tried to send a method that can change the account."""


class GraphAPIError(GraphError):
    """Meta refused the request. Carries the parts worth acting on."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: int | None = None,
        subcode: int | None = None,
        error_type: str | None = None,
        fbtrace_id: str | None = None,
        user_title: str | None = None,
        user_message: str | None = None,
        path: str = "",
    ) -> None:
        self.status = status
        self.code = code
        self.subcode = subcode
        self.error_type = error_type
        self.fbtrace_id = fbtrace_id
        self.user_title = user_title
        self.user_message = user_message
        self.path = path
        super().__init__(message)

    @property
    def retryable(self) -> bool:
        if self.status in RETRYABLE_STATUS:
            return True
        if self.code is None:
            return False
        return self.code in RETRYABLE_CODES or self.code in RETRYABLE_CODE_RANGE

    @property
    def is_token_problem(self) -> bool:
        """Code 190 is always the token, and the subcode says how it died."""
        return self.code == 190

    def explain(self) -> str:
        """The fix, where the code implies one. Empty when it does not."""
        if self.code == 190:
            return {
                458: "The person who authorised this token removed the app.",
                460: (
                    "The token was invalidated because the user changed their "
                    "password. This is the single best argument for a SYSTEM "
                    "USER token: it has no password to change."
                ),
                463: "The token expired. Generate a new one.",
                464: "The user is not fully confirmed and cannot be used.",
                467: "The token is invalid — it was revoked, or never valid.",
            }.get(
                self.subcode or -1,
                "The access token is not usable. Check scripts/token_info.py.",
            )
        if self.code == 200 or self.code == 10:
            return (
                "A permission is missing. A System User token needs "
                "ads_management (or ads_read for reports) AND the System User "
                "must be assigned to this ad account in Business Settings. "
                "Both halves are required; either one alone gives exactly this."
            )
        if self.code == 100 and self.subcode == 33:
            return (
                "The object does not exist, or this token cannot see it. On "
                "Meta those are the same error on purpose, so check the id "
                "AND the account assignment."
            )
        if self.code in RETRYABLE_CODES or (self.code or 0) in RETRYABLE_CODE_RANGE:
            return (
                "A rate limit. Meta reports headroom in the "
                "X-Business-Use-Case-Usage header; see RateLimit.describe()."
            )
        return ""


@dataclass
class RateLimit:
    """What Meta said about headroom, lifted out of the response headers.

    Worth surfacing rather than ignoring: the Marketing API's limits are
    per-account and per-business, they are reported as a PERCENTAGE USED, and
    the useful moment to notice is at 80%, not at the 100% that starts
    returning errors.
    """

    app_usage: dict[str, Any] = field(default_factory=dict)
    business_usage: dict[str, Any] = field(default_factory=dict)
    account_usage: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_headers(cls, headers: Any) -> "RateLimit":
        def parse(name: str) -> dict[str, Any]:
            raw = headers.get(name) if headers else None
            if not raw:
                return {}
            try:
                value = json.loads(raw)
            except (TypeError, ValueError):
                return {}
            return value if isinstance(value, dict) else {}

        return cls(
            app_usage=parse("X-App-Usage"),
            business_usage=parse("X-Business-Use-Case-Usage"),
            account_usage=parse("X-Ad-Account-Usage"),
        )

    @property
    def worst_percent(self) -> float:
        """The highest utilisation percentage anywhere in the headers."""
        worst = 0.0
        for value in self.app_usage.values():
            if isinstance(value, (int, float)):
                worst = max(worst, float(value))
        for entries in self.business_usage.values():
            for entry in entries if isinstance(entries, list) else []:
                for key in ("call_count", "total_cputime", "total_time"):
                    value = entry.get(key) if isinstance(entry, dict) else None
                    if isinstance(value, (int, float)):
                        worst = max(worst, float(value))
        util = self.account_usage.get("acc_id_util_pct")
        if isinstance(util, (int, float)):
            worst = max(worst, float(util))
        return worst

    def describe(self) -> dict[str, Any]:
        return {
            "worst_percent_used": round(self.worst_percent, 1),
            "app": self.app_usage or None,
            "business": self.business_usage or None,
            "ad_account": self.account_usage or None,
        }


def assert_read_method(method: str) -> str:
    """Return ``method`` if it cannot change anything, else raise.

    The one check the read-only client's guarantee rests on.

    >>> assert_read_method("get")
    'GET'
    >>> assert_read_method("POST")
    Traceback (most recent call last):
    ...
    metaads_reporting.graph.ReadOnlyViolation: POST can change the account...
    """
    upper = (method or "").strip().upper()
    if upper not in READ_METHODS:
        raise ReadOnlyViolation(
            f"{upper} can change the account and is not permitted on the "
            f"read-only client. Allowed: {', '.join(sorted(READ_METHODS))}. "
            f"Mutations live in metaads_reporting.write."
        )
    return upper


def appsecret_proof(access_token: str, app_secret: str) -> str:
    """HMAC-SHA256 of the token, keyed by the app secret.

    Sent alongside the token when ``META_ADS_APP_SECRET`` is set. It means a
    stolen token on its own is not enough to call the API, because the proof
    cannot be computed without the secret -- which never leaves this process.
    """
    return hmac.new(
        app_secret.encode("utf-8"), access_token.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _redact(params: dict[str, Any]) -> dict[str, Any]:
    """Params with every credential removed, for the log and for an error.

    The token is a bearer credential with a very long life and no second
    factor. It must not reach the log database, a traceback, or a tool result.
    """
    hidden = {"access_token", "appsecret_proof", "app_secret", "client_secret"}
    return {
        key: ("<redacted>" if key in hidden else value)
        for key, value in (params or {}).items()
    }


class GraphTransport:
    """One HTTP session against the Graph API.

    Knows nothing about campaigns. Its whole job is: build the URL, attach the
    credentials, send it, retry what is worth retrying, and turn Meta's error
    body into a :class:`GraphAPIError` that says what to do.
    """

    def __init__(
        self,
        *,
        access_token: str,
        api_version: str,
        base_url: str,
        app_secret: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        session: Any | None = None,
    ) -> None:
        self._token = access_token
        self._proof = (
            appsecret_proof(access_token, app_secret) if app_secret else None
        )
        self.api_version = api_version
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = session or requests.Session()
        #: Headroom from the most recent response. Read it, do not trust it to
        #: be fresh -- it is only as current as the last call.
        self.last_rate_limit = RateLimit()

    # -- plumbing ----------------------------------------------------------

    def url(self, path: str) -> str:
        """Absolute URL for a Graph path. An absolute URL passes through.

        Paging cursors come back as fully-formed URLs, so this has to accept
        one; everything else is a path like ``act_123/campaigns``.
        """
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}/{self.api_version}/{path.lstrip('/')}"

    def _credentials(self) -> dict[str, str]:
        credentials = {"access_token": self._token}
        if self._proof:
            credentials["appsecret_proof"] = self._proof
        return credentials

    @staticmethod
    def _retries_for(method: str) -> int:
        """Reads retry; writes do not.

        A GET that times out can be repeated for free. A POST that times out
        may already have changed the account -- the request reached Meta and
        the response did not reach us -- so repeating it risks a second change
        nobody asked for. On a budget field a double apply is invisible; on
        anything incremental it is not. The write path handles failure by
        leaving an intent in the ledger with no outcome, which is the honest
        record of "this may have landed".
        """
        return MAX_READ_RETRIES if method in READ_METHODS else MAX_WRITE_RETRIES

    # -- the one request path ----------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send one request and return the decoded JSON body.

        ``params`` goes in the query string, ``data`` in the form body. Meta
        accepts either for most calls; writes use ``data`` so a budget does not
        end up in a URL, where it would be logged by every proxy in between.
        """
        method = (method or "GET").strip().upper()
        if method not in READ_METHODS | WRITE_METHODS:
            raise GraphError(f"Unsupported HTTP method {method!r}.")

        url = self.url(path)
        query = dict(params or {})
        body = dict(data or {})
        # Credentials go wherever the payload is, so a write keeps the token
        # out of the query string too.
        (body if method in WRITE_METHODS else query).update(self._credentials())

        attempts = self._retries_for(method) + 1
        last_error: GraphAPIError | None = None

        with logdb.record_api_call(
            method,
            service="graph",
            path=_log_path(url),
            request={"params": _redact(query), "data": _redact(body)},
        ) as slot:
            for attempt in range(attempts):
                try:
                    response = self._session.request(
                        method,
                        url,
                        params=query if method in READ_METHODS else None,
                        data=body if method in WRITE_METHODS else None,
                        timeout=self.timeout,
                    )
                except requests.RequestException as exc:
                    last_error = GraphAPIError(
                        f"{method} {_log_path(url)} could not be sent: "
                        f"{type(exc).__name__}: {exc}",
                        path=_log_path(url),
                    )
                    if attempt + 1 >= attempts:
                        raise last_error from exc
                    _sleep_backoff(attempt)
                    continue

                self.last_rate_limit = RateLimit.from_headers(response.headers)
                slot["rate_limit_pct"] = self.last_rate_limit.worst_percent

                payload = _decode(response, url)
                if response.ok and "error" not in payload:
                    slot["status"] = response.status_code
                    return payload

                last_error = _as_error(payload, response.status_code, url)
                slot["status"] = response.status_code
                if attempt + 1 >= attempts or not last_error.retryable:
                    raise last_error
                _sleep_backoff(attempt, last_error)

        raise last_error or GraphError(f"{method} {_log_path(url)} failed.")

    # -- reads --------------------------------------------------------------

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        """One GET. Does not page -- see :meth:`paged`."""
        return self.request("GET", path, params=params)

    def paged(
        self,
        path: str,
        *,
        max_pages: int = DEFAULT_MAX_PAGES,
        **params: Any,
    ) -> Iterator[dict[str, Any]]:
        """Yield every object across pages, following ``paging.next``.

        Cursor paging, not offset paging: Meta's ``next`` URL already carries
        the cursor and the original field list, so it is followed verbatim
        rather than rebuilt. Re-deriving it is how a field quietly goes missing
        from page two onwards.
        """
        page = self.request("GET", path, params=params)
        pages = 0
        while True:
            for item in page.get("data", []) or []:
                yield item
            pages += 1
            next_url = ((page.get("paging") or {}).get("next")) or ""
            if not next_url:
                return
            if pages >= max_pages:
                raise GraphError(
                    f"Stopped after {max_pages} pages of {path}. Narrow the "
                    f"filter, or raise max_pages deliberately -- an unbounded "
                    f"walk of a large account is slow and burns the rate limit."
                )
            # The next URL already carries the token; sending ours as well is
            # harmless but duplicative, and `request` adds it either way.
            page = self.request("GET", next_url)


def _decode(response: Any, url: str) -> dict[str, Any]:
    """The JSON body, or a GraphAPIError if it is not JSON at all.

    Meta serves an HTML error page for some infrastructure failures, and
    ``response.json()`` on one raises a decode error that names nothing useful.
    """
    try:
        payload = response.json()
    except ValueError:
        raise GraphAPIError(
            f"{_log_path(url)} returned {response.status_code} with a "
            f"non-JSON body ({len(response.content)} bytes). This is usually "
            f"an infrastructure error rather than an API one.",
            status=response.status_code,
            path=_log_path(url),
        ) from None
    if isinstance(payload, list):
        return {"data": payload}
    if not isinstance(payload, dict):
        return {"data": payload}
    return payload


def _as_error(payload: dict[str, Any], status: int, url: str) -> GraphAPIError:
    """Turn Meta's error body into something with a fix attached."""
    error = payload.get("error") or {}
    code = error.get("code")
    subcode = error.get("error_subcode")
    path = _log_path(url)

    parts = [
        f"Meta Graph API refused {path} (HTTP {status}"
        + (f", code {code}" if code is not None else "")
        + (f", subcode {subcode}" if subcode is not None else "")
        + ")."
    ]
    message = error.get("message") or payload.get("message") or ""
    if message:
        parts.append(f"  {message}")
    # error_user_msg is Meta's own plain-language version, and it is routinely
    # the only part that says what is actually wrong with the request.
    user_message = error.get("error_user_msg")
    if user_message:
        parts.append(f"  {error.get('error_user_title') or 'Details'}: {user_message}")

    built = GraphAPIError(
        "\n".join(parts),
        status=status,
        code=code if isinstance(code, int) else None,
        subcode=subcode if isinstance(subcode, int) else None,
        error_type=error.get("type"),
        fbtrace_id=error.get("fbtrace_id"),
        user_title=error.get("error_user_title"),
        user_message=user_message,
        path=path,
    )
    explanation = built.explain()
    if explanation:
        parts.append(f"  -> {explanation}")
    if built.fbtrace_id:
        parts.append(f"  fbtrace_id: {built.fbtrace_id}")
    # Rebuild with the full text; the first construction was only to reach
    # .explain(), which needs the code and subcode already parsed.
    return GraphAPIError(
        "\n".join(parts),
        status=built.status,
        code=built.code,
        subcode=built.subcode,
        error_type=built.error_type,
        fbtrace_id=built.fbtrace_id,
        user_title=built.user_title,
        user_message=built.user_message,
        path=built.path,
    )


def _log_path(url: str) -> str:
    """The path part of a URL, for logs and errors.

    Never the query string: that is where the access token is.
    """
    try:
        return urlparse(url).path or url
    except ValueError:  # pragma: no cover - defensive
        return "(unparseable url)"


def _sleep_backoff(attempt: int, error: GraphAPIError | None = None) -> None:
    """Exponential backoff with jitter.

    Jitter is not decoration: without it, several tools that hit the same rate
    limit retry in lockstep and hit it again together.
    """
    delay = min(2.0 ** attempt, 16.0)
    time.sleep(delay * (0.5 + random.random() / 2))
