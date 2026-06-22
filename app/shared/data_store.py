from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from .utils import parse_usage_type


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

    def _load_data(self) -> pd.DataFrame:
        if not self.data_path.exists():
            return pd.DataFrame()
        suffix = self.data_path.suffix.lower()
        if suffix in (".xlsx", ".xls"):
            df = pd.read_excel(self.data_path, sheet_name=0)
        else:
            try:
                df = pd.read_csv(self.data_path, encoding="utf-8-sig")
            except UnicodeDecodeError:
                df = pd.read_csv(self.data_path, encoding="cp1252")

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

        for col in ["usage_type_parsed_type", "usage_type_model", "usage_type_date",
                    "usage_type_medium", "usage_type_io"]:
            if col not in self.columns:
                self.columns.append(col)

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
