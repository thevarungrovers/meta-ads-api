"""A small in-memory Graph API, for driving the write path offline.

More than a stub: it keeps an object store, honours the ``fields`` parameter
and APPLIES a POST to the object. That makes preview -> apply -> re-read a real
round trip, so a test can assert that the second preview sees the first one's
change -- which is the behaviour the staleness check depends on and which a
queue of canned responses cannot express.

Two details it reproduces on purpose, because the code is shaped around them:

* **A node returns ONLY the fields asked for.** There is no default field set
  beyond ``id``, so forgetting one reads as a missing value rather than an
  error. Honouring that here is what catches it.
* **Money is a STRING of minor units on a node, and a decimal string in major
  units on insights.** Getting those backwards is the central hazard of this
  API, and a simulator that normalised them would hide it.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from tests.fakes import FakeResponse


class GraphSim:
    """Routes requests against an in-memory account."""

    def __init__(
        self,
        *,
        account: dict[str, Any],
        objects: dict[str, dict[str, Any]] | None = None,
        edges: dict[str, dict[str, list[str]]] | None = None,
        insights: list[dict[str, Any]] | None = None,
    ) -> None:
        self.account = dict(account)
        self.objects = {k: dict(v) for k, v in (objects or {}).items()}
        #: parent id -> edge name -> child ids
        self.edges = edges or {}
        self.insights = list(insights or [])
        self.calls: list[dict[str, Any]] = []
        #: POSTs that were sent with validate_only, so a test can assert that a
        #: preview really did reach Meta and really did change nothing.
        self.validated: list[dict[str, Any]] = []
        self.applied: list[dict[str, Any]] = []
        #: Creates that really happened, and the multipart fields of the last call.
        self.created: list[dict[str, Any]] = []
        self.last_files: dict[str, Any] = {}
        #: Set to a FakeResponse to make the NEXT request fail.
        self.next_failure: FakeResponse | None = None

    # -- the requests.Session interface ------------------------------------

    def request(self, method, url, params=None, data=None, files=None, timeout=None):
        payload = dict(params or {}) or dict(data or {})
        self.last_files = dict(files or {})
        self.calls.append(
            {"method": method, "url": url, "payload": payload, "files": self.last_files}
        )

        if self.next_failure is not None:
            failure, self.next_failure = self.next_failure, None
            return failure

        path = urlparse(url).path
        segments = [s for s in path.split("/") if s]
        # Drop the version segment: /v23.0/act_1/campaigns -> act_1/campaigns
        if segments and segments[0].startswith("v"):
            segments = segments[1:]

        if method == "POST":
            return self._post(segments, dict(data or {}))
        return self._get(segments, dict(params or {}))

    # -- reads --------------------------------------------------------------

    def _get(self, segments: list[str], params: dict[str, Any]) -> FakeResponse:
        if not segments:
            return FakeResponse({"error": {"message": "no path", "code": 100}}, status=400)

        node, edge = segments[0], (segments[1] if len(segments) > 1 else None)
        fields = [f for f in (params.get("fields") or "").split(",") if f]

        if edge == "insights":
            return FakeResponse({"data": list(self.insights)})

        # The accounts a System User token has been ASSIGNED, which is what
        # /me/adaccounts actually answers -- usually far fewer than the business
        # owns. Handled here rather than wired into `edges` by every test,
        # because every test has exactly one account.
        if node == "me" and edge == "adaccounts" and "me" not in self.edges:
            return FakeResponse({"data": [self._project(self.account, fields)]})

        if edge:
            children = (self.edges.get(node) or {}).get(edge, [])
            return FakeResponse(
                {"data": [self._project(self.objects[c], fields) for c in children]}
            )

        if node.startswith("act_"):
            return FakeResponse(self._project(self.account, fields))
        if node == "me":
            return FakeResponse({"id": "SYSTEM_USER_1", "name": "Test System User"})
        if node in self.objects:
            return FakeResponse(self._project(self.objects[node], fields))

        # Meta's own behaviour: a missing object and a missing permission are
        # the same error, which is why the code never tells them apart either.
        return FakeResponse(
            {
                "error": {
                    "message": f"Unsupported get request. Object with ID '{node}' "
                    "does not exist, cannot be loaded due to missing "
                    "permissions, or does not support this operation.",
                    "code": 100,
                    "error_subcode": 33,
                    "type": "GraphMethodException",
                }
            },
            status=400,
        )

    @staticmethod
    def _project(obj: dict[str, Any], fields: list[str]) -> dict[str, Any]:
        """Only what was asked for, plus id -- exactly like the real API.

        Field expansion is handled: `likes.summary(true)` comes back under the
        key `likes`, not under the whole expression. A projection that matched
        the raw string would silently return nothing for every expanded field,
        which is how a social-proof count reads as zero.
        """
        if not fields:
            return {"id": obj.get("id")}
        wanted = {f.split(".", 1)[0].split("(", 1)[0] for f in fields}
        out = {k: v for k, v in obj.items() if k in wanted}
        out.setdefault("id", obj.get("id"))
        return out

    # -- writes -------------------------------------------------------------

    #: Account edges a create may post to, and the id prefix each hands back.
    CREATE_EDGES = {
        "adcreatives": "CREATIVE",
        "ads": "AD",
        "adimages": "IMAGE",
        "customaudiences": "AUD",
        "saved_audiences": "SAVED",
    }

    def _post(self, segments: list[str], body: dict[str, Any]) -> FakeResponse:
        node = segments[0]
        edge = segments[1] if len(segments) > 1 else None

        if edge in self.CREATE_EDGES:
            return self._create(edge, body)

        payload = {
            k: v
            for k, v in body.items()
            if k not in ("access_token", "appsecret_proof", "execution_options")
        }
        validate_only = "validate_only" in json.loads(
            body.get("execution_options") or "[]"
        )

        if node not in self.objects:
            return FakeResponse(
                {"error": {"message": f"no object {node}", "code": 100}}, status=400
            )

        record = {"node": node, "payload": payload}
        if validate_only:
            self.validated.append(record)
            return FakeResponse({"success": True})

        self.applied.append(record)
        for key, value in payload.items():
            # Meta stores budgets as a string of minor units, and returns them
            # as one. A write that sent an int comes back as a string.
            self.objects[node][key] = str(value) if key != "status" else value
        if "status" in payload:
            self.objects[node]["effective_status"] = payload["status"]
        return FakeResponse({"success": True})

    def _create(self, edge: str, body: dict[str, Any]) -> FakeResponse:
        """A create on an account edge. Mirrors the real responses."""
        payload = {
            k: v
            for k, v in body.items()
            if k not in ("access_token", "appsecret_proof", "execution_options")
        }
        validate_only = "validate_only" in json.loads(
            body.get("execution_options") or "[]"
        )
        record = {"edge": edge, "payload": payload}
        if validate_only:
            self.validated.append(record)
            # Meta returns success with NO id for a validate-only create --
            # nothing was made, so there is nothing to name.
            return FakeResponse({"success": True})

        self.created.append(record)
        new_id = f"{self.CREATE_EDGES[edge]}{len(self.created)}"

        if edge == "adimages":
            # Keyed by the FILE NAME the upload was sent under, not a fixed key.
            name = next(iter(self.last_files), "upload.png")
            return FakeResponse(
                {"images": {name: {"hash": f"hash{len(self.created)}", "url": "http://x"}}}
            )

        self.objects[new_id] = {"id": new_id, **payload}
        return FakeResponse({"id": new_id})

    # -- assertions helpers -------------------------------------------------

    @property
    def methods(self) -> list[str]:
        return [c["method"] for c in self.calls]

    @property
    def writes(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] == "POST"]
