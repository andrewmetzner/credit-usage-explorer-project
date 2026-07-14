from __future__ import annotations

import calendar
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd


ACTION_PRIORITY = {
    "CONSIDER_MOVE_UP_TIER": 1,
    "CONSIDER_MOVE_DOWN_TIER": 2,
    "MONITOR_RECENT_SPIKE": 3,
    "MONITOR_MORE_HISTORY_NEEDED": 4,
    "NO_CHANGE": 5,
}


@dataclass
class OptimizationResult:
    source_label: str
    user_week_history: pd.DataFrame
    user_summary: pd.DataFrame
    recommendations: pd.DataFrame
    recommendation_summary: pd.DataFrame
    tier_summary: pd.DataFrame
    latest_summary: dict[str, Any]


# Divisor used to turn a monthly cap into a weekly pace. Default 4.0 matches how
# the monthly caps were derived (4x the old weekly caps). Set weeks_per_month in
# the tier config to a number (e.g. calendar-accurate ~4.345) or to the string
# "actual" to divide each month's cap by the real number of weeks in that month.
DEFAULT_WEEKS_PER_MONTH = 4.0
# Representative average weeks/month, used only when 'actual' mode is asked for a
# weekly cap without a specific week to anchor the month.
CALENDAR_WEEKS_PER_MONTH = 4.345


def weeks_in_month(year: int, month: int) -> int:
    """Weeks (Mondays) that fall within the given calendar month (4 or 5).

    A week is assigned to the month of its Monday, matching how usage is bucketed
    (week_start = Monday). These counts partition the year, so a tier's weekly
    caps across a month sum exactly to its monthly cap.
    """
    cal = calendar.Calendar(firstweekday=0)  # Monday
    return sum(1 for d in cal.itermonthdates(year, month)
               if d.month == month and d.weekday() == 0)


def raw_tier_cap(cfg: dict) -> float:
    """The cap number as stored, regardless of period (period-agnostic read)."""
    if not isinstance(cfg, dict):
        return 0.0
    value = cfg.get("credit_cap", cfg.get("weekly_credit_cap", 0))
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def cap_period(tier_config: dict) -> str:
    """Global interpretation of stored cap numbers: 'weekly' or 'monthly'."""
    return str(tier_config.get("cap_period", "weekly") or "weekly").strip().lower()


def weeks_per_month_setting(tier_config: dict) -> float | str:
    """Configured monthly->weekly divisor: a positive float, or 'actual'."""
    val = tier_config.get("weeks_per_month", DEFAULT_WEEKS_PER_MONTH)
    if isinstance(val, str) and val.strip().lower() == "actual":
        return "actual"
    try:
        wpm = float(val or 0)
    except (TypeError, ValueError):
        wpm = 0.0
    return wpm or DEFAULT_WEEKS_PER_MONTH


def cap_change_date(tier_config: dict) -> pd.Timestamp | None:
    """Date the workspace switched weekly -> monthly caps, or None."""
    raw = tier_config.get("cap_period_change_date")
    if not raw:
        return None
    ts = pd.to_datetime(raw, errors="coerce")
    return None if pd.isna(ts) else ts.normalize()


def weeks_per_month(tier_config: dict, week_start: object = None) -> float:
    """Numeric divisor for this config, resolved for a given week.

    If a weekly->monthly switch date is set, weeks BEFORE it are treated as the
    old weekly regime: dividing the (monthly) cap by DEFAULT_WEEKS_PER_MONTH
    recovers the flat pre-switch weekly cap. Weeks on/after use the configured
    setting (a fixed number or the actual weeks in that month).
    """
    change = cap_change_date(tier_config)
    if change is not None and week_start is not None and not pd.isna(week_start):
        if pd.Timestamp(week_start).normalize() < change:
            return DEFAULT_WEEKS_PER_MONTH
    setting = weeks_per_month_setting(tier_config)
    if setting != "actual":
        return float(setting)
    if week_start is None or pd.isna(week_start):
        return CALENDAR_WEEKS_PER_MONTH
    ts = pd.Timestamp(week_start)
    return float(weeks_in_month(ts.year, ts.month))


