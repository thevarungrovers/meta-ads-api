"""The SQLite log database: every tool call, API call and mutation.

One file, ``logs/meta-ads.db``, gitignored because the rows carry real ad
account ids and campaign names.

WHY IT LIVES IN ``metaads_reporting`` AND NOT ``metaads_mcp``: ``metaads_mcp``
imports this package, not the other way round. The library's own write path
(``write/audit.py``) has to log too, so the module both of them need belongs in
the lower layer. Putting it in the server package would invert the dependency
and make the library unusable without the MCP server installed.

FOUR TABLES, and the relationships are the point:

    tool_calls   one row per MCP tool invocation, or one CLI command
    api_calls    one row per outbound call to Meta, linked to the tool call
                 that caused it
    changes      what the MCP SERVER changed, with the before-value needed to
                 reverse it -- the ledger ``revert_change`` reads
    mutations    every write the LIBRARY sent from any caller, including the
                 ``execute_num_shares``-style no-op paths the changes table
                 never sees

``changes`` and ``mutations`` are deliberately two tables. They answer
different questions -- "what did the agent do that I might want to undo" versus
"what did this package send to Meta" -- and a single table would have to null
out half its columns for whichever kind of row it was holding.

TWO ERROR POLICIES, DELIBERATELY DIFFERENT:

  - Observability writes (tool_calls, api_calls) NEVER raise. A bug in the
    logger must not turn a working tool into a failure, and the MCP server's
    stderr is invisible in normal use, so a logging error would be both fatal
    and silent.

  - Ledger writes (changes, mutations) DO raise. These tables are the only
    record of a real-money mutation, and what ``revert_change`` reads to undo
    one. Losing a row quietly is worse than failing the write that would have
    produced it.

TWO-PHASE: the intent row goes in BEFORE the request leaves the process and the
outcome is filled in after it returns. A row whose outcome is unset is the
dangerous one -- the write started and nothing recorded how it ended, so it may
well have landed at Meta's end. That matters more here than on most APIs,
because the write path deliberately does NOT retry: a POST that timed out may
already have applied. That is an INSERT followed by an UPDATE, never an upsert:
in ``ON CONFLICT ... DO UPDATE``, ``excluded.col`` is the value that WOULD have
been inserted, so a clause written to preserve a column can silently overwrite
it. Two statements cannot express that bug.

CONCURRENCY. The MCP server is long-lived while ``scripts/manage.py`` runs in a
terminal, so two processes write here at once. WAL plus a busy timeout is what
makes that work; the default journal mode would hand the second writer
``database is locked``. ``synchronous=FULL`` because the ledger half of this
file is evidence.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import datetime as dt
import functools
import inspect
import json
import os
import socket
import sqlite3
import sys
import threading
import time
import typing
import uuid
from pathlib import Path
from typing import Any, Iterator

#: Resolved from the package, never from the working directory. The MCP server
#: is spawned by its client with THAT process's cwd, so a relative path here
#: would scatter one database per directory it happened to start in.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
DB_PATH = LOG_DIR / "meta-ads.db"

SCHEMA_VERSION = 1

#: Request payloads are capped before storage. A targeting spec is large and a
#: field list can be long; neither is worth unbounded rows, and the first few
#: thousand characters are what anyone reading the log back looks at.
MAX_JSON_CHARS = 8000

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY,
    started_at TEXT    NOT NULL,
    source     TEXT    NOT NULL,
    pid        INTEGER NOT NULL,
    host       TEXT,
    argv       TEXT
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER REFERENCES sessions(id),
    ts          TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    tool_name   TEXT    NOT NULL,
    args_json   TEXT,
    ok          INTEGER,
    error       TEXT,
    duration_ms INTEGER,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS api_calls (
    id             INTEGER PRIMARY KEY,
    tool_call_id   INTEGER REFERENCES tool_calls(id),
    session_id     INTEGER REFERENCES sessions(id),
    ts             TEXT    NOT NULL,
    source         TEXT    NOT NULL,
    service        TEXT,
    method         TEXT    NOT NULL,
    account_id     TEXT,
    path           TEXT,
    request_json   TEXT,
    row_count      INTEGER,
    http_status    INTEGER,
    rate_limit_pct REAL,
    ok             INTEGER,
    error          TEXT,
    duration_ms    INTEGER,
    finished_at    TEXT
);

-- What the MCP server changed. The ledger revert_change reads.
CREATE TABLE IF NOT EXISTS changes (
    entry_id                    TEXT PRIMARY KEY,
    tool_call_id                INTEGER REFERENCES tool_calls(id),
    at                          TEXT NOT NULL,
    pid                         INTEGER,
    tool                        TEXT NOT NULL,
    account_id                  TEXT,
    entity_type                 TEXT,
    entity_id                   TEXT,
    entity_name                 TEXT,
    field                       TEXT,
    before                      TEXT,
    after                       TEXT,
    projected_daily_spend_delta TEXT,
    ok                          INTEGER,
    outcome_at                  TEXT,
    resource_name               TEXT,
    error                       TEXT,
    reverts                     TEXT
);

-- Every write the library sent, from any caller.
CREATE TABLE IF NOT EXISTS mutations (
    correlation_id   TEXT PRIMARY KEY,
    tool_call_id     INTEGER REFERENCES tool_calls(id),
    at               TEXT NOT NULL,
    host             TEXT,
    pid              INTEGER,
    account_id       TEXT,
    node_id          TEXT,
    method           TEXT,
    edge             TEXT,
    field_count      INTEGER,
    payload_json     TEXT,
    ok               INTEGER,
    outcome_at       TEXT,
    response_json    TEXT,
    error            TEXT
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_ts    ON tool_calls(ts);
CREATE INDEX IF NOT EXISTS idx_api_calls_ts     ON api_calls(ts);
CREATE INDEX IF NOT EXISTS idx_api_calls_tool   ON api_calls(tool_call_id);
CREATE INDEX IF NOT EXISTS idx_changes_at       ON changes(at);
CREATE INDEX IF NOT EXISTS idx_changes_reverts  ON changes(reverts);
CREATE INDEX IF NOT EXISTS idx_mutations_at     ON mutations(at);
"""

