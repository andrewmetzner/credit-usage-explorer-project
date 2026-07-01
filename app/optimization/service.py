from __future__ import annotations

import ast
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd


DEFAULT_GROUP_TO_TIER = {
    "Advanced Credit Users": "Advanced",
    "High Credit Consumption Users": "Super",
    "One K Credit Users": "Highest",
}

DEFAULT_SPECIAL_GROUPS = {
    "Emergency Credit Users": "emergency_override",
}

ACTION_PRIORITY = {
    "REVIEW_EMERGENCY_OVERRIDE": 1,
    "CONSIDER_MOVE_UP_TIER": 2,
    "CONSIDER_MOVE_DOWN_TIER": 3,
    "MONITOR_RECENT_SPIKE": 4,
    "MONITOR_MORE_HISTORY_NEEDED": 5,
    "NO_CHANGE": 6,
}


@dataclass
class OptimizationResult:
    source_label: str
    user_week_history: pd.DataFrame
    user_summary: pd.DataFrame
    recommendations: pd.DataFrame
    recommendation_summary: pd.DataFrame
    tier_week_summary: pd.DataFrame
    tier_summary: pd.DataFrame
    latest_summary: dict[str, Any]


def parse_groups(value) -> list[str]:
    if pd.isna(value):
        return []
    if isinstance(value, dict):
        return [str(v) for v in value.values()]
    if isinstance(value, list):
        return [str(v) for v in value]
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return []
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return [text]
    if isinstance(parsed, dict):
        return [str(v) for v in parsed.values()]
    if isinstance(parsed, list):
        return [str(v) for v in parsed]
    return [str(parsed)]


def _tier_caps(tier_config: dict) -> dict[str, float]:
    tiers = tier_config.get("tiers") or {}
    caps = {str(name): float(cfg.get("weekly_credit_cap", 0) or 0) for name, cfg in tiers.items()}
    if "Baseline" not in caps:
        caps["Baseline"] = min(caps.values()) if caps else 100.0
    return caps


def _assign_tier(groups: list[str], group_to_tier: dict, tier_caps: dict[str, float]) -> str:
    matched = [group_to_tier[g] for g in groups if g in group_to_tier and group_to_tier[g] in tier_caps]
    if not matched:
        return "Baseline"
    return max(matched, key=lambda tier: tier_caps.get(tier, -math.inf))


def _pressure_flag(utilization: float) -> str:
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


def _gini(values: pd.Series) -> float:
    usage = pd.to_numeric(values, errors="coerce").fillna(0).clip(lower=0).sort_values().reset_index(drop=True)
    total = usage.sum()
    n = len(usage)
    if n == 0 or total <= 0:
        return 0.0
    weighted = sum((idx + 1) * value for idx, value in enumerate(usage))
    return float((2 * weighted) / (n * total) - (n + 1) / n)


def _hhi(values: pd.Series) -> float:
    usage = pd.to_numeric(values, errors="coerce").fillna(0).clip(lower=0)
    total = usage.sum()
    if total <= 0:
        return 0.0
    shares = usage / total
    return float((shares ** 2).sum())


def _cap_pressure_index(user_detail: pd.DataFrame) -> float:
    if user_detail.empty:
        return 0.0
    util = user_detail["cap_utilization"]
    avg_util = util.clip(lower=0, upper=1).mean()
    users = len(user_detail)
    share_80 = (util >= 0.80).sum() / users
    share_90 = (util >= 0.90).sum() / users
    share_100 = (util >= 1.00).sum() / users
    threshold_pressure = 0.25 * share_80 + 0.35 * share_90 + 0.40 * share_100
    top_5 = _top_share(user_detail["credits_used"], 0.05)
    top_10 = _top_share(user_detail["credits_used"], 0.10)
    concentration = 0.40 * min(top_5 / 0.50, 1) + 0.30 * min(top_10 / 0.70, 1) + 0.30 * min(_gini(user_detail["credits_used"]) / 0.80, 1)
    return float(min(max(100 * (0.40 * avg_util + 0.30 * threshold_pressure + 0.30 * concentration), 0), 100))


