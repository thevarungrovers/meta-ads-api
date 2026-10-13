"""Hard bounds on what the server will apply, and the session budget.

Two kinds of limit, and the second matters more:

* **Per-call** -- this budget is not above X, this bid is not a 10x jump. They
  catch a fat finger or a misread number.
* **Per-session** -- at most N applies, at most X total projected daily spend
  added. A per-call limit does nothing against a loop making four hundred
  individually-legal changes, which is the failure mode an agent actually has.

Values come from ``guardrails.toml`` at the repo root, falling back to the
constants below. A key that is misspelled raises at startup rather than being
ignored, because a silently-ignored ceiling is worse than no ceiling: it reads
as protection that is not there.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "guardrails.toml"

#: Written when writes are disabled; its presence is checked before every apply.
KILL_SWITCH = REPO_ROOT / "audit" / "WRITES_DISABLED"

DEFAULTS: dict[str, object] = {
    "max_daily_budget": Decimal("500.00"),
    "max_budget_change_pct": 50.0,
    "max_bid_amount": Decimal("50.00"),
    "max_bid_increase_pct": 50.0,
    "max_entities_per_call": 25,
    "max_projected_delta_per_day": Decimal("200.00"),
    "max_applies_per_session": 20,
    "max_projected_session_delta": Decimal("500.00"),
}

_DECIMAL_KEYS = {
    "max_daily_budget",
    "max_bid_amount",
    "max_projected_delta_per_day",
    "max_projected_session_delta",
}
_FLOAT_KEYS = {"max_budget_change_pct", "max_bid_increase_pct"}
_INT_KEYS = {"max_entities_per_call", "max_applies_per_session"}


class WritesDisabled(RuntimeError):
    """The kill switch is set. A deliberate refusal, so its message must survive.

    Its own type rather than a reused one, because this is the refusal an
    operator most needs to read verbatim: it names the file to delete.
    """


class GuardrailConfigError(RuntimeError):
    """guardrails.toml is malformed. Raised at startup, never swallowed."""


@dataclass(frozen=True)
class Limits:
    max_daily_budget: Decimal
    max_budget_change_pct: float
    max_bid_amount: Decimal
    max_bid_increase_pct: float
    max_entities_per_call: int
    max_projected_delta_per_day: Decimal
    max_applies_per_session: int
    max_projected_session_delta: Decimal
    allowed_account_ids: tuple[str, ...] = ()
    allowed_campaign_ids: tuple[str, ...] = ()
    allowed_currencies: tuple[str, ...] = ("CAD",)

    def describe(self) -> dict[str, object]:
        return {
            "max_daily_budget": str(self.max_daily_budget),
            "max_budget_change_pct": self.max_budget_change_pct,
            "max_bid_amount": str(self.max_bid_amount),
            "max_bid_increase_pct": self.max_bid_increase_pct,
            "max_entities_per_call": self.max_entities_per_call,
            "max_projected_delta_per_day": str(self.max_projected_delta_per_day),
            "max_applies_per_session": self.max_applies_per_session,
            "max_projected_session_delta": str(self.max_projected_session_delta),
            "allowed_account_ids": list(self.allowed_account_ids)
            or ["(only META_ADS_ACCOUNT_ID)"],
            "allowed_campaign_ids": list(self.allowed_campaign_ids) or ["(all)"],
            "allowed_currencies": list(self.allowed_currencies),
        }


def _coerce(key: str, value: object) -> object:
    try:
        if key in _DECIMAL_KEYS:
            return Decimal(str(value))
        if key in _FLOAT_KEYS:
            return float(value)
        if key in _INT_KEYS:
            return int(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise GuardrailConfigError(
            f"guardrails.toml: [limits] {key} = {value!r} is not valid."
        ) from exc
    raise GuardrailConfigError(
        f"guardrails.toml: unknown key [limits] {key!r}. "
        f"Known keys: {', '.join(sorted(DEFAULTS))}."
    )


def load_limits(path: Path | None = None) -> Limits:
    """Read guardrails.toml over the defaults."""
    path = path or CONFIG_PATH
    values = dict(DEFAULTS)
    scope: dict[str, object] = {}

    if path.is_file():
        try:
            parsed = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise GuardrailConfigError(
                f"guardrails.toml is not valid TOML: {exc}"
            ) from exc
        for key, value in (parsed.get("limits") or {}).items():
            values[key] = _coerce(key, value)
        scope = parsed.get("scope") or {}

    accounts = tuple(
        str(a).replace("act_", "") for a in scope.get("allowed_account_ids", [])
    )
    campaigns = tuple(str(c) for c in scope.get("allowed_campaign_ids", []))
    currencies = tuple(
        str(c).upper() for c in scope.get("allowed_currencies", ["CAD"])
    )

    # An env var can narrow the scope further but never widen it: a
    # machine-local override that could ADD accounts would defeat the committed
    # file.
    env_accounts = os.environ.get("META_ADS_MCP_ALLOWED_ACCOUNT_IDS", "").strip()
    if env_accounts:
        from_env = tuple(
            a.strip().replace("act_", "") for a in env_accounts.split(",") if a.strip()
        )
        accounts = tuple(a for a in from_env if not accounts or a in accounts)

    return Limits(
        **{k: values[k] for k in DEFAULTS},
        allowed_account_ids=accounts,
        allowed_campaign_ids=campaigns,
        allowed_currencies=currencies,
    )


def writes_disabled() -> str | None:
    """The kill switch: a reason string if writes are off, else None."""
    if KILL_SWITCH.is_file():
        return KILL_SWITCH.read_text().strip() or "writes disabled"
    return None


def disable_writes(reason: str = "") -> Path:
    KILL_SWITCH.parent.mkdir(parents=True, exist_ok=True)
    KILL_SWITCH.write_text(reason or "writes disabled")
    return KILL_SWITCH


@dataclass
class Check:
    """One named bound, and whether this change clears it."""

    name: str
    ok: bool
    detail: str

    def render(self) -> str:
        return f"{'OK  ' if self.ok else 'FAIL'} {self.name}: {self.detail}"


class SessionCounters:
    """What this server process has already spent of its change budget."""

    def __init__(self, limits: Limits) -> None:
        self._limits = limits
        self.applies = 0
        self.projected_delta = Decimal("0")

    @property
    def limits(self) -> Limits:
        return self._limits

    def would_exceed(self, projected_delta: Decimal) -> str | None:
        """Why this apply would break the session budget, or None."""
        if self.applies + 1 > self._limits.max_applies_per_session:
            return (
                f"session apply limit reached: {self.applies} of "
                f"{self._limits.max_applies_per_session} already used. Restart "
                "the server deliberately if more are genuinely needed."
            )
        total = self.projected_delta + max(projected_delta, Decimal("0"))
        if total > self._limits.max_projected_session_delta:
            return (
                f"this change would take the session's projected added daily "
                f"spend to {total}, over the "
                f"{self._limits.max_projected_session_delta} ceiling."
            )
        return None

    def record(self, projected_delta: Decimal) -> None:
        self.applies += 1
        self.projected_delta += max(projected_delta, Decimal("0"))

    def snapshot(self) -> dict[str, object]:
        return {
            "applies_used": self.applies,
            "applies_remaining": max(
                0, self._limits.max_applies_per_session - self.applies
            ),
            "projected_session_delta": str(self.projected_delta),
            "projected_session_delta_remaining": str(
                max(
                    Decimal("0"),
                    self._limits.max_projected_session_delta - self.projected_delta,
                )
            ),
        }


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_account_in_scope(limits: Limits, account_id: str, default: str) -> Check:
    allowed = limits.allowed_account_ids or (default,)
    ok = account_id in allowed
    return Check(
        "account_in_scope",
        ok,
        f"{account_id} is {'in' if ok else 'NOT in'} scope ({', '.join(allowed)})",
    )


def check_campaign_in_scope(limits: Limits, campaign_id: str | None) -> Check:
    if not limits.allowed_campaign_ids:
        return Check("campaign_in_scope", True, "no campaign restriction configured")
    ok = campaign_id is not None and str(campaign_id) in limits.allowed_campaign_ids
    return Check(
        "campaign_in_scope",
        ok,
        f"{campaign_id} is {'in' if ok else 'NOT in'} the allowed list",
    )


def check_currency(limits: Limits, currency: str | None) -> Check:
    ok = bool(currency) and currency.upper() in limits.allowed_currencies
    return Check(
        "currency_allowed",
        ok,
        f"{currency or '(unknown)'} against {', '.join(limits.allowed_currencies)}",
    )


def check_ceiling(name: str, value: Decimal, ceiling: Decimal, unit: str = "") -> Check:
    ok = value <= ceiling
    suffix = f" {unit}" if unit else ""
    return Check(name, ok, f"{value}{suffix} against ceiling {ceiling}{suffix}")


def check_pct_increase(
    name: str, before: Decimal | None, after: Decimal, ceiling_pct: float
) -> Check:
    if before is None or before == 0:
        return Check(name, True, "no previous value to compare against")
    pct = float((after - before) / before * 100)
    ok = pct <= ceiling_pct
    return Check(name, ok, f"{pct:+.1f}% against ceiling +{ceiling_pct:.0f}%")


def check_count(limits: Limits, count: int) -> Check:
    ok = count <= limits.max_entities_per_call
    return Check(
        "entities_per_call", ok, f"{count} against {limits.max_entities_per_call}"
    )


def check_projected_delta(limits: Limits, projected: Decimal) -> Check:
    ok = projected <= limits.max_projected_delta_per_day
    return Check(
        "projected_delta_per_day",
        ok,
        f"{projected} against {limits.max_projected_delta_per_day}",
    )


def check_writes_enabled() -> Check:
    reason = writes_disabled()
    return Check("writes_enabled", reason is None, reason or "kill switch is not set")


READ_ONLY_ENV = "META_ADS_MCP_READ_ONLY"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def read_only_mode() -> bool:
    """True when this process must publish no `apply_*` tool at all.

    A fourth layer, and the only one that works no matter which client is
    driving. Claude Code can allowlist tools by name, so there "never approve a
    write" is something the client can express. Claude Desktop, Cursor and Codex
    have no equivalent -- their MCP config says which servers to run, not which
    tools may be called -- so for them the only honest way to offer a read-only
    install is for the server to publish no write tools in the first place. An
    unregistered tool is unreachable, exactly as an unwritten one is.

    Read from the PROCESS environment rather than .env, and that is the point:
    every client's MCP config has its own `env` block, so one checkout can be
    read-only for one client and writable for another without two copies of the
    repo or two sets of credentials.

    Not the same thing as the kill switch. WRITES_DISABLED is a file, it stops
    an already-published apply mid-session, and it is account-wide. This is set
    once at startup, per client, and the tools simply never appear.
    """
    return os.environ.get(READ_ONLY_ENV, "").strip().lower() in _TRUTHY


def require_writes_enabled() -> None:
    """Raise if the kill switch is set. Call at APPLY time, never cached.

    `check_writes_enabled()` above returns a Check for the preview's list, which
    is the right shape there -- it shows up alongside the other bounds in the
    text a human reads. It is the wrong shape at apply time, because a preview
    is good for ten minutes and the switch's whole purpose is that somebody can
    throw it from another terminal and have the very next apply stop. A switch
    consulted only when the token was minted would let every outstanding preview
    through after it was thrown, which is precisely the moment it is needed.
    """
    reason = writes_disabled()
    if reason:
        raise WritesDisabled(
            f"writes are disabled: {reason}. This is the kill switch, and it was "
            f"set after this preview was taken. Remove {KILL_SWITCH} to re-enable "
            f"writes; do not work around it."
        )


def render_checks(checks: list[Check]) -> list[str]:
    return [c.render() for c in checks]


def any_failed(checks: list[Check]) -> bool:
    return any(not c.ok for c in checks)