def tier_caps(tier_config: dict, week_start: object = None) -> dict[str, float]:
    """Effective *weekly* caps for the engine.

    Stored caps may be weekly or monthly (global `cap_period`, overridable per
    tier via `cap_period` on the tier). Monthly caps are divided down to a weekly
    pace so all downstream utilization/pressure math stays weekly and unchanged.
    In 'actual' weeks-per-month mode, pass `week_start` to divide by the real
    number of weeks in that week's month; the result is that week's weekly cap.
    """
    tiers = tier_config.get("tiers") or {}
    global_period = cap_period(tier_config)
    wpm = weeks_per_month(tier_config, week_start)

    def weekly(cfg: dict) -> float:
        raw = raw_tier_cap(cfg)
        period = str(cfg.get("cap_period", global_period) or global_period).strip().lower()
        return raw / wpm if period == "monthly" else raw

    caps = {str(name): weekly(cfg) for name, cfg in tiers.items()}
    if "Baseline" not in caps:
        caps["Baseline"] = min(caps.values()) if caps else 100.0
    return caps


def tier_monthly_caps(tier_config: dict) -> dict[str, float]:
    """Monthly allowance per tier (the inverse view of tier_caps).

    Used for story/pace metrics that compare spend against a whole month's cap.
    Weekly-period tiers are scaled up by the representative weeks/month.
    """
    tiers = tier_config.get("tiers") or {}
    global_period = cap_period(tier_config)
    wpm = weeks_per_month(tier_config)
    out: dict[str, float] = {}
    for name, cfg in tiers.items():
        raw = raw_tier_cap(cfg)
        period = str(cfg.get("cap_period", global_period) or global_period).strip().lower()
        out[str(name)] = raw if period == "monthly" else raw * wpm
    if "Baseline" not in out:
        out["Baseline"] = min(out.values()) if out else 400.0
    return out


def highest_cap_tier(history: list[str], caps: dict[str, float]) -> str:
    """The group in `history` granting the largest credit allotment.

    Users can belong to several groups at once; their governance tier is the
    most generous one (ties break toward the most recent entry). Groups not
    in the cap config rank lowest; falls back to the last entry when none
    match the config at all."""
    known = [
        (float(caps[name]), idx, name)
        for idx, name in enumerate(history)
        if name in caps
    ]
    if known:
        return max(known)[2]
    return history[-1] if history else ""


def is_codex_access_tier(tier: object) -> bool:
    """Codex groups grant product access, not a credit-governance tier.

    They surface as a "Codex access" badge on the user profile and are excluded
    from optimization tier math so they never override a user's real credit tier.
    """
    return str(tier or "").strip().lower().startswith("codex")


def resolve_governance_assignments(
    assignments: dict[str, str] | None,
    histories: dict[str, list[str]] | None,
    caps: dict[str, float],
) -> dict[str, str]:
    """Resolve stored assignments to effective governance tiers.

    Codex groups count as real tiers when the policy gives them a cap (e.g.
    "Codex Users" at 3,000/month) — the Codex-access badge is tracked
    separately and survives either way. Only a Codex-flagged assignment with
    NO configured cap falls back to the user's most generous configured tier
    from the tierlist history; if they have no other tier at all, Codex
    access IS their tier — kept as-is rather than dropped to Baseline.
    """
    histories = histories or {}
    resolved: dict[str, str] = {}
    for email, tier in (assignments or {}).items():
        key = str(email).strip().lower()
        if not key:
            continue
        name = str(tier).strip()
        if not is_codex_access_tier(name) or name in caps:
            resolved[key] = name
            continue
        candidates = [
            str(past).strip() for past in histories.get(key, [])
            if str(past).strip() in caps
        ]
        resolved[key] = (highest_cap_tier(candidates, caps) if candidates else "") or name
    return resolved


def _recommendation_tiers(caps: dict[str, float]) -> dict[str, float]:
    preferred = [
        "Baseline",
        "Advanced Credit Users",
        "High Credit Consumption Users",
        "One K Credit Users",
        "Emergency Credit Users",
    ]
    ladder = {name: caps[name] for name in preferred if name in caps}
    if len(ladder) >= 2:
        return ladder

    baseline_cap = caps.get("Baseline", min(caps.values()) if caps else 100.0)
    credit_tiers = {
        name: cap
        for name, cap in caps.items()
        if name == "Baseline" or (cap > baseline_cap and ("Credit" in name or "One K" in name))
    }
    return credit_tiers or caps


