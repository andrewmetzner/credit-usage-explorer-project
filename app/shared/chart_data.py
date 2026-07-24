"""Shared chart-data builders.

These produce JSON-serialisable structures consumed by the front-end Chart.js
code. Keeping them here (rather than inline in each blueprint) lets any page —
Summary, Forecast, Leaderboard — render the same chart from one source of truth.
"""
from __future__ import annotations

import json

import pandas as pd

from .contracts import contract_ids_for_dates

# Preferred column for the "usage type" breakdown, in order of readability.
# The parsed category (chat / codex / voice / ...) is far cleaner than the raw
# usage_type string, which can have hundreds of distinct values.
_USAGE_TYPE_COLS = ("usage_type_parsed_type", "usage_type_model", "usage_type")


def _pick_type_column(df: pd.DataFrame, preferred: str | None = None) -> str | None:
    if preferred and preferred in df.columns:
        return preferred
    for col in _USAGE_TYPE_COLS:
        if col in df.columns:
            return col
    return None


def usage_type_weekly(
    df: pd.DataFrame,
    type_col: str | None = None,
    top_n: int = 8,
    value_col: str = "usage_credits",
    contracts: list[dict] | None = None,
) -> dict:
    """Weekly credits broken down by usage type, ready for a stacked bar chart.

    Returns:
        {
          "weeks": ["2026-03-02", ...],          # Monday-anchored week starts
          "series": [{"name": "chat", "data": [..]}, ...],   # one per type
          "contract_id": ["abc123", null, ...],  # parallel to weeks
          "in_contract": [true, false, ...],     # parallel to weeks
          "type_col": "usage_type_parsed_type",
          "total": 1234.5,
        }
    Types beyond ``top_n`` (by total credits) are bucketed into "Other".
    Returns empty structure when the required columns are absent.
    """
    if df is None or df.empty:
        return {"weeks": [], "series": [], "contract_id": [], "in_contract": [], "type_col": None, "total": 0.0}

    type_column = _pick_type_column(df, type_col)
    if (
        type_column is None
        or "date_partition" not in df.columns
        or value_col not in df.columns
    ):
        return {"weeks": [], "series": [], "contract_id": [], "in_contract": [], "type_col": type_column, "total": 0.0}

    work = df[["date_partition", type_column, value_col]].copy()
    work["_date"] = pd.to_datetime(work["date_partition"], errors="coerce")
    work = work.dropna(subset=["_date"])
    if work.empty:
        return {"weeks": [], "series": [], "contract_id": [], "in_contract": [], "type_col": type_column, "total": 0.0}

    work["_week"] = work["_date"] - pd.to_timedelta(work["_date"].dt.dayofweek, unit="D")
    work["_type"] = work[type_column].fillna("N/A").astype(str).replace("", "N/A")
    work["_val"] = pd.to_numeric(work[value_col], errors="coerce").fillna(0.0)

    # Rank types by total credits; collapse the long tail into "Other".
    totals = work.groupby("_type")["_val"].sum().sort_values(ascending=False)
    keep = list(totals.head(top_n).index)
    if len(totals) > top_n:
        work["_type"] = work["_type"].where(work["_type"].isin(keep), "Other")
        ordered_types = keep + ["Other"]
    else:
        ordered_types = keep

    pivot = (
        work.groupby(["_week", "_type"])["_val"].sum().unstack(fill_value=0.0).sort_index()
    )
    # Preserve the credit-ranked column order
    ordered_types = [t for t in ordered_types if t in pivot.columns]
    pivot = pivot[ordered_types]

    weeks = [str(w.date()) for w in pivot.index]
    series = [
        {"name": str(t), "data": [round(float(v), 2) for v in pivot[t].tolist()]}
        for t in ordered_types
    ]
    # Per-week contract id (which configured contract's window the week start
    # falls into, or None for pre-contract/gap weeks). No contracts configured
    # -> every week is None, same "scope filter is a no-op" behavior as before.
    contract_id = contract_ids_for_dates(pivot.index.to_series(), contracts)
    in_contract = [cid is not None for cid in contract_id]
    return {
        "weeks": weeks,
        "series": series,
        "contract_id": contract_id,
        "in_contract": in_contract,
        "type_col": type_column,
        "total": round(float(work["_val"].sum()), 2),
    }


def usage_type_weekly_json(df: pd.DataFrame, **kwargs) -> str:
    return json.dumps(usage_type_weekly(df, **kwargs))