def _pressure_level(index: float) -> str:
    if index >= 80:
        return "CRITICAL"
    if index >= 60:
        return "HIGH"
    if index >= 30:
        return "MODERATE"
    return "LOW"


def _latest(series: pd.Series):
    return series.iloc[-1]


def _mode(series: pd.Series):
    modes = series.dropna().mode()
    return None if modes.empty else modes.iloc[0]


def _trend(first: float, latest: float) -> str:
    delta = latest - first
    if delta >= 0.20:
        return "INCREASING_PRESSURE"
    if delta <= -0.20:
        return "DECREASING_PRESSURE"
    return "STABLE_PRESSURE"


def _recommended_action(row: pd.Series) -> str:
    if bool(row.get("ever_emergency_override", False)):
        return "REVIEW_EMERGENCY_OVERRIDE"
    if row["weeks_observed"] < 2:
        return "MONITOR_MORE_HISTORY_NEEDED"
    if row["weeks_observed"] >= 3 and row["share_weeks_over_90_percent_cap"] >= 0.50:
        return "CONSIDER_MOVE_UP_TIER"
    if row["weeks_observed"] >= 4 and row["avg_cap_utilization"] <= 0.25 and row["latest_cap_utilization"] <= 0.25:
        return "CONSIDER_MOVE_DOWN_TIER"
    if row["latest_cap_utilization"] >= 0.90 and row["share_weeks_over_90_percent_cap"] < 0.50:
        return "MONITOR_RECENT_SPIKE"
    return "NO_CHANGE"


def _prepare_operational_history(processed_dir: Path) -> pd.DataFrame | None:
    path = processed_dir / "weekly_operational_usage_all.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    if df.empty or not {"email", "credits_used", "week_start", "week_end"}.issubset(df.columns):
        return None
    return df


def _derive_weekly_from_records(df: pd.DataFrame) -> pd.DataFrame | None:
    if df is None or df.empty or not {"email", "date_partition", "usage_credits"}.issubset(df.columns):
        return None
    wdf = df.copy()
    wdf["_date"] = pd.to_datetime(wdf["date_partition"], errors="coerce")
    wdf = wdf.dropna(subset=["_date"])
    if wdf.empty:
        return None
    wdf["week_start"] = wdf["_date"] - pd.to_timedelta(wdf["_date"].dt.dayofweek, unit="D")
    wdf["week_end"] = wdf["week_start"] + pd.Timedelta(days=6)
    wdf["credits_used"] = pd.to_numeric(wdf["usage_credits"], errors="coerce").fillna(0)
    group_cols = ["week_start", "week_end", "email"]
    named = "name" in wdf.columns
    agg = {"credits_used": ("credits_used", "sum")}
    if named:
        agg["name"] = ("name", "last")
    weekly = wdf.groupby(group_cols, as_index=False).agg(**agg)
    weekly["groups"] = "[]"
    weekly["source_file"] = "current-project-records"
    return weekly