def _tier_at_cap(candidates: dict[str, float], target_cap: float) -> tuple[str, float]:
    matches = [(name, cap) for name, cap in candidates.items() if cap == target_cap]
    order = {
        "Baseline": 0,
        "Advanced Credit Users": 1,
        "High Credit Consumption Users": 2,
        "One K Credit Users": 3,
        "Emergency Credit Users": 4,
    }
    return sorted(matches, key=lambda item: (order.get(item[0], 99), item[0]))[0]


def next_tier(current: str, caps: dict[str, float], direction: int) -> tuple[str, float]:
    if not caps:
        return ("Baseline", 100.0)
    current = current if current in caps else "Baseline"
    current_cap = caps.get(current, caps.get("Baseline", min(caps.values())))
    if direction == 0:
        return (current, current_cap)

    ladder = _recommendation_tiers(caps)
    ladder_caps = sorted(set(ladder.values()))
    if direction > 0:
        higher = [cap for cap in ladder_caps if cap > current_cap]
        return _tier_at_cap(ladder, higher[0]) if higher else (current, current_cap)

    lower = [cap for cap in ladder_caps if cap < current_cap]
    return _tier_at_cap(ladder, lower[-1]) if lower else (current, current_cap)



def pressure_flag(utilization: float) -> str:
    if utilization >= 1.10:
        return "ABOVE_CAP_110_PLUS"
    if utilization >= 1.00:
        return "AT_OR_ABOVE_CAP"
    if utilization >= 0.90:
        return "HIGH_PRESSURE_90_PLUS"
    if utilization >= 0.80:
        return "ELEVATED_PRESSURE_80_PLUS"
    return "NORMAL"


def _top_share(values: pd.Series, fraction: float) -> float:
    usage = pd.to_numeric(values, errors="coerce").fillna(0).clip(lower=0).sort_values(ascending=False)
    total = usage.sum()
    if total <= 0 or usage.empty:
        return 0.0
    return float(usage.head(max(1, math.ceil(len(usage) * fraction))).sum() / total)


def _derive_weekly_from_records(
    records_df: pd.DataFrame,
    tier_config: dict,
    tier_assignments: dict[str, str] | None = None,
) -> pd.DataFrame:
    required = {"email", "date_partition", "usage_credits"}
    if records_df is None or records_df.empty or not required.issubset(records_df.columns):
        return pd.DataFrame()

    caps = tier_caps(tier_config)
    baseline_cap = caps.get("Baseline", min(caps.values()) if caps else 100.0) or 1.0

    df = records_df.copy()
    df["_date"] = pd.to_datetime(df["date_partition"], errors="coerce")
    df = df.dropna(subset=["_date"])
    if df.empty:
        return pd.DataFrame()

    df["week_start"] = df["_date"] - pd.to_timedelta(df["_date"].dt.dayofweek, unit="D")
    df["week_end"] = df["week_start"] + pd.Timedelta(days=6)
    df["credits_used"] = pd.to_numeric(df["usage_credits"], errors="coerce").fillna(0)

    agg = {"credits_used": ("credits_used", "sum")}
    if "name" in df.columns:
        agg["latest_name"] = ("name", "last")
    if "department" in df.columns:
        agg["latest_department"] = ("department", "last")

    weekly = (
        df.groupby(["week_start", "week_end", "email"], as_index=False)
        .agg(**agg)
        .sort_values(["week_start", "credits_used"], ascending=[True, False])
    )
    assignments = {
        str(email).strip().lower(): str(tier).strip()
        for email, tier in (tier_assignments or {}).items()
        if str(email).strip() and str(tier).strip() in caps
    }
    weekly["_assignment_key"] = weekly["email"].astype(str).str.strip().str.lower()
    weekly["governance_tier"] = weekly["_assignment_key"].map(assignments).fillna("Baseline")
    weekly["tier_assignment_source"] = weekly["_assignment_key"].map(assignments).notna().map({
        True: "assigned",
        False: "default",
    })
    # The monthly->weekly divisor can vary by month ('actual' mode), so resolve
    # caps once per distinct month present, then look up each row's tier.
    caps_by_month = {
        (ts.year, ts.month): tier_caps(tier_config, week_start=ts)
        for ts in pd.to_datetime(weekly["week_start"].dropna().unique())
    }
    weekly["weekly_credit_cap"] = [
        caps_by_month.get((ws.year, ws.month), caps).get(tier, baseline_cap)
        for ws, tier in zip(weekly["week_start"], weekly["governance_tier"])
    ]
    weekly["cap_utilization"] = weekly["credits_used"] / weekly["weekly_credit_cap"].replace(0, baseline_cap)
    weekly["remaining_weekly_credits"] = weekly["weekly_credit_cap"] - weekly["credits_used"]
    weekly["pressure_flag"] = weekly["cap_utilization"].apply(pressure_flag)
    return weekly.drop(columns=["_assignment_key"])


