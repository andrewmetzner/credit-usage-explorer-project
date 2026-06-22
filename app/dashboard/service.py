from __future__ import annotations

import json

import pandas as pd


def compute_summary_metrics(df: pd.DataFrame) -> dict:
    total_credits = float(df["usage_credits"].sum()) if "usage_credits" in df.columns else 0.0
    unique_users = int(df["email"].nunique()) if "email" in df.columns else 0
    total_records = len(df)
    usage_types = int(df["usage_type"].nunique()) if "usage_type" in df.columns else 0

    date_min = date_max = None
    active_users_recent = 0
    if "date_partition" in df.columns:
        dates = pd.to_datetime(df["date_partition"], errors="coerce").dropna()
        if not dates.empty:
            date_min = str(dates.min().date())
            date_max = str(dates.max().date())
            recent_cutoff = dates.max() - pd.Timedelta(days=7)
            recent_mask = pd.to_datetime(df["date_partition"], errors="coerce") >= recent_cutoff
            recent_df = df[recent_mask]
            if "usage_credits" in recent_df.columns and "email" in recent_df.columns:
                active_users_recent = int(
                    recent_df[recent_df["usage_credits"] > 0]["email"].nunique()
                )

    return {
        "total_credits": total_credits,
        "unique_users": unique_users,
        "total_records": total_records,
        "usage_types": usage_types,
        "date_min": date_min,
        "date_max": date_max,
        "active_users_recent": active_users_recent,
    }


def compute_outlier_users(
    df: pd.DataFrame,
    credit_threshold: float,
    lookback_days: int,
    model_filter: str,
) -> tuple[list[dict], int, str, str]:
    if "date_partition" not in df.columns or "usage_credits" not in df.columns or "email" not in df.columns:
        return [], 0, "", ""

    dates_all = pd.to_datetime(df["date_partition"], errors="coerce").dropna()
    if dates_all.empty:
        return [], 0, "", ""

    cutoff = dates_all.max() - pd.Timedelta(days=lookback_days)
    lookback_start = str(cutoff.date())
    lookback_end = str(dates_all.max().date())
    recent = df[pd.to_datetime(df["date_partition"], errors="coerce") >= cutoff].copy()

    if model_filter and "usage_type_model" in recent.columns:
        recent = recent[recent["usage_type_model"] == model_filter]

    if recent.empty:
        return [], 0, lookback_start, lookback_end

    group_cols = [c for c in ["name", "email"] if c in recent.columns]
    user_recent = (
        recent.groupby(group_cols, as_index=False)
        .agg(recent_credits=("usage_credits", "sum"), recent_records=("usage_credits", "count"))
        .sort_values("recent_credits", ascending=False)
    )

    if "usage_type_model" in recent.columns:
        top_model = (
            recent.groupby(group_cols + ["usage_type_model"], as_index=False)
            .agg(mc=("usage_credits", "sum"))
            .sort_values("mc", ascending=False)
            .drop_duplicates(subset=group_cols)
        )
        user_recent = user_recent.merge(top_model[group_cols + ["usage_type_model"]], on=group_cols, how="left")
    else:
        user_recent["usage_type_model"] = ""

    outlier_count = int((user_recent["recent_credits"] > credit_threshold).sum())
    outlier_users = user_recent[user_recent["recent_credits"] > credit_threshold].to_dict("records")
    return outlier_users, outlier_count, lookback_start, lookback_end


def compute_weekly_trend(df: pd.DataFrame) -> str:
    if "date_partition" not in df.columns or "usage_credits" not in df.columns:
        return "[]"
    wdf = df[["date_partition", "usage_credits", "email"]].copy()
    wdf["_date"] = pd.to_datetime(wdf["date_partition"], errors="coerce")
    wdf = wdf.dropna(subset=["_date"])
    wdf["week"] = wdf["_date"] - pd.to_timedelta(wdf["_date"].dt.dayofweek, unit="D")
    weekly = (
        wdf.groupby("week", as_index=False)
        .agg(total_credits=("usage_credits", "sum"), unique_users=("email", "nunique"))
        .sort_values("week")
    )
    return json.dumps([
        {
            "week": str(row["week"].date()),
            "total_credits": round(float(row["total_credits"]), 2),
            "unique_users": int(row["unique_users"]),
        }
        for _, row in weekly.iterrows()
    ])
