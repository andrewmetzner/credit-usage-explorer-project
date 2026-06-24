"""Typed alert-rule model.

An ``AlertRule`` replaces the loose ``{"metric": ..., "threshold": ...}`` dicts
that used to be passed around. It validates/normalizes input in one place
(``from_dict``), serializes cleanly for JSON storage (``to_dict``), and — being
a ``DictMixin`` — stays drop-in compatible with the template's ``r.name`` access
and ``evaluate_rules``' ``rule.get(...)`` reads.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass

from .typed import DictMixin

# Metrics the outlier/alert engine knows how to evaluate.
VALID_METRICS = ("per_record", "per_user_day", "per_user_window")


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
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:8]),
            name=(str(data.get("name") or "").strip() or "Alert rule"),
            metric=metric,
            threshold=_num(data.get("threshold"), 1000.0, float),
            window_days=_num(data.get("window_days"), 7, int),
            usage_type=str(data.get("usage_type") or "").strip(),
            model=str(data.get("model") or "").strip(),
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
