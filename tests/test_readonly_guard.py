"""The read/write separation, asserted rather than assumed.

On the Graph API the HTTP method IS the boundary: GET reads, POST creates and
updates, DELETE deletes. So "can this file change the account?" reduces to "can
this file name a write method?" -- which is a question the AST can answer.

That is the whole argument for talking to the Graph API directly instead of
through the `facebook-business` SDK. The SDK hangs ``.api_update()`` and
``.remote_delete()`` off every object it returns, so a write would be one
attribute access away from every read and there would be nothing to grep for.

Three things are checked here:

1. No module outside the allowlist names a write method, in code.
2. No module outside the allowlist calls ``.post()``, ``.delete()``,
   ``.put()`` or ``.patch()`` on anything.
3. The write package really does contain a write -- so this file cannot pass by
   guarding an empty set.

DOCSTRINGS ARE SKIPPED, NOT STRIPPED. Prose routinely says "POST" while
explaining why a module does not send one, and a scanner that stripped strings
before searching would miss a real `"POST"` used as an argument. Matching whole
string CONSTANTS in code position is the version that survives both.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Methods that can change the account.
WRITE_METHOD_NAMES = {"POST", "DELETE", "PUT", "PATCH"}

#: Files allowed to name one, and why each is allowed.
ALLOWED = {
    # Defines the two method sets and is the single transport both clients
    # share. It takes the method as an ARGUMENT -- see the test below, which
    # pins that it never defaults to a write.
    "metaads_reporting/graph.py",
    # The write package. The point of the exercise.
    "metaads_reporting/write/client.py",
    "metaads_reporting/write/plan.py",
    "metaads_reporting/write/audit.py",
    # Uploads a file to the adimages edge, so it names POST like the rest of
    # the write package.
    "metaads_reporting/write/media.py",
    # The human-driven CLI.
    "scripts/manage.py",
}

PACKAGE_DIRS = ("metaads_reporting", "metaads_mcp", "scripts")


def source_files() -> list[Path]:
    found: list[Path] = []
    for directory in PACKAGE_DIRS:
        found.extend(sorted((REPO_ROOT / directory).rglob("*.py")))
    return found


def relative(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def docstring_nodes(tree: ast.AST) -> set[int]:
    """ids() of every Constant node that is a docstring, so they can be skipped."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                found.add(id(body[0].value))
    return found


def write_method_literals(path: Path) -> list[str]:
    """Write-method strings used in CODE, not in a docstring."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = docstring_nodes(tree)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or id(node) in skip:
            continue
        if isinstance(node.value, str) and node.value.strip().upper() in WRITE_METHOD_NAMES:
            # Whole-string match only. A docstring saying "never POST" is a
            # different node and was skipped above; a sentence that happens to
            # contain the word is not a whole-string match and never was.
            hits.append(f"line {node.lineno}: {node.value!r}")
    return hits


def write_method_calls(path: Path) -> list[str]:
    """Calls to .post/.delete/.put/.patch on anything."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"post", "delete", "put", "patch"}
        ):
            hits.append(f"line {node.lineno}: .{node.func.attr}(...)")
    return hits


@pytest.mark.parametrize("path", source_files(), ids=relative)
def test_no_module_outside_the_allowlist_names_a_write_method(path):
    if relative(path) in ALLOWED:
        pytest.skip(f"{relative(path)} is allowlisted")
    hits = write_method_literals(path)
    assert not hits, (
        f"{relative(path)} names a write method in code: {hits}. "
        f"Mutations belong in metaads_reporting/write/."
    )


@pytest.mark.parametrize("path", source_files(), ids=relative)
def test_no_module_outside_the_allowlist_calls_a_write_method(path):
    if relative(path) in ALLOWED:
        pytest.skip(f"{relative(path)} is allowlisted")
    hits = write_method_calls(path)
    assert not hits, f"{relative(path)} calls a write method: {hits}"


def test_the_write_package_really_does_contain_a_write():
    """Otherwise this whole file passes by guarding nothing."""
    source = (REPO_ROOT / "metaads_reporting" / "write" / "client.py").read_text()
    assert '"POST"' in source, "the write client sends no POST; the guard is vacuous"


def test_the_transport_never_defaults_to_a_write_method():
    """`request(method, ...)` takes the method; a write default would make every
    careless call a mutation."""
    import inspect

    from metaads_reporting.graph import GraphTransport

    signature = inspect.signature(GraphTransport.request)
    default = signature.parameters["method"].default
    assert default is inspect.Parameter.empty or str(default).upper() == "GET"


def test_the_read_client_checks_every_request():
    """Both request paths on the read client call the guard."""
    import inspect

    from metaads_reporting.client import ReadOnlyMetaAdsClient

    for name in ("_get", "_paged"):
        source = inspect.getsource(getattr(ReadOnlyMetaAdsClient, name))
        assert "assert_read_method" in source, f"{name} does not call the guard"


def test_the_read_client_exposes_no_method_that_takes_a_body():
    """A write needs a form payload. Nothing here can carry one."""
    import inspect

    from metaads_reporting.client import ReadOnlyMetaAdsClient

    for name, member in inspect.getmembers(ReadOnlyMetaAdsClient, inspect.isfunction):
        parameters = inspect.signature(member).parameters
        assert "data" not in parameters, f"{name} takes a body"


def test_the_write_shim_the_read_tools_use_exposes_no_write():
    """tools_read reuses the write package's SEARCH helpers, which only read.

    The shim is what makes that safe: a helper that grew a write would fail
    loudly here rather than quietly gaining the ability to mutate.
    """
    from metaads_mcp.tools_read import _WriteShim

    # An INSTANCE, not the class: `read` is set in __init__ and would be
    # invisible to dir() on the class, which would make this pass for the
    # wrong reason.
    shim = _WriteShim(object())
    public = {n for n in dir(shim) if not n.startswith("_")}
    assert public == {"read", "resolve_account_id", "currency", "node"}, public
    for forbidden in ("update", "create", "delete", "request", "transport"):
        assert not hasattr(shim, forbidden), f"the shim exposes .{forbidden}"


def test_the_mutating_client_has_no_delete_at_all():
    """Meta's delete is not a soft state you can set back. The capability is
    absent rather than guarded."""
    from metaads_reporting.write import MutatingMetaAdsClient

    for name in ("delete", "remove", "destroy", "archive"):
        assert not hasattr(MutatingMetaAdsClient, name), f"it has .{name}()"
