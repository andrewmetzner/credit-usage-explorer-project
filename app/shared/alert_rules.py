"""Typed alert-rule model.

An ``AlertRule`` replaces the loose ``{"metric": ..., "threshold": ...}`` dicts
that used to be passed around. It validates/normalizes input in one place
(``from_dict``), serializes cleanly for JSON storage (``to_dict``), and — being
a ``DictMixin`` — stays drop-in compatible with the template's ``r.name`` access
and ``evaluate_rules``' ``rule.get(...)`` reads.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass

from .typed import DictMixin

# Metrics the outlier/alert engine knows how to evaluate.
#   per_record          — one record (prompt) over the threshold
#   per_user_day        — one user's single-day total over the threshold
#   per_user_window     — one user's window total over the threshold
#   total_window        — org-wide spend over the window exceeds the threshold
#   total_day           — org-wide spend on any single day exceeds the threshold
#   active_users_window — distinct active users in the window exceed the threshold
# The first three drill down to the advanced outlier search (which shares these
# names); the org-wide three deep-link to the Summary spend overview instead.
VALID_METRICS = (
    "per_record",
    "per_user_day",
    "per_user_window",
    "total_window",
    "total_day",
    "active_users_window",
)


def _num(value, default, cast):
    """Coerce form/JSON input to a number, falling back to ``default``.

    Tolerates None and blank strings (treated as "unset" -> default) while
    preserving an explicit 0.
    """
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return default
        return cast(float(value))
    except (TypeError, ValueError):
        return default


@dataclass
class AlertRule(DictMixin):
    """One user-defined alert/outlier trigger."""

    id: str
    name: str = "Alert rule"
    metric: str = "per_user_window"
    threshold: float = 1000.0
    window_days: int = 7
    usage_type: str = ""
    model: str = ""
    enabled: bool = True

    @classmethod
    def from_dict(cls, data) -> "AlertRule":
        """Build a validated rule from a dict (or another mapping/AlertRule).

        Always returns a fresh instance, so loading defaults or copying a rule
        never shares mutable state.
        """
        data = data or {}
        metric = str(data.get("metric") or "per_user_window")
        if metric not in VALID_METRICS:
            metric = "per_user_window"
        name = (str(data.get("name") or "").strip() or "Alert rule")
        threshold = _num(data.get("threshold"), 1000.0, float)
        window_days = _num(data.get("window_days"), 7, int)
        usage_type = str(data.get("usage_type") or "").strip()
        model = str(data.get("model") or "").strip()

        # The id drives the notification bell's read/unread state, so it MUST be
        # stable across loads and process restarts. When a rule has no saved id,
        # derive one deterministically from its defining fields instead of a
        # random uuid — otherwise every reload mints a fresh id and previously
        # read alerts re-appear as unread.
        rid = data.get("id")
        if not rid:
            basis = "|".join([name, metric, str(threshold), str(window_days), usage_type, model])
            rid = hashlib.md5(basis.encode("utf-8")).hexdigest()[:8]

        return cls(
            id=str(rid),
            name=name,
            metric=metric,
            threshold=threshold,
            window_days=window_days,
            usage_type=usage_type,
            model=model,
            enabled=bool(data.get("enabled", True)),
        )

    def to_dict(self) -> dict:
        return asdict(self)


# Starter rule — mirrors the previous built-in "heavy users this week".
DEFAULT_ALERT_RULES: list[AlertRule] = [
    AlertRule(
        id="heavy-users",
        name="Heavy users this week",
        metric="per_user_window",
        threshold=1000,
        window_days=7,
    ),
]