def build_user_week_history(weekly_df: pd.DataFrame, tier_config: dict) -> pd.DataFrame:
    caps = _tier_caps(tier_config)
    group_to_tier = tier_config.get("group_to_tier", DEFAULT_GROUP_TO_TIER)
    special_groups = tier_config.get("special_groups", DEFAULT_SPECIAL_GROUPS)

    df = weekly_df.copy()
    df["week_start"] = pd.to_datetime(df["week_start"], errors="coerce")
    df["week_end"] = pd.to_datetime(df["week_end"], errors="coerce")
    df["credits_used"] = pd.to_numeric(df["credits_used"], errors="coerce").fillna(0)
    if "groups" not in df.columns:
        df["groups"] = "[]"
    df["parsed_groups"] = df["groups"].apply(parse_groups)
    df["governance_tier"] = df["parsed_groups"].apply(lambda groups: _assign_tier(groups, group_to_tier, caps))
    df["weekly_credit_cap"] = df["governance_tier"].map(caps).fillna(caps.get("Baseline", 100.0)).replace(0, 1)
    df["cap_utilization"] = df["credits_used"] / df["weekly_credit_cap"]
    df["remaining_weekly_credits"] = df["weekly_credit_cap"] - df["credits_used"]
    df["is_over_80_percent_cap"] = df["cap_utilization"] >= 0.80
    df["is_over_90_percent_cap"] = df["cap_utilization"] >= 0.90
    df["is_at_or_over_cap"] = df["cap_utilization"] >= 1.00
    df["is_over_110_percent_cap"] = df["cap_utilization"] >= 1.10
    df["pressure_flag"] = df["cap_utilization"].apply(_pressure_flag)
    for group_name, flag in special_groups.items():
        df[flag] = df["parsed_groups"].apply(lambda groups, g=group_name: g in groups)
    df["groups_parsed_text"] = df["parsed_groups"].apply(lambda groups: "; ".join(groups))
    return df.sort_values(["week_start", "credits_used"], ascending=[True, False]).reset_index(drop=True)


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
        "latest_credits_used": ("credits_used", _latest),
        "avg_cap_utilization": ("cap_utilization", "mean"),
        "latest_cap_utilization": ("cap_utilization", _latest),
        "first_cap_utilization": ("cap_utilization", lambda s: s.iloc[0]),
        "max_cap_utilization": ("cap_utilization", "max"),
        "weeks_over_90_percent_cap": ("is_over_90_percent_cap", "sum"),
        "weeks_at_or_over_cap": ("is_at_or_over_cap", "sum"),
        "latest_governance_tier": ("governance_tier", _latest),
        "most_common_governance_tier": ("governance_tier", _mode),
        "latest_weekly_credit_cap": ("weekly_credit_cap", _latest),
    }
    for col in ("name", "department"):
        if col in df.columns:
            agg[f"latest_{col}"] = (col, _latest)
    if "emergency_override" in df.columns:
        agg["ever_emergency_override"] = ("emergency_override", "max")
        agg["emergency_override_weeks"] = ("emergency_override", "sum")
    summary = df.groupby("email", as_index=False).agg(**agg)
    summary["share_weeks_over_90_percent_cap"] = summary["weeks_over_90_percent_cap"] / summary["weeks_observed"]
    summary["share_weeks_at_or_over_cap"] = summary["weeks_at_or_over_cap"] / summary["weeks_observed"]
    if "ever_emergency_override" not in summary.columns:
        summary["ever_emergency_override"] = False
        summary["emergency_override_weeks"] = 0
    summary["pressure_trend"] = summary.apply(lambda row: _trend(row["first_cap_utilization"], row["latest_cap_utilization"]), axis=1)
    summary["recommended_action"] = summary.apply(_recommended_action, axis=1)
    return summary


def _next_tier(current: str, caps: dict[str, float], direction: int) -> tuple[str, float]:
    ordered = sorted(caps.items(), key=lambda item: item[1])
    names = [name for name, _ in ordered]
    if current not in names:
        current = "Baseline"
    idx = names.index(current)
    idx = max(0, min(len(ordered) - 1, idx + direction))
    return ordered[idx]


def build_recommendations(user_summary: pd.DataFrame, tier_config: dict) -> pd.DataFrame:
    if user_summary.empty:
        return pd.DataFrame()
    caps = _tier_caps(tier_config)
    rows = user_summary.copy()
    targets = []
    for _, row in rows.iterrows():
        action = row["recommended_action"]
        direction = 1 if action == "CONSIDER_MOVE_UP_TIER" else (-1 if action == "CONSIDER_MOVE_DOWN_TIER" else 0)
        targets.append(_next_tier(str(row["latest_governance_tier"]), caps, direction))
    rows["recommended_tier"] = [t[0] for t in targets]
    rows["recommended_weekly_credit_cap"] = [t[1] for t in targets]
    rows["recommended_cap_change"] = rows["recommended_weekly_credit_cap"] - rows["latest_weekly_credit_cap"]
    rows["estimated_avg_utilization_after_change"] = rows["avg_weekly_credits_used"] / rows["recommended_weekly_credit_cap"].replace(0, float("nan"))
    rows["review_priority"] = rows["recommended_action"].map({
        "REVIEW_EMERGENCY_OVERRIDE": "URGENT",
        "CONSIDER_MOVE_UP_TIER": "ACTIONABLE",
        "CONSIDER_MOVE_DOWN_TIER": "ACTIONABLE",
        "MONITOR_RECENT_SPIKE": "MONITOR",
        "MONITOR_MORE_HISTORY_NEEDED": "MONITOR",
        "NO_CHANGE": "INFORMATIONAL",
    }).fillna("INFORMATIONAL")
    rows["action_priority_rank"] = rows["recommended_action"].map(ACTION_PRIORITY).fillna(99)
    return rows.sort_values(["action_priority_rank", "latest_cap_utilization", "total_credits_used"], ascending=[True, False, False])


