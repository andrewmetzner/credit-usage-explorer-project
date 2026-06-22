from __future__ import annotations

import pandas as pd


def agg_credits(df: pd.DataFrame, group_col: str, top_n: int) -> list[dict]:
    agg_dict: dict = {
        "rows": ("usage_credits", "count"),
        "total_credits": ("usage_credits", "sum"),
    }
    if "email" in df.columns:
        agg_dict["unique_users"] = ("email", "nunique")
    agg = (
        df.groupby(group_col).agg(**agg_dict)
        .reset_index().sort_values("total_credits", ascending=False).head(top_n)
    )
    if "unique_users" not in agg.columns:
        agg["unique_users"] = 0
    return agg.to_dict(orient="records")


def aggregate_by_period(df: pd.DataFrame, col: str, top_n: int) -> list[dict]:
    return agg_credits(df, col, top_n) if col in df.columns else []


def aggregate_by_week(df: pd.DataFrame, top_n: int) -> list[dict]:
    if "date_partition" not in df.columns:
        return []
    wdf = df.copy()
    wdf["date_partition"] = pd.to_datetime(wdf["date_partition"], errors="coerce")
    wdf = wdf.dropna(subset=["date_partition"])
    if wdf.empty:
        return []
    wdf["week"] = wdf["date_partition"].dt.to_period("W").dt.start_time.dt.strftime("%Y-%m-%d")
    return agg_credits(wdf, "week", top_n)


def aggregate_by_period_fmt(
    df: pd.DataFrame, period: str, fmt: str, col_name: str, top_n: int
) -> list[dict]:
    if "date_partition" not in df.columns:
        return []
    pdf = df.copy()
    pdf["date_partition"] = pd.to_datetime(pdf["date_partition"], errors="coerce")
    pdf = pdf.dropna(subset=["date_partition"])
    if pdf.empty:
        return []
    pdf[col_name] = pdf["date_partition"].dt.to_period(period).dt.start_time.dt.strftime(fmt)
    return agg_credits(pdf, col_name, top_n)
