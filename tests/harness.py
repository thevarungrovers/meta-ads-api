"""Build the MCP tool set against a simulated Graph API, in-process.

The tools are registered through a capturing registrar rather than a real
``MCPServer``, so each one can be CALLED directly with ordinary arguments. That
is what lets a test drive preview -> apply end to end without a transport, a
stdio handshake or a live account.
"""

from __future__ import annotations

from typing import Any

from metaads_mcp import tools_read, tools_write
from metaads_mcp.guardrails import Limits, SessionCounters, load_limits
from metaads_mcp.previews import PreviewStore
from metaads_mcp.server import ServerState


class Registrar:
    """Captures what would have been registered, keyed by function name."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}
        self.annotations: dict[str, Any] = {}

    def tool(self, **kwargs: Any):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            self.annotations[fn.__name__] = kwargs.get("annotations")
            return fn

        return decorate


def build_tools(sim, *, limits: Limits | None = None, monkeypatch=None):
    """Register every tool against ``sim`` and hand back (tools, state).

    ``requests.Session`` is replaced so BOTH clients -- the read one and the
    mutating one -- end up on the same simulated account, which is what makes
    "apply, then read it back" work.
    """
    import metaads_reporting.graph as graph

    if monkeypatch is not None:
        monkeypatch.setattr(graph.requests, "Session", lambda: sim)
    else:  # pragma: no cover - monkeypatch is always passed in this suite
        graph.requests.Session = lambda: sim

    resolved = limits or load_limits()
    state = ServerState(
        limits=resolved,
        counters=SessionCounters(resolved),
        previews=PreviewStore(),
    )
    registrar = Registrar()
    tools_read.register(registrar, state)
    tools_write.register(registrar, state)
    return registrar, state