# One connection per thread. The MCP SDK runs sync tools on a worker thread, and
# a sqlite3 connection may only be used from the thread that created it.
_local = threading.local()
_session_id: int | None = None
_session_lock = threading.Lock()

#: The tool call currently in flight, so an API call can name its parent. A
#: ContextVar rather than a global because anyio copies the context into the
#: worker thread it runs a sync tool on, which a threading.local would not
#: survive and a plain global would get wrong under concurrency.
_current_tool_call: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "metaads_current_tool_call", default=None
)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    try:
        text = json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = json.dumps(str(value))
    if len(text) > MAX_JSON_CHARS:
        return text[:MAX_JSON_CHARS] + f"... [truncated, {len(text)} chars]"
    return text


def _loads(raw: str | None) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return raw


def connect() -> sqlite3.Connection:
    """The calling thread's connection, opening and migrating it on first use."""
    existing = getattr(_local, "conn", None)
    if existing is not None:
        return existing

    LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(LOG_DIR, 0o700)
    except OSError:
        pass

    fresh = not DB_PATH.exists()
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL lets a CLI write while the server holds a read; busy_timeout turns a
    # concurrent writer from an exception into a short wait. FULL because the
    # ledger tables are the mutation record, not just telemetry.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO NOTHING",
        (str(SCHEMA_VERSION),),
    )
    if fresh:
        try:
            os.chmod(DB_PATH, 0o600)
        except OSError:
            pass

    _local.conn = conn
    return conn


def reset_for_tests(path: Path | None = None) -> Path:
    """Point the module at a fresh database and drop the cached connection.

    The connection is cached per thread and the session row per process, so a
    test that only reassigns DB_PATH would keep writing to its predecessor's
    file.
    """
    global _session_id, LOG_DIR, DB_PATH
    if path is not None:
        DB_PATH = path
        LOG_DIR = path.parent
    _local.__dict__.pop("conn", None)
    _session_id = None
    return DB_PATH


