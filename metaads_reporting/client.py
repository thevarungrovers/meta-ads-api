"""The read-only Meta Marketing API client.

This client must not change anything. The ``ads_management`` scope a System
User token carries has no read-only variant worth relying on -- the same token
that pulls a report can pause a campaign -- so the restriction has to live
here, in code.

Three layers enforce it:

1. :func:`~metaads_reporting.graph.assert_read_method` -- every request this
   class sends goes through it, and it admits only GET and HEAD. On the Graph
   API the HTTP method *is* the read/write boundary, so this is not a proxy for
   the guarantee, it is the guarantee.
2. There is no method on this class that takes a body. A write needs a form
   payload, and nothing here can carry one.
3. ``tests/test_readonly_guard.py`` AST-parses every file outside ``write/``
   and fails on a call that could send a POST or a DELETE, so a write has to be
   added deliberately, inside that package.

**Mutations live in** :mod:`metaads_reporting.write`, a sibling package with
its own guard and its own confirmation step, which reuses
:func:`build_transport` rather than constructing a second client. Keep it that
way: adding a write here would end the guarantee this module exists to make.
"""

from __future__ import annotations

from typing import Any, Iterator

from . import logdb
from .account_id import account_path, normalize_account_id
from .config import Settings
from .currency import CurrencyError
from .fields import object_to_row
from .graph import GraphAPIError, GraphTransport, assert_read_method
from .insights import InsightsQuery, node_fields

#: Fields read off the ad account itself. ``currency`` is the important one:
#: nothing monetary in this package can be rendered without it, so it is
#: fetched once and cached per client rather than guessed at.
ACCOUNT_FIELDS = (
    "id",
    "account_id",
    "name",
    "currency",
    "timezone_name",
    "timezone_offset_hours_utc",
    "account_status",
    "business_name",
    "amount_spent",
    "balance",
    "spend_cap",
    "disable_reason",
)

#: ``account_status`` is an integer, and 1 is the only one that serves ads.
ACCOUNT_STATUS = {
    1: "ACTIVE",
    2: "DISABLED",
    3: "UNSETTLED",
    7: "PENDING_RISK_REVIEW",
    8: "PENDING_SETTLEMENT",
    9: "IN_GRACE_PERIOD",
    100: "PENDING_CLOSURE",
    101: "CLOSED",
    201: "ANY_ACTIVE",
    202: "ANY_CLOSED",
}


class MetaAdsReportingError(RuntimeError):
    """A read failed. Wraps the Graph error with the request's context."""


def build_transport(settings: Settings, *, session: Any | None = None) -> GraphTransport:
    """Construct the HTTP transport from ``settings``.

    The single place credentials become a client. The write package calls this
    too rather than re-reading config, so there stays exactly one construction
    path to audit.

    **This performs no network call.** Unlike an OAuth client, a Graph token is
    a bearer string -- there is nothing to exchange up front, so a bad token
    surfaces on the first request rather than here.
    ``scripts/test_connection.py`` accounts for that: the rung that constructs
    the client is NOT the rung that validates the credential.
    """
    return GraphTransport(
        access_token=settings.access_token,
        api_version=settings.api_version,
        base_url=settings.graph_base,
        app_secret=settings.app_secret,
        session=session,
    )