def _trend(first: float, latest: float) -> str:
    delta = latest - first
    if delta >= 0.20:
        return "INCREASING_PRESSURE"
    if delta <= -0.20:
        return "DECREASING_PRESSURE"
    return "STABLE_PRESSURE"


def _recommended_action(row: pd.Series) -> str:
    if row["weeks_observed"] < 2:
        return "MONITOR_MORE_HISTORY_NEEDED"
    if row["weeks_observed"] >= 3 and row["share_weeks_over_90_percent_cap"] >= 0.50:
        return "CONSIDER_MOVE_UP_TIER"
    if row["weeks_observed"] >= 4 and row["avg_cap_utilization"] <= 0.25 and row["latest_cap_utilization"] <= 0.25:
        return "CONSIDER_MOVE_DOWN_TIER"
    if row["latest_cap_utilization"] >= 0.90:
        return "MONITOR_RECENT_SPIKE"
    return "NO_CHANGE"


def build_user_summary(user_week_history: pd.DataFrame) -> pd.DataFrame:
    if user_week_history.empty:
        return pd.DataFrame()
    df = user_week_history.sort_values(["email", "week_start"])
    agg = {
        "weeks_observed": ("week_start", "nunique"),
        "first_week_start": ("week_start", "min"),
        "latest_week_start": ("week_start", "max"),
        "total_credits_used": ("credits_used", "sum"),
        "avg_weekly_credits_used": ("credits_used", "mean"),
        "latest_credits_used": ("credits_used", "last"),
        "avg_cap_utilization": ("cap_utilization", "mean"),
        "latest_cap_utilization": ("cap_utilization", "last"),
        "first_cap_utilization": ("cap_utilization", "first"),
        "max_cap_utilization": ("cap_utilization", "max"),
        "weeks_over_90_percent_cap": ("cap_utilization", lambda s: int((s >= 0.90).sum())),
        "weeks_at_or_over_cap": ("cap_utilization", lambda s: int((s >= 1.00).sum())),
        "latest_governance_tier": ("governance_tier", "last"),
        "latest_weekly_credit_cap": ("weekly_credit_cap", "last"),
    }
    if "tier_assignment_source" in df.columns:
        agg["latest_tier_assignment_source"] = ("tier_assignment_source", "last")
    if "latest_name" in df.columns:
        agg["latest_name"] = ("latest_name", "last")
    if "latest_department" in df.columns:
        agg["latest_department"] = ("latest_department", "last")

    summary = df.groupby("email", as_index=False).agg(**agg)
    summary["share_weeks_over_90_percent_cap"] = summary["weeks_over_90_percent_cap"] / summary["weeks_observed"]
    summary["share_weeks_at_or_over_cap"] = summary["weeks_at_or_over_cap"] / summary["weeks_observed"]
    summary["pressure_trend"] = summary.apply(
        lambda row: _trend(row["first_cap_utilization"], row["latest_cap_utilization"]),
        axis=1,
    )
    summary["recommended_action"] = summary.apply(_recommended_action, axis=1)
    return summary