def session_id(source: str = "mcp") -> int:
    """This process's row in `sessions`, created once and reused."""
    global _session_id
    if _session_id is not None:
        return _session_id
    with _session_lock:
        if _session_id is not None:
            return _session_id
        cursor = connect().execute(
            "INSERT INTO sessions(started_at, source, pid, host, argv) VALUES(?,?,?,?,?)",
            (_now(), source, os.getpid(), socket.gethostname(), _dumps(sys.argv)),
        )
        _session_id = int(cursor.lastrowid)
    return _session_id


def _source_default() -> str:
    """`mcp` under the server, `cli` otherwise.

    argv[0] is the only thing that distinguishes them: the server is launched
    as an absolute path ending in `metaads_mcp/server.py`, every other entry
    point is a script under `scripts/`.
    """
    return "mcp" if Path(sys.argv[0]).name == "server.py" else "cli"


def current_tool_call() -> int | None:
    return _current_tool_call.get()


# --- observability: never raises ------------------------------------------


@contextlib.contextmanager
def record_tool_call(tool_name: str, args: dict[str, Any] | None = None) -> Iterator[None]:
    """Log one tool invocation, and make it the parent of its API calls.

    Swallows its own failures: see the module docstring. The tool body runs
    whether or not any of this worked.
    """
    source = _source_default()
    started = time.monotonic()
    row_id: int | None = None
    try:
        cursor = connect().execute(
            "INSERT INTO tool_calls(session_id, ts, source, tool_name, args_json) "
            "VALUES(?,?,?,?,?)",
            (session_id(source), _now(), source, tool_name, _dumps(args)),
        )
        row_id = int(cursor.lastrowid)
    except Exception:
        row_id = None

    token = _current_tool_call.set(row_id)
    try:
        yield
    except BaseException as exc:
        _finish_tool_call(row_id, started, ok=False, error=f"{type(exc).__name__}: {exc}")
        raise
    else:
        _finish_tool_call(row_id, started, ok=True, error=None)
    finally:
        _current_tool_call.reset(token)


def _finish_tool_call(row_id: int | None, started: float, *, ok: bool, error: str | None) -> None:
    if row_id is None:
        return
    try:
        connect().execute(
            "UPDATE tool_calls SET ok=?, error=?, duration_ms=?, finished_at=? WHERE id=?",
            (1 if ok else 0, error, int((time.monotonic() - started) * 1000), _now(), row_id),
        )
    except Exception:
        pass


@contextlib.contextmanager
def record_api_call(
    method: str,
    *,
    service: str | None = None,
    account_id: str | None = None,
    path: str | None = None,
    request: Any = None,
) -> Iterator[dict[str, Any]]:
    """Log one outbound call to Meta, linked to the tool call that caused it.

    Yields a small dict the caller may stamp ``row_count``, ``status`` and
    ``rate_limit_pct`` into -- a paged read only knows how much it returned
    once it has finished iterating, and the rate-limit headroom only exists
    once a response has come back.
    """
    source = _source_default()
    started = time.monotonic()
    slot: dict[str, Any] = {}
    row_id: int | None = None
    try:
        cursor = connect().execute(
            "INSERT INTO api_calls"
            "(tool_call_id, session_id, ts, source, service, method, account_id, path, "
            " request_json) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                _current_tool_call.get(),
                session_id(source),
                _now(),
                source,
                service,
                method,
                account_id,
                path,
                _dumps(request),
            ),
        )
        row_id = int(cursor.lastrowid)
    except Exception:
        row_id = None

    try:
        yield slot
    except GeneratorExit:
        # The caller stopped iterating early (a `break` over a paged read, or
        # an abandoned generator). The request itself was fine, so this is a
        # clean close, not a failure -- recording it as an error would make
        # every partially-consumed read look like a broken API call.
        _finish_api_call(row_id, started, ok=True, error=None, slot=slot)
        raise
    except BaseException as exc:
        _finish_api_call(
            row_id, started, ok=False, error=f"{type(exc).__name__}: {exc}", slot=slot
        )
        raise
    else:
        _finish_api_call(row_id, started, ok=True, error=None, slot=slot)


