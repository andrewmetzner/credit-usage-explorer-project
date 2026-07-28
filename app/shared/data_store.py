from __future__ import annotations

import io
import re
import threading
from datetime import datetime
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
        # Reloads can race (scheduler thread, per-request mtime check, upload).
        self._reload_lock = threading.Lock()
        self._loaded_signature = self._file_signature()

    @property
    def data(self) -> CreditUsageData:
        return self._data

    @property
    def path(self) -> Path:
        return self._path

    @property
    def revision(self) -> str:
        """Token for the loaded file's on-disk identity (mtime + size), polled
        by open pages to detect new data. Derived from the file, not a counter,
        so it stays stable across an app restart of the same sheet and only
        changes when the file is actually rewritten."""
        mtime_ns, size = self._loaded_signature
        return f"{mtime_ns}-{size}"

    def _file_signature(self) -> tuple[int, int]:
        """(mtime_ns, size) of the data file — a cheap os.stat, no read."""
        try:
            st = self._path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return (0, 0)

    def reload(self, new_path: Path | None = None) -> None:
        with self._reload_lock:
            if new_path:
                self._path = new_path
            self._data = CreditUsageData(self._path)
            self._loaded_signature = self._file_signature()

    def reload_if_changed(self) -> bool:
        """Reload from disk if the file changed since it was last loaded; return
        True if it did. Called before every request so the serving process
        picks up a background sync's writes without a restart. Cheap on the
        common no-change path (just an os.stat)."""
        if self._file_signature() == self._loaded_signature:
            return False
        with self._reload_lock:
            sig = self._file_signature()
            if sig == self._loaded_signature:
                return False
            try:
                new_data = CreditUsageData(self._path)
            except Exception:
                # Transient read error (e.g. mid-write); keep current data, retry next call.
                return False
            self._data = new_data
            self._loaded_signature = sig
            return True


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

        col = self.df["usage_type"]
        # Parse each DISTINCT usage_type once (dozens of values across ~25k
        # rows) and map back, rather than running the regex parser per row.
        parsed = {val: parse_usage_type(val) for val in col.dropna().unique()}
        na = parse_usage_type(None)

        def field(key: str):
            return col.map({v: p[key] for v, p in parsed.items()}).where(col.notna(), na[key])

        self.df["usage_type_parsed_type"] = field("type")
        self.df["usage_type_model"] = field("model_and_num")
        self.df["usage_type_date"] = field("date")
        self.df["usage_type_medium"] = field("medium")
        self.df["usage_type_io"] = field("io")

        # View-only correction (not written back to the data file).
        apply_usage_corrections(self.df)

        for col in ["usage_type_parsed_type", "usage_type_model", "usage_type_date",
                    "usage_type_medium", "usage_type_io"]:
            if col not in self.columns:
                self.columns.append(col)

    def _add_timestamp(self) -> None:
        """Build a combined date+time `timestamp` column for display. API-synced
        rows carry a UTC pull time in data_source ("API GET <ISO>"), shown in
        local time; other rows get midnight, flagged by `timestamp_has_time`
        (read by _format_record_cell) so they render as "--:--:--"."""
        if "date_partition" not in self.df.columns:
            return

        df = self.df
        # Vectorized: parse the whole date column once (was pd.to_datetime/row).
        dates = pd.to_datetime(df["date_partition"], errors="coerce")
        normalized = dates.dt.normalize()
        timestamp = normalized.copy()
        has_time = pd.Series(False, index=df.index)

        if "data_source" in df.columns:
            # Pull time is embedded as "API GET <ISO UTC>". Parse only the
            # distinct timestamps into a local time-of-day offset per row.
            raw = df["data_source"].astype("string").str.extract(r"API GET\s+(\S+)", expand=False)
            valid = raw.notna() & dates.notna()
            if valid.any():
                offset_by_raw: dict = {}
                for r in raw[valid].unique():
                    try:
                        local = datetime.fromisoformat(str(r).replace("Z", "+00:00")).astimezone()
                        offset_by_raw[r] = pd.Timedelta(hours=local.hour, minutes=local.minute, seconds=local.second)
                    except (ValueError, TypeError):
                        offset_by_raw[r] = None
                offsets = raw.map(offset_by_raw)
                apply_mask = valid & offsets.notna()
                if apply_mask.any():
                    timestamp = timestamp.mask(apply_mask, normalized + offsets)
                    has_time = apply_mask

        df["timestamp"] = timestamp
        df["timestamp_has_time"] = has_time.to_numpy()
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
