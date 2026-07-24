"""Order-independent merging of overlapping, inconsistent usage data files.

The credit-usage exports we ingest routinely overlap (different date windows of
the same data) and can disagree on values for the same record, and they do not
even agree on date formatting (``2026-03-03`` vs ``6/1/2026``).  This module
makes appending such files *foolproof*:

* Dates are canonicalised per-frame so mixed formats can never corrupt
  comparison or storage (see :func:`normalize_date_partition`).
* Rows are deduplicated on a natural record key, so the same line item is never
  double-counted no matter how many overlapping files are loaded.
* When two files disagree on a record's value, the larger ``usage_credits`` wins.
  ``max`` is commutative, so the merged result is identical regardless of upload
  order — the core property that was previously broken.
"""
from __future__ import annotations

import pandas as pd

DATE_COL = "date_partition"
VALUE_COLS = ("usage_credits", "usage_quantity")
USER_ID_COLS = ("account_user_id", "email", "public_id")


def normalize_date_partition(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with ``date_partition`` rewritten to ISO ``YYYY-MM-DD``.

    Parsing happens while the column is still a single source format, so pandas'
    format inference cannot be poisoned by mixing US-style and ISO dates in one
    Series.  Values that cannot be parsed keep their original text.
    """
    if DATE_COL not in df.columns:
        return df
    df = df.copy()
    parsed = pd.to_datetime(df[DATE_COL], errors="coerce")
    iso = parsed.dt.strftime("%Y-%m-%d")
    df[DATE_COL] = iso.where(parsed.notna(), df[DATE_COL])
    return df


def record_key(columns) -> list[str] | None:
    """Build the natural record key from whatever identity columns are present.

    Returns ``None`` when a safe key cannot be formed (missing date, user
    identifier, or usage type), signalling callers to fall back to exact-row
    deduplication rather than risk collapsing distinct records.
    """
    cols = set(columns)
    key: list[str] = []
    if DATE_COL in cols:
        key.append(DATE_COL)
    # Sub-day granularity, if present — currently only ever populated by the
    # ChatGPT Admin API sync (openai_admin_API/sync_usage.py), which reports
    # in hourly buckets rather than the daily grain a manual upload has.
    # Manually-uploaded rows simply have no value here, so they keep
    # deduping on date_partition alone exactly as before this existed —
    # NaN safely compares equal to NaN for drop_duplicates()'s purposes.
    if "hour" in cols:
        key.append("hour")
    if "account_id" in cols:
        key.append("account_id")
    for user_col in USER_ID_COLS:
        if user_col in cols:
            key.append(user_col)
            break
    if "usage_type" in cols:
        key.append("usage_type")

    has_user = any(u in key for u in USER_ID_COLS)
    if DATE_COL in key and has_user and "usage_type" in key:
        return key
    return None


def dedupe_usage(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse duplicate usage records deterministically (order-independent).

    One row is kept per natural key; on a value conflict the row with the
    greatest ``usage_credits`` survives.  Output is sorted by date for stable,
    readable on-disk ordering.
    """
    if df is None or df.empty:
        return df.reset_index(drop=True) if df is not None else df

    key = record_key(df.columns)

    if key is None:
        out = df.drop_duplicates()
    elif "usage_credits" in df.columns:
        order = pd.to_numeric(df["usage_credits"], errors="coerce").fillna(float("-inf"))
        out = (
            df.assign(_merge_order=order)
            .sort_values(key + ["_merge_order"], kind="stable")
            .drop_duplicates(subset=key, keep="last")
            .drop(columns="_merge_order")
        )
    else:
        out = df.drop_duplicates(subset=key, keep="first")

    if DATE_COL in out.columns:
        out = out.sort_values(DATE_COL, kind="stable")
    return out.reset_index(drop=True)


def merge_usage_data(
    existing: pd.DataFrame | None, new: pd.DataFrame
) -> pd.DataFrame:
    """Merge ``new`` into ``existing``, returning a clean, deduplicated frame.

    Safe to call with ``existing=None`` (fresh load) — the new frame is still
    normalised and de-duplicated.  The result is identical for any order in
    which a given set of files is merged.
    """
    new = normalize_date_partition(new)
    if existing is None or existing.empty:
        return dedupe_usage(new)
    existing = normalize_date_partition(existing)
    combined = pd.concat([existing, new], ignore_index=True)
    return dedupe_usage(combined)
