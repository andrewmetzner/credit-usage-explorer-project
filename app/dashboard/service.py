from __future__ import annotations

import json

import pandas as pd

# ── Records-page column model ───────────────────────────────────────────────
# One place that decides how each column is labeled, aligned, clipped, and
# formatted so the template can render generically (no per-column if/elif) and
# the wall of raw columns collapses to a clean, condensed default view.

# Friendly units for the merged "Quantity" cell (raw values are terse codes).
UNIT_LABELS = {"tokens": "tokens", "counts": "counts", "duration_s": "sec"}

# (label, align, clip) per known column. Unknown columns fall back to a
# title-cased label, left-aligned and clipped (safe for long id/text fields).
_RECORD_COLUMN_META: dict[str, tuple[str, str, bool]] = {
    "date_partition":         ("Date", "left", False),
    "name":                   ("User", "left", True),
    "email":                  ("Email", "left", True),
    "usage_type_parsed_type": ("Usage Type", "left", False),
    "usage_type_model":       ("Model", "left", True),
    "usage_quantity":         ("Quantity", "right", False),
    "usage_credits":          ("Credits", "right", False),
    "usage_units":            ("Units", "left", False),
    "usage_type":             ("Raw usage type", "left", True),
    "usage_type_io":          ("IO", "left", False),
    "usage_type_medium":      ("Medium", "left", False),
    "usage_type_date":        ("Usage Date", "left", False),
    "account_id":             ("Account ID", "left", True),
    "account_user_id":        ("Account User ID", "left", True),
    "public_id":              ("Public ID", "left", True),
}

# Curated, clean default view — parsed/corrected fields over the raw ones, so
# there's no horizontal scroll on first load (raw + id columns stay opt-in).
DEFAULT_RECORD_COLUMNS = [
    "date_partition", "name", "email",
    "usage_type_parsed_type", "usage_type_model",
    "usage_quantity", "usage_credits",
]


def record_column_meta(col: str) -> dict:
    """Render descriptor ({key, label, align, clip}) for one records column."""
    label, align, clip = _RECORD_COLUMN_META.get(
        col, (col.replace("_", " ").title(), "left", True)
    )
    return {"key": col, "label": label, "align": align, "clip": clip}


def _fmt_number(value, decimals: int = 2) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return ""
    if pd.isna(f):
        return ""
    if f == int(f):
        return f"{int(f):,}"
    # Non-integer: fixed decimals, then drop trailing zeros (73.70 -> 73.7).
    return f"{f:,.{decimals}f}".rstrip("0").rstrip(".")


def _format_record_cell(col: str, row: dict, units_present: bool) -> str:
    """Display string for one cell — numbers get thousands separators, Quantity
    absorbs its unit, and empty/"N/A" values render as an em dash."""
    value = row.get(col)
    if col == "usage_credits":
        return _fmt_number(value, 2)
    if col == "usage_quantity":
        qty = _fmt_number(value, 2)
        if units_present:
            unit = str(row.get("usage_units") or "").strip()
            return f"{qty} {UNIT_LABELS.get(unit, unit)}".strip()
        return qty
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    text = str(value).strip()
    return "—" if text in ("", "N/A", "nan") else text


def build_record_view(df: pd.DataFrame, selected_fields: list[str]) -> tuple[list[dict], list[dict]]:
    """(columns, rows) for the records table: column descriptors + formatted rows
    keyed by column, so the template renders every cell the same way."""
    columns = [record_column_meta(c) for c in selected_fields]
    units_present = "usage_units" in df.columns
    rows = [
        {c: _format_record_cell(c, rec, units_present) for c in selected_fields}
        for rec in df.to_dict(orient="records")
    ]
    return columns, rows


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


def compute_weekly_trend(df: pd.DataFrame, contract_start: str = "") -> str:
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
    # Weeks starting before the contract are "pre-contract" (rendered gray and
    # hidden by the in-contract scope filter). No contract set → treat all as
    # in-contract so the chart stays normal.
    cstart = pd.to_datetime(contract_start, errors="coerce")
    cstart = None if pd.isna(cstart) else cstart
    return json.dumps([
        {
            "week": str(row["week"].date()),
            "total_credits": round(float(row["total_credits"]), 2),
            "unique_users": int(row["unique_users"]),
            "in_contract": bool(cstart is None or row["week"] >= cstart),
        }
        for _, row in weekly.iterrows()
    ])


def compute_daily_trend(df: pd.DataFrame, contract_start: str = "") -> str:
    """Daily credits burned, filled across the full date span."""
    if "date_partition" not in df.columns or "usage_credits" not in df.columns:
        return "[]"

    ddf = df[["date_partition", "usage_credits", "email"]].copy()
    ddf["_date"] = pd.to_datetime(ddf["date_partition"], errors="coerce")
    ddf = ddf.dropna(subset=["_date"])
    if ddf.empty:
        return "[]"
    ddf["_day"] = ddf["_date"].dt.normalize()

    daily = (
        ddf.groupby("_day", as_index=False)
        .agg(total_credits=("usage_credits", "sum"), unique_users=("email", "nunique"))
        .sort_values("_day")
        .rename(columns={"_day": "day"})
    )

    full_days = pd.DataFrame({"day": pd.date_range(daily["day"].min(), daily["day"].max(), freq="D")})
    daily = full_days.merge(daily, on="day", how="left").fillna({"total_credits": 0.0, "unique_users": 0})

    cstart = pd.to_datetime(contract_start, errors="coerce")
    cstart = None if pd.isna(cstart) else cstart
    return json.dumps([
        {
            "day": str(row["day"].date()),
            "total_credits": round(float(row["total_credits"]), 2),
            "unique_users": int(row["unique_users"]),
            "in_contract": bool(cstart is None or row["day"] >= cstart),
        }
        for _, row in daily.iterrows()
    ])


def compute_active_users_weekly(df: pd.DataFrame, contract_start: str = "") -> str:
    """Active users (distinct emails with credits > 0) per Monday-anchored week.

    Built straight from the raw frame — same week grouping as the weekly-burn /
    usage-type charts — so all three Summary charts cover the exact same weeks.
    (The forecast service's historical/operational split drops a week that
    straddles the contract start, which is why that source skipped a week.)
    """
    if "date_partition" not in df.columns or "email" not in df.columns:
        return "[]"
    cols = ["date_partition", "email"] + (["usage_credits"] if "usage_credits" in df.columns else [])
    wdf = df[cols].copy()
    wdf["_date"] = pd.to_datetime(wdf["date_partition"], errors="coerce")
    wdf = wdf.dropna(subset=["_date"])
    if "usage_credits" in wdf.columns:
        wdf = wdf[pd.to_numeric(wdf["usage_credits"], errors="coerce").fillna(0.0) > 0]
    if wdf.empty:
        return "[]"
    wdf["week"] = wdf["_date"] - pd.to_timedelta(wdf["_date"].dt.dayofweek, unit="D")
    weekly = (
        wdf.groupby("week", as_index=False)
        .agg(active_users=("email", "nunique"))
        .sort_values("week")
    )
    cstart = pd.to_datetime(contract_start, errors="coerce")
    cstart = None if pd.isna(cstart) else cstart
    return json.dumps([
        {
            "week_start": str(row["week"].date()),
            "active_users": int(row["active_users"]),
            "in_contract": bool(cstart is None or row["week"] >= cstart),
        }
        for _, row in weekly.iterrows()
    ])