def build_optimization_result(processed_dir: Path, records_df: pd.DataFrame, tier_config: dict) -> OptimizationResult:
    weekly = _prepare_operational_history(processed_dir)
    source_label = "project weekly operational uploads"
    if weekly is None:
        weekly = _derive_weekly_from_records(records_df)
        source_label = "current project records"
    if weekly is None:
        empty = pd.DataFrame()
        return OptimizationResult(source_label, empty, empty, empty, empty, empty, empty, {})

    user_week = build_user_week_history(weekly, tier_config)
    user_summary = build_user_summary(user_week)
    recommendations = build_recommendations(user_summary, tier_config)
    rec_summary = (
        recommendations.groupby(["recommended_action", "review_priority"], as_index=False)
        .agg(users=("email", "count"), total_recommended_cap_change=("recommended_cap_change", "sum"), avg_latest_utilization=("latest_cap_utilization", "mean"))
        .sort_values("users", ascending=False)
        if not recommendations.empty else pd.DataFrame()
    )
    tier_week = (
        user_week.groupby(["week_start", "week_end", "governance_tier"], as_index=False)
        .agg(users=("email", "count"), total_credits_used=("credits_used", "sum"), avg_cap_utilization=("cap_utilization", "mean"), users_over_90_percent_cap=("is_over_90_percent_cap", "sum"), users_at_or_over_cap=("is_at_or_over_cap", "sum"))
        if not user_week.empty else pd.DataFrame()
    )
    tier_summary = (
        user_week.groupby("governance_tier", as_index=False)
        .agg(unique_users=("email", "nunique"), total_credits_used=("credits_used", "sum"), avg_cap_utilization=("cap_utilization", "mean"), user_weeks_over_90_percent_cap=("is_over_90_percent_cap", "sum"))
        .sort_values("total_credits_used", ascending=False)
        if not user_week.empty else pd.DataFrame()
    )

    latest_week = user_week[user_week["week_start"] == user_week["week_start"].max()].copy()
    cpi = _cap_pressure_index(latest_week)
    latest_summary = {
        "week_start": str(latest_week["week_start"].iloc[0].date()) if not latest_week.empty else "",
        "week_end": str(latest_week["week_end"].iloc[0].date()) if not latest_week.empty else "",
        "users": int(len(latest_week)),
        "credit_active_users": int((latest_week["credits_used"] > 0).sum()) if not latest_week.empty else 0,
        "total_credits_used": float(latest_week["credits_used"].sum()) if not latest_week.empty else 0.0,
        "avg_cap_utilization": float(latest_week["cap_utilization"].mean()) if not latest_week.empty else 0.0,
        "users_over_90_percent_cap": int(latest_week["is_over_90_percent_cap"].sum()) if not latest_week.empty else 0,
        "users_at_or_over_cap": int(latest_week["is_at_or_over_cap"].sum()) if not latest_week.empty else 0,
        "top_10_percent_consumption_share": _top_share(latest_week["credits_used"], 0.10) if not latest_week.empty else 0.0,
        "gini_coefficient": _gini(latest_week["credits_used"]) if not latest_week.empty else 0.0,
        "hhi": _hhi(latest_week["credits_used"]) if not latest_week.empty else 0.0,
        "cap_pressure_index": cpi,
        "pressure_level": _pressure_level(cpi),
    }
    return OptimizationResult(source_label, user_week, user_summary, recommendations, rec_summary, tier_week, tier_summary, latest_summary)
