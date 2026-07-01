from __future__ import annotations

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


def tier_caps(tier_config: dict) -> dict[str, float]:
    tiers = tier_config.get("tiers") or {}
    caps = {str(name): float(cfg.get("weekly_credit_cap", 0) or 0) for name, cfg in tiers.items()}
    if "Baseline" not in caps:
        caps["Baseline"] = min(caps.values()) if caps else 100.0
    return caps


def next_tier(current: str, caps: dict[str, float], direction: int) -> tuple[str, float]:
    ordered = sorted(caps.items(), key=lambda item: item[1])
    names = [name for name, _ in ordered]
    if current not in names:
        current = "Baseline"
    idx = names.index(current)
    idx = max(0, min(len(ordered) - 1, idx + direction))
    return ordered[idx]


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


def _derive_weekly_from_records(records_df: pd.DataFrame, tier_config: dict) -> pd.DataFrame:
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
    weekly["governance_tier"] = "Baseline"
    weekly["weekly_credit_cap"] = baseline_cap
    weekly["cap_utilization"] = weekly["credits_used"] / baseline_cap
    weekly["remaining_weekly_credits"] = weekly["weekly_credit_cap"] - weekly["credits_used"]
    weekly["pressure_flag"] = weekly["cap_utilization"].apply(pressure_flag)
    return weekly


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
    caps = tier_caps(tier_config)
    rows = user_summary.copy()
    targets = []
    for _, row in rows.iterrows():
        action = row["recommended_action"]
        direction = 1 if action == "CONSIDER_MOVE_UP_TIER" else (-1 if action == "CONSIDER_MOVE_DOWN_TIER" else 0)
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


def build_optimization_result(records_df: pd.DataFrame, tier_config: dict) -> OptimizationResult:
    user_week = _derive_weekly_from_records(records_df, tier_config)
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