def build_recommendations(user_summary: pd.DataFrame, tier_config: dict) -> pd.DataFrame:
    if user_summary.empty:
        return pd.DataFrame()
    rows = user_summary.copy()
    # Resolve the tier ladder on the same weeks-per-month basis as each user's
    # latest week, so recommended caps line up with latest_weekly_credit_cap.
    caps_cache: dict[object, dict[str, float]] = {}

    def caps_for(week_start: object) -> dict[str, float]:
        key = (pd.Timestamp(week_start).year, pd.Timestamp(week_start).month) \
            if not pd.isna(week_start) else None
        if key not in caps_cache:
            caps_cache[key] = tier_caps(tier_config, week_start=week_start)
        return caps_cache[key]

    targets = []
    for _, row in rows.iterrows():
        action = row["recommended_action"]
        direction = 1 if action == "CONSIDER_MOVE_UP_TIER" else (-1 if action == "CONSIDER_MOVE_DOWN_TIER" else 0)
        caps = caps_for(row.get("latest_week_start"))
        targets.append(next_tier(str(row["latest_governance_tier"]), caps, direction))
    rows["recommended_tier"] = [target[0] for target in targets]
    rows["recommended_weekly_credit_cap"] = [target[1] for target in targets]
    rows["recommended_cap_change"] = rows["recommended_weekly_credit_cap"] - rows["latest_weekly_credit_cap"]
    rows["estimated_avg_utilization_after_change"] = rows["avg_weekly_credits_used"] / rows["recommended_weekly_credit_cap"].replace(0, float("nan"))
    rows["review_priority"] = rows["recommended_action"].map({
        "CONSIDER_MOVE_UP_TIER": "ACTIONABLE",
        "CONSIDER_MOVE_DOWN_TIER": "ACTIONABLE",
        "MONITOR_RECENT_SPIKE": "MONITOR",
        "MONITOR_MORE_HISTORY_NEEDED": "MONITOR",
        "NO_CHANGE": "INFORMATIONAL",
    }).fillna("INFORMATIONAL")
    rows["action_priority_rank"] = rows["recommended_action"].map(ACTION_PRIORITY).fillna(99)
    return rows.sort_values(
        ["action_priority_rank", "latest_cap_utilization", "total_credits_used"],
        ascending=[True, False, False],
    )


def build_optimization_result(
    records_df: pd.DataFrame,
    tier_config: dict,
    tier_assignments: dict[str, str] | None = None,
) -> OptimizationResult:
    user_week = _derive_weekly_from_records(records_df, tier_config, tier_assignments)
    if user_week.empty:
        empty = pd.DataFrame()
        return OptimizationResult("current project records", empty, empty, empty, empty, empty, {})

    user_summary = build_user_summary(user_week)
    recommendations = build_recommendations(user_summary, tier_config)
    rec_summary = (
        recommendations.groupby(["recommended_action", "review_priority"], as_index=False)
        .agg(
            users=("email", "count"),
            total_recommended_cap_change=("recommended_cap_change", "sum"),
            avg_latest_utilization=("latest_cap_utilization", "mean"),
        )
        .sort_values("users", ascending=False)
        if not recommendations.empty else pd.DataFrame()
    )
    tier_summary = (
        user_week.groupby("governance_tier", as_index=False)
        .agg(
            unique_users=("email", "nunique"),
            total_credits_used=("credits_used", "sum"),
            avg_cap_utilization=("cap_utilization", "mean"),
            user_weeks_over_90_percent_cap=("cap_utilization", lambda s: int((s >= 0.90).sum())),
        )
        .sort_values("total_credits_used", ascending=False)
        if not user_week.empty else pd.DataFrame()
    )

    latest_week = user_week[user_week["week_start"] == user_week["week_start"].max()].copy()
    latest_summary = {
        "week_start": str(latest_week["week_start"].iloc[0].date()) if not latest_week.empty else "",
        "week_end": str(latest_week["week_end"].iloc[0].date()) if not latest_week.empty else "",
        "users": int(len(latest_week)),
        "credit_active_users": int((latest_week["credits_used"] > 0).sum()) if not latest_week.empty else 0,
        "total_credits_used": float(latest_week["credits_used"].sum()) if not latest_week.empty else 0.0,
        "avg_cap_utilization": float(latest_week["cap_utilization"].mean()) if not latest_week.empty else 0.0,
        "users_over_90_percent_cap": int((latest_week["cap_utilization"] >= 0.90).sum()) if not latest_week.empty else 0,
        "users_at_or_over_cap": int((latest_week["cap_utilization"] >= 1.00).sum()) if not latest_week.empty else 0,
        "top_10_percent_consumption_share": _top_share(latest_week["credits_used"], 0.10) if not latest_week.empty else 0.0,
    }
    return OptimizationResult("current project records", user_week, user_summary, recommendations, rec_summary, tier_summary, latest_summary)