def _finish_api_call(
    row_id: int | None,
    started: float,
    *,
    ok: bool,
    error: str | None,
    slot: dict[str, Any] | None = None,
) -> None:
    if row_id is None:
        return
    values = slot or {}
    rows = values.get("row_count")
    status = values.get("status")
    pct = values.get("rate_limit_pct")
    try:
        connect().execute(
            "UPDATE api_calls SET ok=?, error=?, row_count=?, http_status=?, "
            "rate_limit_pct=?, duration_ms=?, finished_at=? WHERE id=?",
            (
                1 if ok else 0,
                error,
                int(rows) if isinstance(rows, int) else None,
                int(status) if isinstance(status, int) else None,
                float(pct) if isinstance(pct, (int, float)) else None,
                int((time.monotonic() - started) * 1000),
                _now(),
                row_id,
            ),
        )
    except Exception:
        pass


#: Tools that could not be instrumented, by name. Should always be empty; a
#: test asserts it, because the fallback below is silent by design and a silent
#: gap in the log is exactly the thing worth failing a build over.
UNINSTRUMENTED: list[str] = []


class InstrumentedServer:
    """An `MCPServer` whose `@tool()` decorator also logs the call.

    Wrapping the registrar rather than every tool body: one place to change,
    nothing for a new tool to forget, and no decorator to leave off by
    accident. Every other attribute passes through to the real server.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        register_tool = self._inner.tool(*args, **kwargs)

        def decorate(fn: Any) -> Any:
            return register_tool(instrument_tool(fn))

        return decorate


def instrument_tool(fn: Any) -> Any:
    """Wrap one tool function so each invocation writes a `tool_calls` row.

    The annotations are RESOLVED here rather than passed through, and that is
    not a detail. The tool modules use `from __future__ import annotations`, so
    every annotation is a string that pydantic resolves against the function's
    `__globals__` -- and a wrapper defined in this module carries THIS module's
    globals, where the tool modules' return types do not exist. A pass-through
    wrapper therefore registers fine and then fails to build an output schema,
    which the SDK reports as a warning and carries on from.
    """
    try:
        hints = typing.get_type_hints(fn, include_extras=True)
        signature = inspect.signature(fn)
        resolved = signature.replace(
            parameters=[
                parameter.replace(annotation=hints.get(parameter.name, parameter.annotation))
                for parameter in signature.parameters.values()
            ],
            return_annotation=hints.get("return", signature.return_annotation),
        )
    except Exception:
        # Better an uninstrumented tool than one whose schema we degraded.
        UNINSTRUMENTED.append(getattr(fn, "__name__", repr(fn)))
        return fn

    def bind(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        try:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            return dict(bound.arguments)
        except TypeError:
            return {"args": list(args), "kwargs": kwargs}

    def finish(wrapper: Any) -> Any:
        wrapper.__signature__ = resolved
        wrapper.__annotations__ = hints
        return wrapper

    if asyncio.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with record_tool_call(fn.__name__, bind(args, kwargs)):
                return await fn(*args, **kwargs)

        return finish(async_wrapper)

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with record_tool_call(fn.__name__, bind(args, kwargs)):
            return fn(*args, **kwargs)

    return finish(wrapper)


# --- the change ledger: raises on failure, by design -----------------------


def new_entry_id() -> str:
    import secrets

    return secrets.token_hex(6)


def insert_change(
    *,
    entry_id: str,
    tool: str,
    account_id: str,
    entity_type: str,
    entity_id: str,
    entity_name: str,
    field_name: str,
    before: str,
    after: str,
    projected_delta: str,
) -> None:
    """Record what we are ABOUT to change. Call before the mutation is sent."""
    connect().execute(
        "INSERT INTO changes"
        "(entry_id, tool_call_id, at, pid, tool, account_id, entity_type, entity_id, "
        " entity_name, field, before, after, projected_daily_spend_delta) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            entry_id,
            _current_tool_call.get(),
            _now(),
            os.getpid(),
            tool,
            account_id,
            entity_type,
            entity_id,
            entity_name,
            field_name,
            before,
            after,
            projected_delta,
        ),
    )


def complete_change(
    *,
    entry_id: str,
    ok: bool,
    resource_name: str | None = None,
    error: str | None = None,
    reverts: str | None = None,
) -> None:
    """Record how it went. A plain UPDATE, never an upsert -- see the docstring."""
    connect().execute(
        "UPDATE changes SET ok=?, outcome_at=?, resource_name=?, error=?, reverts=? "
        "WHERE entry_id=?",
        (1 if ok else 0, _now(), resource_name, error, reverts, entry_id),
    )


def _row_to_entry(row: sqlite3.Row) -> dict[str, Any]:
    """One `changes` row in the shape the ledger hands back.

    Callers read `entry["applied"]`, `entry["field"]` and `entry["before"]`
    straight off this dict, so the key set is a contract. `applied` is False
    (not None) when the outcome never arrived, because that is what
    `revert_change` checks.
    """
    return {
        "entry_id": row["entry_id"],
        "at": row["at"],
        "pid": row["pid"],
        "tool": row["tool"],
        "account_id": row["account_id"],
        "entity_type": row["entity_type"],
        "entity_id": row["entity_id"],
        "entity_name": row["entity_name"],
        "field": row["field"],
        "before": row["before"],
        "after": row["after"],
        "projected_daily_spend_delta": row["projected_daily_spend_delta"],
        "applied": bool(row["ok"]),
        "resource_name": row["resource_name"],
        "error": row["error"],
        "reverts": row["reverts"],
    }


def change_entries() -> list[dict[str, Any]]:
    """Every change, OLDEST FIRST.

    The order is load-bearing: `list_my_changes` slices `entries()[-limit:]` to
    get the most recent, which silently returns the oldest if this is reversed.
    """
    return [
        _row_to_entry(row)
        for row in connect().execute("SELECT * FROM changes ORDER BY at, rowid")
    ]


def find_change(entry_id: str) -> dict[str, Any] | None:
    row = connect().execute("SELECT * FROM changes WHERE entry_id=?", (entry_id,)).fetchone()
    return None if row is None else _row_to_entry(row)


def reverted_entry_ids() -> set[str]:
    """Entries some later change already reverted.

    The `reverts` marker is counted regardless of whether that revert
    succeeded. A revert that failed still blocks a second attempt, which is the
    safe direction -- the first may yet have landed at Meta's end, and the
    write path does not retry precisely because it cannot tell.
    """
    rows = connect().execute(
        "SELECT DISTINCT reverts FROM changes WHERE reverts IS NOT NULL"
    )
    return {row[0] for row in rows}


# --- the library mutation log: raises on failure, by design ----------------


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def insert_mutation(
    *,
    correlation_id: str,
    account_id: str,
    node_id: str,
    method: str,
    edge: str | None,
    payload: dict[str, Any],
) -> None:
    """Record an intent to write, before the request is sent."""
    connect().execute(
        "INSERT INTO mutations"
        "(correlation_id, tool_call_id, at, host, pid, account_id, node_id, method, "
        " edge, field_count, payload_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            correlation_id,
            _current_tool_call.get(),
            _now(),
            socket.gethostname(),
            os.getpid(),
            account_id,
            node_id,
            method,
            edge,
            len(payload or {}),
            _dumps(payload),
        ),
    )


def complete_mutation(
    correlation_id: str,
    *,
    ok: bool,
    response: Any = None,
    error: str | None = None,
) -> None:
    connect().execute(
        "UPDATE mutations SET ok=?, outcome_at=?, response_json=?, error=? "
        "WHERE correlation_id=?",
        (1 if ok else 0, _now(), _dumps(response), error, correlation_id),
    )


def mutation_rows(limit: int = 100) -> list[dict[str, Any]]:
    """Recent mutations, newest first. For inspection, not for revert."""
    rows = connect().execute(
        "SELECT * FROM mutations ORDER BY at DESC, rowid DESC LIMIT ?", (limit,)
    )
    out = []
    for row in rows:
        entry = dict(row)
        entry["payload"] = _loads(entry.pop("payload_json", None))
        entry["response"] = _loads(entry.pop("response_json", None))
        out.append(entry)
    return out