class ReadOnlyMetaAdsClient:
    """A Meta Ads client that can read and cannot write.

    Every path out of this class ends at :meth:`_get`, which refuses anything
    but GET before a request is built.
    """

    def __init__(self, transport: GraphTransport, settings: Settings) -> None:
        self._transport = transport
        self._settings = settings
        self._account_cache: dict[str, dict[str, Any]] = {}

    @classmethod
    def from_env(
        cls, *, settings: Settings | None = None, session: Any | None = None
    ) -> "ReadOnlyMetaAdsClient":
        resolved = settings or Settings.from_env()
        return cls(build_transport(resolved, session=session), resolved)

    # -- configuration ----------------------------------------------------

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def api_version(self) -> str:
        return self._settings.api_version

    @property
    def transport(self) -> GraphTransport:
        return self._transport

    def resolve_account_id(self, override: str | None = None) -> str:
        return self._settings.resolve_account_id(override)

    # -- the guarded request path -----------------------------------------

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        """One GET. The guard is here and nowhere else."""
        assert_read_method("GET")
        try:
            return self._transport.get(path, **params)
        except GraphAPIError as exc:
            raise MetaAdsReportingError(str(exc)) from exc

    def _paged(self, path: str, **params: Any) -> Iterator[dict[str, Any]]:
        assert_read_method("GET")
        try:
            yield from self._transport.paged(path, **params)
        except GraphAPIError as exc:
            raise MetaAdsReportingError(str(exc)) from exc

    # NOTE: `limit` is the page size, `max_items` is how many you actually
    # want, and `max_pages` is the runaway guard. See GraphTransport.paged.

    # -- the account ------------------------------------------------------

    def account(self, account_id: str | None = None) -> dict[str, Any]:
        """The ad account object, cached for the life of this client.

        Cached because ``currency`` is needed to render every monetary field in
        the package, which would otherwise be one extra request per preview.
        The fields here do not change during a session -- except
        ``amount_spent`` and ``balance``, which nothing makes a decision on.
        """
        target = self.resolve_account_id(account_id)
        if target not in self._account_cache:
            self._account_cache[target] = self._get(
                account_path(target), fields=node_fields(*ACCOUNT_FIELDS)
            )
        return self._account_cache[target]

    def currency(self, account_id: str | None = None) -> str:
        """The account's currency code. Every amount in this package needs it.

        Raises rather than defaulting: a missing currency means an unreadable
        budget, and a guessed one means a budget that is wrong by 100x in an
        approval prompt. See :mod:`metaads_reporting.currency`.
        """
        code = (self.account(account_id).get("currency") or "").strip().upper()
        if not code:
            raise CurrencyError(
                "The ad account did not report a currency, so no budget or bid "
                "on it can be read. Check the token can see the account: a "
                "partial field set usually means a missing permission rather "
                "than a missing value."
            )
        return code

    def account_summary(self, account_id: str | None = None) -> dict[str, Any]:
        """The account, rendered for a human: status named, money converted."""
        from .currency import from_minor_units

        data = self.account(account_id)
        code = self.currency(account_id)
        status = data.get("account_status")
        return {
            "account_id": normalize_account_id(data.get("account_id") or data.get("id")),
            "name": data.get("name"),
            "business": data.get("business_name"),
            "currency": code,
            "timezone": data.get("timezone_name"),
            "status": ACCOUNT_STATUS.get(status, f"UNKNOWN({status})"),
            "disable_reason": data.get("disable_reason"),
            "amount_spent": str(from_minor_units(data.get("amount_spent"), code) or 0),
            "balance": str(from_minor_units(data.get("balance"), code) or 0),
            "spend_cap": (
                str(from_minor_units(data.get("spend_cap"), code))
                if data.get("spend_cap") not in (None, "", "0")
                else None
            ),
        }

    def list_ad_accounts(self) -> list[dict[str, Any]]:
        """Every ad account this token can reach, via ``/me/adaccounts``.

        For a System User token this is the accounts it has been ASSIGNED in
        Business Settings -- which is usually far fewer than the business owns,
        and is the list that actually matters.
        """
        rows = list(
            self._paged(
                "me/adaccounts",
                fields=node_fields("id", "account_id", "name", "currency",
                                   "account_status", "timezone_name"),
                limit=200,
            )
        )
        return [
            {
                "account_id": normalize_account_id(
                    row.get("account_id") or row.get("id")
                ),
                "name": row.get("name"),
                "currency": row.get("currency"),
                "timezone": row.get("timezone_name"),
                "status": ACCOUNT_STATUS.get(
                    row.get("account_status"), f"UNKNOWN({row.get('account_status')})"
                ),
            }
            for row in rows
        ]

    # -- objects ----------------------------------------------------------

    def node(self, node_id: str, fields: tuple[str, ...]) -> dict[str, Any]:
        """One object by id. Raises if the id is unknown to this token."""
        return self._get(str(node_id), fields=node_fields(*fields))

    def edge(
        self,
        node_id: str,
        edge: str,
        fields: tuple[str, ...],
        *,
        limit: int = 100,
        max_items: int | None = None,
        max_pages: int = 20,
        **params: Any,
    ) -> list[dict[str, Any]]:
        """Objects on an edge, following paging.

        ``limit`` is the PAGE SIZE asked of Meta, ``max_items`` is how many you
        actually want back, and ``max_pages`` is the guard against walking a
        whole account by accident. Three different numbers; see
        :meth:`GraphTransport.paged` for why conflating the last two is a bug.
        """
        return list(
            self._paged(
                f"{node_id}/{edge}",
                fields=node_fields(*fields),
                limit=limit,
                max_items=max_items,
                max_pages=max_pages,
                **params,
            )
        )

    # -- insights ---------------------------------------------------------

    def insights(
        self,
        query: InsightsQuery,
        *,
        node_id: str | None = None,
        account_id: str | None = None,
        max_pages: int = 20,
    ) -> list[dict[str, Any]]:
        """Run ``query`` against an account (the default) or one object.

        Returns raw rows: money stays as Meta sent it. Use :meth:`insight_rows`
        for rows with the units already resolved.
        """
        target = node_id or account_path(self.resolve_account_id(account_id))
        with logdb.record_api_call(
            "GET",
            service="insights",
            account_id=self.resolve_account_id(account_id),
            path=f"{target}/insights",
            request=query.to_params(),
        ) as slot:
            rows = list(
                self._paged(
                    f"{target}/insights", max_pages=max_pages, **query.to_params()
                )
            )
            slot["row_count"] = len(rows)
        return rows

    def insight_rows(
        self,
        query: InsightsQuery,
        *,
        node_id: str | None = None,
        account_id: str | None = None,
        max_pages: int = 20,
    ) -> list[dict[str, Any]]:
        """Insights rows flattened to ``{field: value}``, units resolved.

        Columns follow ``query.fields``, plus ``date_start`` and ``date_stop``,
        which Meta returns on every row and which are the only way to tell what
        a row covers once ``time_increment`` is in play.
        """
        code = self.currency(account_id)
        rows = self.insights(
            query, node_id=node_id, account_id=account_id, max_pages=max_pages
        )
        columns = tuple(query.fields) + ("date_start", "date_stop")
        return [object_to_row(row, columns, code) for row in rows]

    # -- rate limit -------------------------------------------------------

    def rate_limit(self) -> dict[str, Any]:
        """Headroom as of the last response. Empty before the first call."""
        return self._transport.last_rate_limit.describe()
