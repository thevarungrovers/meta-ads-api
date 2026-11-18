"""Who made this? Names the agent and the CLI create carry their maker's prefix.

    <OWNER>-<agent>-<base name>          AB-max-spring_promo_week_12

The prefix goes FIRST, before any house-style name, so ``find_*`` can filter
by it and a human reading Ads Manager a month later can tell an entity this
server made from one made by hand. The base keeps whatever separators the
agent chose; only the two ``-`` after the initials and the agent name are the
delimiter, and neither part may contain one, so the prefix always parses.

THE IDENTITY COMES FROM THE REPO'S ``.env`` ONLY (``config.env`` too, when
present; ``.env`` wins), never from a tool argument and never from the process
environment. A value in the process env is IGNORED -- one identity per
checkout -- and the server warns at startup when one disagrees with ``.env``.

EMPTY IS NOT AN ERROR. Each part is used only when it is set:

    owner and agent    AB-max-<base>
    owner only         AB-<base>
    agent only         max-<base>
    neither            <base>, exactly as given

A part that is set but MALFORMED is refused at preview, naming the ``.env`` key:
a bad prefix would make every later parse ambiguous.

The CLI is a human, not the agent: ``AB-cli-<base>`` when the initials are set,
the plain base when they are not (with no owner there is nothing to mark).

Names are REFUSED when too long, never truncated: a shortened name is not the
one a human approved. Meta keeps names to the house limit of 400 characters
(the same limit renames have always used).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OWNER_KEY = "META_ADS_MCP_OWNER_INITIALS"
AGENT_KEY = "META_ADS_MCP_AGENT_NAME"

OWNER_RE = re.compile(r"^[A-Z]{2,4}$")
AGENT_RE = re.compile(r"^[a-z][a-z0-9_]{1,23}$")
#: ``cli`` marks a human using scripts/manage.py; ``manual`` is kept back so no
#: agent can claim to be a person in Ads Manager.
RESERVED_AGENTS = frozenset({"cli", "manual"})

#: A full prefix as it appears at the start of a name.
_FULL = re.compile(r"^([A-Z]{2,4})-([a-z][a-z0-9_]{1,23})-(.*)$", re.S)

#: Every entity type a name is stamped on, and its limit.
NAME_LIMIT = 400
NAME_LIMITS: dict[str, int] = {
    kind: NAME_LIMIT
    for kind in ("campaign", "ad set", "ad", "creative", "audience", "ad study", "label")
}

#: Characters no name may carry.
_CONTROL = {"\x00": "NUL", "\r": "CR", "\n": "LF", "\t": "TAB"}

NO_IDENTITY_RULE = "no identity configured; name used as given"


class NameRefused(ValueError):
    """A name this server will not send. The message says what to change."""


@dataclass(frozen=True)
class Parsed:
    owner: str
    agent: str
    base: str

    @property
    def prefix(self) -> str:
        return f"{self.owner}-{self.agent}-"


def parse(name: str | None) -> Parsed | None:
    """The FULL ``OWNER-agent-`` prefix a name carries, or None.

    >>> parse("AB-max-spring_promo")
    Parsed(owner='AB', agent='max', base='spring_promo')
    >>> parse("AB-cli-QC 25-54").agent
    'cli'
    >>> parse("Spring - Prospecting") is None
    True
    """
    match = _FULL.match(name or "")
    if not match:
        return None
    return Parsed(match.group(1), match.group(2), match.group(3))


def origin_of(name: str | None) -> dict[str, Any]:
    """``origin`` (agent | cli | manual) and the parsed parts, from the NAME only.

    The name is a convention; the ledger is the proof.

    >>> origin_of("AB-cli-x")["origin"]
    'cli'
    >>> origin_of("launch")["origin"]
    'manual'
    """
    parsed = parse(name)
    if parsed is None:
        return {"origin": "manual", "owner": None, "agent": None, "base": None}
    return {
        "origin": "cli" if parsed.agent == "cli" else "agent",
        "owner": parsed.owner,
        "agent": parsed.agent,
        "base": parsed.base,
    }


# ---------------------------------------------------------------------------
# The identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    """The owner initials and agent name a checkout stamps, either may be empty."""

    owner: str = ""
    agent: str = ""
    source: str = ".env"
    #: Set when a value is present but malformed: naming the .env key to fix.
    problem: str = ""
    warnings: tuple[str, ...] = field(default=(), compare=False)

    @property
    def prefix(self) -> str:
        """``AB-max-``, ``AB-``, ``max-`` or ``""``."""
        if self.problem:
            return ""
        return "".join(f"{part}-" for part in (self.owner, self.agent) if part)

    @property
    def configured(self) -> bool:
        return bool(self.prefix)

    def owns(self, name: str | None) -> bool:
        """Whether ``name`` carries THIS identity's prefix.

        A full ``OWNER-agent-`` prefix must match both parts exactly: an
        owner-only identity ``AB-`` does not own ``AB-max-x``, which is
        another agent's.
        """
        if not self.configured or not name:
            return False
        parsed = parse(name)
        if parsed is not None:
            return (parsed.owner, parsed.agent) == (self.owner, self.agent)
        return name.startswith(self.prefix)

    def describe(self) -> dict[str, Any]:
        return {
            "owner_initials": self.owner or None,
            "agent_name": self.agent or None,
            "prefix": self.prefix,
            "source": self.source,
            "parts_set": [k for k, v in (("owner_initials", self.owner),
                                         ("agent_name", self.agent)) if v],
            "problem": self.problem or None,
            "env_keys": [OWNER_KEY, AGENT_KEY],
        }

    @classmethod
    def from_values(cls, owner: str | None, agent: str | None, *, source: str = ".env",
                    warnings: tuple[str, ...] = ()) -> "Identity":
        """Validate both parts; a malformed one sets ``problem``, never raises."""
        owner = (owner or "").strip()
        agent = (agent or "").strip()
        problems = []
        if owner and not OWNER_RE.match(owner):
            problems.append(
                f"{OWNER_KEY} in .env is {owner!r}: it must be 2-4 uppercase letters "
                "(for example AB), or empty"
            )
        if agent:
            if not AGENT_RE.match(agent):
                problems.append(
                    f"{AGENT_KEY} in .env is {agent!r}: it must be a lowercase slug of 2-24 "
                    "characters, a letter first, then letters, digits or _ (no -), for "
                    "example max, or empty"
                )
            elif agent in RESERVED_AGENTS:
                problems.append(
                    f"{AGENT_KEY} in .env is {agent!r}, which is reserved "
                    f"({', '.join(sorted(RESERVED_AGENTS))}); pick another name"
                )
        return cls(owner=owner, agent=agent, source=source, problem="; ".join(problems),
                    warnings=warnings)

    @classmethod
    def from_env(cls, paths: list[Path] | None = None) -> "Identity":
        """Read the two keys from the repo's ``.env`` (and ``config.env``).

        ``.env`` wins over ``config.env``. The PROCESS environment is ignored
        on purpose -- one identity per checkout -- but a value there that
        disagrees with the file is reported in ``warnings``.
        """
        from dotenv import dotenv_values

        from .config import CONFIG_ENV_PATH, ENV_PATH

        files = paths if paths is not None else [CONFIG_ENV_PATH, ENV_PATH]
        values: dict[str, str] = {}
        sources = []
        for path in files:  # later files win: .env last
            if not Path(path).is_file():
                continue
            found = dotenv_values(path)
            for key in (OWNER_KEY, AGENT_KEY):
                if key in found and found[key] is not None:
                    values[key] = found[key] or ""
            if any(k in found for k in (OWNER_KEY, AGENT_KEY)):
                sources.append(Path(path).name)
        warnings = []
        for key in (OWNER_KEY, AGENT_KEY):
            in_process = (os.environ.get(key) or "").strip()
            if in_process and in_process != (values.get(key) or "").strip():
                warnings.append(
                    f"{key} is set in the process environment but differs from .env; "
                    "the process value is IGNORED (the identity comes from .env only)"
                )
        return cls.from_values(values.get(OWNER_KEY), values.get(AGENT_KEY),
                               source=" + ".join(sources) or ".env (keys absent)",
                               warnings=tuple(warnings))

    def cli(self) -> "Identity":
        """A human using the CLI: ``OWNER-cli-``, or nothing without initials."""
        if self.problem and self.owner and not OWNER_RE.match(self.owner):
            return Identity(owner=self.owner, agent="cli", source=self.source, problem=self.problem)
        if not self.owner:
            return Identity(source=self.source)
        return Identity(owner=self.owner, agent="cli", source=self.source)


# ---------------------------------------------------------------------------
# Stamping
# ---------------------------------------------------------------------------


@dataclass
class Stamped:
    """The name that will be SENT, and how it was arrived at."""

    final: str
    base: str
    requested: str
    prefix: str
    rule: str
    entity_type: str
    notes: list[str] = field(default_factory=list)

    @property
    def max_length(self) -> int:
        return NAME_LIMITS.get(self.entity_type, NAME_LIMIT)

    def block(self) -> dict[str, Any]:
        """The ``naming`` block a preview shows."""
        return {
            "requested": self.requested,
            "final": self.final,
            "prefix": self.prefix,
            "base": self.base,
            "length": len(self.final),
            "max_length": self.max_length,
            "rule": self.rule,
        }

    def warning(self) -> str | None:
        if self.final != self.requested.strip():
            why = {"stamped": "prefix added", "copy_restamped": "copy named with the prefix",
                   "restoring_prefix": "restoring the prefix"}.get(self.rule, self.rule)
            return f'the name will be "{self.final}" ({why})'
        return None


def _base_of(identity: Identity, name: str, entity_type: str) -> tuple[str, str]:
    """(the base to stamp, the rule) for a requested name. Refuses impersonation."""
    parsed = parse(name)
    if parsed is not None:
        if identity.configured and (parsed.owner, parsed.agent) == (identity.owner, identity.agent):
            return parsed.base.strip(), "already_ours"
        raise NameRefused(
            f"the {entity_type} name starts with {parsed.prefix}, which is "
            f"{'the CLI marker' if parsed.agent == 'cli' else 'another agent'}'s prefix; "
            "pass the base name only (reorder it if the base itself looks like a prefix)"
        )
    if identity.configured and name.startswith(identity.prefix):
        return name[len(identity.prefix):].strip(), "already_ours"
    return name, "stamped" if identity.configured else NO_IDENTITY_RULE


def _check_base(base: str, identity: Identity, entity_type: str) -> None:
    if not base:
        raise NameRefused(f"the {entity_type} name is empty")
    bad = sorted({label for char, label in _CONTROL.items() if char in base})
    if bad:
        raise NameRefused(f"the {entity_type} name contains control characters ({', '.join(bad)})")
    limit = NAME_LIMITS.get(entity_type, NAME_LIMIT)
    room = limit - len(identity.prefix)
    if len(base) > room:
        raise NameRefused(
            f"the {entity_type} name is too long: {len(identity.prefix) + len(base)} characters "
            f"against {limit}"
            + (f"; with the prefix {identity.prefix!r} the base may be at most {room} "
               f"characters, and it is {len(base)}" if identity.prefix else "")
            + ". Names are refused, never truncated: shorten it"
        )


def _require_valid(identity: Identity) -> None:
    if identity.problem:
        raise NameRefused(identity.problem)


def stamp(identity: Identity, requested: str, *, entity_type: str) -> Stamped:
    """The final name for something being CREATED with ``requested``.

    >>> stamp(Identity("AB", "max"), "spring", entity_type="campaign").final
    'AB-max-spring'
    >>> stamp(Identity("AB", "max"), "AB-max-spring", entity_type="campaign").final
    'AB-max-spring'
    >>> stamp(Identity(), "spring", entity_type="campaign").final
    'spring'
    """
    _require_valid(identity)
    text = (requested or "").strip()
    if not text:
        raise NameRefused(f"the {entity_type} needs a name")
    base, rule = _base_of(identity, text, entity_type)
    _check_base(base, identity, entity_type)
    return Stamped(final=f"{identity.prefix}{base}", base=base, requested=requested or "",
                   prefix=identity.prefix, rule=rule, entity_type=entity_type)


def derived(name: str, *, entity_type: str) -> Stamped:
    """A name built from one already decided (an ad's creative: "<ad name>
    creative"): only the length and characters are checked, the prefix is
    whatever the name it came from carries."""
    _check_base(name, Identity(), entity_type)
    return Stamped(final=name, base=name, requested=name, prefix="", rule="derived",
                   entity_type=entity_type)


def strip_prefix(identity: Identity, name: str) -> str:
    """``name`` without any full prefix (ours, another agent's, the CLI's), and
    without our own partial one."""
    parsed = parse(name)
    if parsed is not None:
        return parsed.base
    if identity.configured and name.startswith(identity.prefix):
        return name[len(identity.prefix):]
    return name


def restamp_for_copy(identity: Identity, source_name: str, *, entity_type: str,
                     suffix: str = "") -> Stamped:
    """The name of a COPY the agent makes of ``source_name``.

    A copy made by the agent is created by the agent: OUR prefix on the
    source's base, any prefix stripped. With no identity configured, the
    source name plus ``suffix`` -- the naming used before this existed.

    >>> restamp_for_copy(Identity("AB", "max"), "CD-bot-launch", entity_type="ad",
    ...                  suffix=" (copy)").final
    'AB-max-launch (copy)'
    """
    _require_valid(identity)
    source = (source_name or "").strip()
    if not identity.configured:
        final = f"{source}{suffix}"
        _check_base(final, identity, entity_type)
        return Stamped(final=final, base=final, requested=final, prefix="",
                       rule=NO_IDENTITY_RULE, entity_type=entity_type)
    base = f"{strip_prefix(identity, source).strip()}{suffix}".strip()
    _check_base(base, identity, entity_type)
    return Stamped(final=f"{identity.prefix}{base}", base=base, requested=f"{source}{suffix}",
                   prefix=identity.prefix, rule="copy_restamped", entity_type=entity_type)


def rename(identity: Identity, current: str, requested: str, *, entity_type: str,
           ledger_owned: bool) -> Stamped:
    """The new name for renaming an entity now called ``current``.

    Only the agent's OWN entities may be renamed: the live name carries this
    identity's prefix, or the ledger shows this server created it. A manual
    entity and another agent's are refused -- there is no override. The
    prefix is kept (or restored, on a ledger-owned entity whose prefix was
    removed in Ads Manager); only the base can change.
    """
    _require_valid(identity)
    ours = identity.owns(current) or ledger_owned
    if not ours:
        parsed = parse(current)
        if parsed is not None:
            raise NameRefused(
                f"this {entity_type} is named with {parsed.prefix}, "
                f"{'a CLI creation' if parsed.agent == 'cli' else 'another agent'}'s prefix; "
                "the agent may rename only its own entities"
            )
        raise NameRefused(
            f"this {entity_type} was not made by this agent (its name has no "
            f"{identity.prefix or 'agent'} prefix and the ledger shows no creation of it); "
            "the agent may rename only its own entities"
        )
    stamped = stamp(identity, requested, entity_type=entity_type)
    if identity.configured and not identity.owns(current):
        stamped.rule = "restoring_prefix"
        stamped.notes.append("restoring the prefix: it was removed outside this server")
    elif identity.configured:
        stamped.rule = "kept_prefix"
    return stamped
