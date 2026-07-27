from __future__ import annotations

import io
import re
from datetime import datetime, time
from pathlib import Path
from typing import Any

import pandas as pd

from .data_filters import apply_usage_corrections
from .utils import parse_usage_type

# Matches the pull-time timestamp embedded in the data_source column by
# openai_admin_API/sync_usage.py, e.g. "API GET 2026-07-24T16:04:18Z".
_PULL_TIME_RE = re.compile(r"API GET\s+(\S+)")


def read_tabular_file(source: "Path | bytes", suffix: str) -> pd.DataFrame:
    """Read a tabular data file (xlsx/xls/csv) from either a path on disk or
    raw in-memory bytes (an upload) — pandas' readers accept both. CSVs try
    utf-8-sig first (handles a BOM some export tools add), falling back to
    cp1252 for older Windows-exported files that trip UnicodeDecodeError.
    Re-opens `source` fresh for each read attempt: a Path just reopens the
    file, and bytes get wrapped in a new BytesIO each time so a failed
    utf-8-sig attempt can't leave the buffer partially consumed for the
    cp1252 retry."""
    def _open():
        return source if isinstance(source, Path) else io.BytesIO(source)

    suffix = suffix.lower()
    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(_open(), sheet_name=0)
    try:
        return pd.read_csv(_open(), encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(_open(), encoding="cp1252")


class DataStore:
    """Mutable holder for CreditUsageData that supports live reload after a file upload."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data = CreditUsageData(path)

    @property
    def data(self) -> CreditUsageData:
        return self._data

    @property
    def path(self) -> Path:
        return self._path

    def reload(self, new_path: Path | None = None) -> None:
        if new_path:
            self._path = new_path
        self._data = CreditUsageData(self._path)


class CreditUsageData:
    def __init__(self, data_path: Path) -> None:
        self.data_path = data_path
        self.df = self._load_data()
        self.columns = list(self.df.columns)
        self._add_parsed_usage_type()
        self._add_timestamp()

    def _load_data(self) -> pd.DataFrame:
        if not self.data_path.exists():
            return pd.DataFrame()
        df = read_tabular_file(self.data_path, self.data_path.suffix)

        if "usage_credits" in df.columns:
            df["usage_credits"] = pd.to_numeric(df["usage_credits"], errors="coerce").fillna(0.0)
        if "usage_quantity" in df.columns:
            df["usage_quantity"] = pd.to_numeric(df["usage_quantity"], errors="coerce").fillna(0.0)

        return df

    def _add_parsed_usage_type(self) -> None:
        if "usage_type" not in self.df.columns:
            return

        parsed = self.df["usage_type"].apply(parse_usage_type)
        self.df["usage_type_parsed_type"] = parsed.apply(lambda x: x["type"])
        self.df["usage_type_model"] = parsed.apply(lambda x: x["model_and_num"])
        self.df["usage_type_date"] = parsed.apply(lambda x: x["date"])
        self.df["usage_type_medium"] = parsed.apply(lambda x: x["medium"])
        self.df["usage_type_io"] = parsed.apply(lambda x: x["io"])

        # View-only correction: this DataFrame is loaded for the app, not
        # written back to the uploaded/current data sheet.
        apply_usage_corrections(self.df)

        for col in ["usage_type_parsed_type", "usage_type_model", "usage_type_date",
                    "usage_type_medium", "usage_type_io"]:
            if col not in self.columns:
                self.columns.append(col)

    def _add_timestamp(self) -> None:
        """A combined date+time column for display: rows pulled by the
        ChatGPT Admin API sync carry their actual pull time in `data_source`
        (e.g. "API GET 2026-07-24T16:04:18Z", stored UTC — converted to
        local time here); every other row (manual uploads, or an API row
        whose data_source has no parseable timestamp) gets midnight, since
        there's no finer-grained time to show for those."""
        if "date_partition" not in self.df.columns:
            return

        source_col = self.df["data_source"] if "data_source" in self.df.columns else None

        def _row_timestamp(date_val, source_val):
            d = pd.to_datetime(date_val, errors="coerce")
            if pd.isna(d):
                return pd.NaT
            time_part = time(0, 0)
            if isinstance(source_val, str):
                match = _PULL_TIME_RE.search(source_val)
                if match:
                    try:
                        dt_utc = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
                        time_part = dt_utc.astimezone().time()
                    except ValueError:
                        pass
            return datetime.combine(d.date(), time_part)

        if source_col is not None:
            self.df["timestamp"] = [
                _row_timestamp(d, s) for d, s in zip(self.df["date_partition"], source_col)
            ]
        else:
            self.df["timestamp"] = [_row_timestamp(d, None) for d in self.df["date_partition"]]
        if "timestamp" not in self.columns:
            self.columns.append("timestamp")

    def filter_by_date(
        self,
        df: pd.DataFrame,
        start_date: str = "",
        end_date: str = "",
        col: str = "date_partition",
    ) -> pd.DataFrame:
        if not (start_date or end_date) or col not in df.columns:
            return df
        date_col = pd.to_datetime(df[col], errors="coerce")
        mask = pd.Series(True, index=df.index)
        if start_date:
            s = pd.to_datetime(start_date, errors="coerce")
            if not pd.isna(s):
                mask &= date_col >= s
        if end_date:
            e = pd.to_datetime(end_date, errors="coerce")
            if not pd.isna(e):
                mask &= date_col <= e
        return df[mask]

    def filter_by_credits(
        self,
        df: pd.DataFrame,
        min_credits: str = "",
        max_credits: str = "",
        zero_credits: str = "",
    ) -> pd.DataFrame:
        if "usage_credits" not in df.columns:
            return df
        credits = pd.to_numeric(df["usage_credits"], errors="coerce")
        if zero_credits == "1":
            return df[credits.fillna(0) == 0]
        if min_credits:
            min_val = pd.to_numeric(min_credits, errors="coerce")
            if not pd.isna(min_val):
                df = df[credits >= min_val]
        if max_credits:
            max_val = pd.to_numeric(max_credits, errors="coerce")
            if not pd.isna(max_val):
                df = df[credits <= max_val]
        return df

    def summary_metrics(self) -> dict[str, Any]:
        return {
            "total_rows": len(self.df),
            "total_credits": float(self.df["usage_credits"].sum())
            if "usage_credits" in self.df.columns else 0.0,
            "total_quantity": float(self.df["usage_quantity"].sum())
            if "usage_quantity" in self.df.columns else 0.0,
            "unique_emails": int(self.df["email"].nunique())
            if "email" in self.df.columns else 0,
            "usage_types": int(self.df["usage_type"].nunique())
            if "usage_type" in self.df.columns else 0,
        }
