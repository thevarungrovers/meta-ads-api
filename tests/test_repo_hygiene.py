"""The repo is destined to be public. Nothing real may be committed.

A test that NAMED the company or the account would reintroduce exactly what it
guards, so these check the SHAPE instead: a hostname in a string literal has to
be a reserved example domain or a Meta endpoint, and an id-shaped literal has to
be an obvious placeholder.

The business-name half has no shape to match and stays a manual
``git grep -i '<company>' -- $(git ls-files)`` before publishing.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def tracked_files() -> list[Path]:
    """Only what git actually tracks. An untracked scratch file is not shipped."""
    try:
        out = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):  # pragma: no cover
        pytest.skip("not a git checkout")
    return [REPO_ROOT / line for line in out.splitlines() if line.strip()]


def tracked_python_files() -> list[Path]:
    return [p for p in tracked_files() if p.suffix == ".py" and p.exists()]


# --- secrets ----------------------------------------------------------------


def test_no_env_file_is_tracked():
    names = {p.name for p in tracked_files()}
    assert ".env" not in names
    assert not any(n.endswith(".env") for n in names if n != ".env.example")


def test_the_env_example_has_no_values():
    """It documents the keys. A filled-in one is a leaked credential."""
    example = (REPO_ROOT / ".env.example").read_text()
    for line in example.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        assert value.strip() == "", f"{key} has a value in .env.example"


def test_gitignore_covers_the_token_the_secret_and_the_logs():
    ignored = (REPO_ROOT / ".gitignore").read_text()
    for pattern in (".env", "logs/", "audit/", "output/"):
        assert pattern in ignored, pattern


def test_no_tracked_file_contains_a_long_token_shaped_literal():
    """A Meta token is a long opaque string. Nothing shaped like one belongs in
    a tracked file."""
    # Meta tokens are typically 150-250 chars of [A-Za-z0-9_-]. Anything that
    # long and that alphabet in source is worth failing over.
    suspicious = re.compile(r"\b[A-Za-z0-9_-]{100,}\b")
    offenders = []
    for path in tracked_python_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if suspicious.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    assert not offenders, f"token-shaped literals: {offenders}"


# --- real-world identifiers -------------------------------------------------

#: Hostnames that are allowed to appear. Meta's own endpoints, the reserved
#: example domains, and the documentation/console hosts the error messages and
#: the README legitimately point people at.
ALLOWED_HOSTS = {
    "graph.facebook.com",
    "www.facebook.com",
    "business.facebook.com",
    "developers.facebook.com",
    "adsmanager.facebook.com",
    "example.com",
    "example.org",
    "example.net",
    "localhost",
}

#: A closed list, because a naive hostname regex flags `campaign.name` and
#: `metrics.clicks`, and this codebase is made of dotted field paths.
REAL_TLDS = (
    ".com", ".org", ".net", ".io", ".ai", ".co", ".dev", ".app", ".ca",
    ".uk", ".fr", ".de", ".es", ".it", ".nl", ".se", ".no", ".jp", ".cn",
)

_HOSTNAME = re.compile(
    r"\b(?:https?://)?((?:[a-z0-9-]+\.)+(?:"
    + "|".join(t.lstrip(".") for t in REAL_TLDS)
    + r"))\b",
    re.IGNORECASE,
)


def string_literals(path: Path) -> list[tuple[int, str]]:
    """Every string constant in the file, docstrings included.

    Deliberately NOT a comment/string strip followed by a search. Stripping the
    strings is what hides a real credential in one; searching the literals is
    what finds it. Prose in a docstring is matched the same way as code, which
    is why the host check is a whole-hostname match rather than a substring.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover
        return []
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


@pytest.mark.parametrize(
    "path", tracked_python_files(), ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_no_tracked_file_names_a_real_world_host(path):
    offenders = []
    for lineno, text in string_literals(path):
        for match in _HOSTNAME.finditer(text):
            host = match.group(1).lower()
            if host not in ALLOWED_HOSTS:
                offenders.append(f"line {lineno}: {host}")
    assert not offenders, (
        f"{path.relative_to(REPO_ROOT)} names a non-example host: {offenders}. "
        f"Use example.com, or add a Meta endpoint to ALLOWED_HOSTS."
    )


#: Placeholder ids used throughout the tests and docs. Anything else that long
#: and that numeric is likely a real account or object.
ALLOWED_IDS = {
    "1234567890",
    "9876543210",
    "5555555555",
    "120200000000001",
    "120200000000002",
    "120200000000003",
    "404404404",
    "999999999",
}


#: A long run of digits that is NOT part of a decimal number.
#:
#: The lookarounds are load-bearing. `\b\d{9,}\b` matches the tail of
#: 0.30000000000000004 -- which appears verbatim in a docstring explaining why
#: money is parsed as Decimal rather than float -- and flags it as an account
#: id. Refusing a digit run that touches a dot on either side keeps the check
#: pointed at identifiers.
_BARE_LONG_NUMBER = re.compile(r"(?<![\d.])\d{9,}(?![\d.])")


@pytest.mark.parametrize(
    "path",
    [p for p in tracked_python_files() if "tests/" not in str(p)],
    ids=lambda p: str(p.relative_to(REPO_ROOT)),
)
def test_no_shipped_module_contains_a_real_looking_object_id(path):
    """Tests are exempt: they are full of deliberate placeholders, listed above.
    Shipped code should carry no id at all."""
    offenders = []
    for lineno, text in string_literals(path):
        for candidate in _BARE_LONG_NUMBER.findall(text):
            if candidate not in ALLOWED_IDS:
                offenders.append(f"line {lineno}: {candidate}")
    assert not offenders, f"{path.relative_to(REPO_ROOT)} carries ids: {offenders}"


# --- consistency ------------------------------------------------------------


def test_the_readme_and_the_skill_agree_on_the_tool_count():
    """Both quote it, and a stale count is how documentation stops being read."""
    from tests.test_mcp_surface import APPLY_TOOLS, PREVIEW_TOOLS, READ_TOOLS

    total = len(READ_TOOLS | PREVIEW_TOOLS | APPLY_TOOLS)
    skill = (REPO_ROOT / ".claude" / "skills" / "meta-ads" / "SKILL.md").read_text()
    readme = (REPO_ROOT / "README.md").read_text()
    assert f"{total} in total" in skill or f"{total} tools" in skill, (
        f"the skill does not quote {total} tools"
    )
    assert str(total) in readme


def test_the_requirements_are_all_pinned():
    """An unpinned upgrade can change the API surface under a working server."""
    for line in (REPO_ROOT / "requirements.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert "==" in line, f"{line} is not pinned"
