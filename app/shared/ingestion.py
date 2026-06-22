from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


def _clean_column_names(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = (
        df.columns
        .str.strip()
        .str.lower()
        .str.replace(" ", "_", regex=False)
        .str.replace("-", "_", regex=False)
        .str.replace("/", "_", regex=False)
        .str.replace("(", "", regex=False)
        .str.replace(")", "", regex=False)
    )
    return df


def _find_column(df: pd.DataFrame, required_terms: list[str], fallback_candidates: list[str]) -> str:
    for candidate in fallback_candidates:
        if candidate in df.columns:
            return candidate
    for col in df.columns:
        if all(term in col for term in required_terms):
            return col
    raise ValueError(
        f"Could not identify column with terms {required_terms}. "
        f"Columns found: {list(df.columns)}"
    )


def _find_optional_column(
    df: pd.DataFrame,
    required_terms: list[str],
    fallback_candidates: list[str],
) -> str | None:
    try:
        return _find_column(df, required_terms, fallback_candidates)
    except ValueError:
        return None


def _infer_week_from_filename(path: Path) -> tuple[str | None, str | None]:
    name = path.stem
    match = re.search(r"(\d{4}-\d{2}-\d{2})[\s_]*-[\s_]*(\d{4}-\d{2}-\d{2})", name)
    if match:
        return match.group(1), match.group(2)
    return None, None


class IngestionPipeline:
    HISTORICAL_WEEKLY_SUMMARY = "historical_weekly_summary.csv"
    HISTORICAL_USAGE_CLEANED = "historical_usage_cleaned.csv"
    HISTORICAL_USER_SUMMARY = "historical_user_summary.csv"
    WEEKLY_SUMMARY_ALL = "weekly_summary_all.csv"
    WEEKLY_OPERATIONAL_ALL = "weekly_operational_usage_all.csv"
    LATEST_WEEK_SUMMARY = "latest_week_summary.csv"
    LATEST_WEEK_OPERATIONAL = "latest_week_operational_usage_cleaned.csv"
    CONTRACT_STATUS_SUMMARY = "contract_status_summary.csv"
    FORECAST_SUMMARY = "forecast_summary.csv"
    FORECAST_HISTORY = "forecast_history.csv"
    SNAPSHOTS_DIR    = "snapshots"
    UPLOAD_HISTORY_FILE = "upload_history.json"

    def __init__(self, processed_dir: Path) -> None:
        self.processed_dir = processed_dir
        processed_dir.mkdir(parents=True, exist_ok=True)

    def ingest_historical(self, file_path: Path) -> dict[str, Any]:
        suffix = file_path.suffix.lower()
        if suffix in (".xlsx", ".xls"):
            try:
                df = pd.read_excel(file_path, sheet_name="weekly_user_export")
            except Exception:
                df = pd.read_excel(file_path, sheet_name=0)
        else:
            df = pd.read_csv(file_path)

        df = _clean_column_names(df)

        required_cols = ["period_start", "period_end", "email", "messages", "credits_used"]
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            raise ValueError(f"Missing required columns in historical file: {missing}")

        df["period_start"] = pd.to_datetime(df["period_start"])
        df["period_end"] = pd.to_datetime(df["period_end"])
        df["credits_used"] = pd.to_numeric(df["credits_used"], errors="coerce").fillna(0)
        df["messages"] = pd.to_numeric(df["messages"], errors="coerce").fillna(0)
        df["is_credit_active"] = df["credits_used"] > 0
        df["is_message_active"] = df["messages"] > 0

        existing_path = self.processed_dir / self.HISTORICAL_USAGE_CLEANED
        if existing_path.exists():
            existing = pd.read_csv(existing_path)
            existing["period_start"] = pd.to_datetime(existing["period_start"])
            existing["period_end"] = pd.to_datetime(existing["period_end"])
            df = pd.concat([existing, df], ignore_index=True)
            df = df.drop_duplicates(subset=["period_start", "period_end", "email"], keep="last")

        df = df.sort_values(["period_start", "email"]).reset_index(drop=True)

        weekly = (
            df.groupby(["period_start", "period_end"], as_index=False)
            .agg(
                total_credits_used=("credits_used", "sum"),
                total_messages=("messages", "sum"),
                credit_active_users=("is_credit_active", "sum"),
                message_active_users=("is_message_active", "sum"),
                unique_users=("email", "nunique"),
            )
            .sort_values("period_start")
        )
        weekly["credits_per_credit_active_user"] = (
            weekly["total_credits_used"] / weekly["credit_active_users"].replace(0, float("nan"))
        )

        user_summary = (
            df.groupby("email", as_index=False)
            .agg(
                user_total_credits=("credits_used", "sum"),
                user_total_messages=("messages", "sum"),
                active_credit_weeks=("is_credit_active", "sum"),
                active_message_weeks=("is_message_active", "sum"),
                first_period=("period_start", "min"),
                last_period=("period_start", "max"),
            )
            .sort_values("user_total_credits", ascending=False)
        )

        df.to_csv(self.processed_dir / self.HISTORICAL_USAGE_CLEANED, index=False)
        weekly.to_csv(self.processed_dir / self.HISTORICAL_WEEKLY_SUMMARY, index=False)
        user_summary.to_csv(self.processed_dir / self.HISTORICAL_USER_SUMMARY, index=False)

        stats = {
            "rows": len(df),
            "weeks": int(df["period_start"].nunique()),
            "users": int(df["email"].nunique()),
            "total_credits": float(df["credits_used"].sum()),
        }
        self._log_upload("historical", file_path.name, stats)
        return stats

    def ingest_weekly(
        self,
        file_path: Path,
        week_start: str | None = None,
        week_end: str | None = None,
    ) -> dict[str, Any]:
        df = pd.read_csv(file_path)
        df = _clean_column_names(df)

        email_col = _find_column(df, ["email"], ["email", "user_email", "email_address"])
        credit_col = _find_column(
            df,
            ["credit"],
            ["credits_used", "credit_used", "total_credits_used", "credits", "amu_credits"],
        )
        message_col = _find_optional_column(
            df,
            ["message"],
            ["messages", "total_messages", "message_count"],
        )

        rename_map: dict[str, str] = {email_col: "email", credit_col: "credits_used"}
        if message_col and message_col != "messages":
            rename_map[message_col] = "messages"
        df = df.rename(columns=rename_map)

        if "messages" not in df.columns:
            df["messages"] = 0

        df["credits_used"] = pd.to_numeric(df["credits_used"], errors="coerce").fillna(0)
        df["messages"] = pd.to_numeric(df["messages"], errors="coerce").fillna(0)
        df["is_credit_active"] = df["credits_used"] > 0
        df["is_message_active"] = df["messages"] > 0

        inferred_start, inferred_end = _infer_week_from_filename(file_path)
        week_start = week_start or inferred_start
        week_end = week_end or inferred_end

        if not week_start or not week_end:
            raise ValueError(
                "Could not infer week dates from filename. "
                "Please supply week_start and week_end (YYYY-MM-DD)."
            )

        df["week_start"] = pd.to_datetime(week_start)
        df["week_end"] = pd.to_datetime(week_end)
        df["source_file"] = file_path.name

        credit_active = df[df["is_credit_active"]]
        week_summary = {
            "week_start": df["week_start"].iloc[0],
            "week_end": df["week_end"].iloc[0],
            "source_file": df["source_file"].iloc[0],
            "reported_rows": len(df),
            "unique_users": int(df["email"].nunique()),
            "message_active_users": int(df["is_message_active"].sum()),
            "credit_active_users": int(df["is_credit_active"].sum()),
            "total_credits_used": float(df["credits_used"].sum()),
            "total_messages": float(df["messages"].sum()),
            "avg_credits_per_credit_active_user": float(credit_active["credits_used"].mean())
            if len(credit_active) > 0 else 0.0,
            "median_credits_per_credit_active_user": float(credit_active["credits_used"].median())
            if len(credit_active) > 0 else 0.0,
            "p95_credits_per_credit_active_user": float(credit_active["credits_used"].quantile(0.95))
            if len(credit_active) > 0 else 0.0,
        }
        summary_df = pd.DataFrame([week_summary])

        df.to_csv(self.processed_dir / self.LATEST_WEEK_OPERATIONAL, index=False)
        summary_df.to_csv(self.processed_dir / self.LATEST_WEEK_SUMMARY, index=False)

        self._append_without_duplicate_weeks(df, self.WEEKLY_OPERATIONAL_ALL)
        self._append_without_duplicate_weeks(summary_df, self.WEEKLY_SUMMARY_ALL)

        result = {
            "week_start": str(week_start),
            "week_end": str(week_end),
            "rows": len(df),
            "unique_users": week_summary["unique_users"],
            "credit_active_users": week_summary["credit_active_users"],
            "total_credits": week_summary["total_credits_used"],
        }
        self._log_upload("weekly", file_path.name, result)
        return result

    def _append_without_duplicate_weeks(self, new_df: pd.DataFrame, filename: str) -> None:
        output_file = self.processed_dir / filename
        date_col = "week_start"

        if output_file.exists():
            existing = pd.read_csv(output_file)
            existing[date_col] = pd.to_datetime(existing[date_col])
            new_week_start = pd.to_datetime(new_df[date_col].iloc[0])
            existing = existing[existing[date_col] != new_week_start]
            combined = pd.concat([existing, new_df], ignore_index=True)
        else:
            combined = new_df.copy()

        sort_cols = [date_col, "email"] if "email" in combined.columns else [date_col]
        combined = combined.sort_values(sort_cols).reset_index(drop=True)
        combined.to_csv(output_file, index=False)

    def get_historical_weekly_summary(self) -> pd.DataFrame | None:
        path = self.processed_dir / self.HISTORICAL_WEEKLY_SUMMARY
        if not path.exists():
            return None
        df = pd.read_csv(path)
        df["period_start"] = pd.to_datetime(df["period_start"])
        df["period_end"] = pd.to_datetime(df["period_end"])
        return df

    def get_operational_weekly_summary(self) -> pd.DataFrame | None:
        path = self.processed_dir / self.WEEKLY_SUMMARY_ALL
        if not path.exists():
            return None
        df = pd.read_csv(path)
        if df.empty:
            return None
        df["week_start"] = pd.to_datetime(df["week_start"])
        df["week_end"] = pd.to_datetime(df["week_end"])
        return df.sort_values("week_start").reset_index(drop=True)

    def get_ingested_weeks(self) -> list[dict[str, Any]]:
        path = self.processed_dir / self.WEEKLY_SUMMARY_ALL
        if not path.exists():
            return []
        df = pd.read_csv(path)
        if df.empty:
            return []
        df["week_start"] = pd.to_datetime(df["week_start"])
        df["week_end"] = pd.to_datetime(df["week_end"])
        df = df.sort_values("week_start", ascending=False)
        records = []
        for _, row in df.iterrows():
            records.append({
                "week_start": str(row["week_start"].date()),
                "week_end": str(row["week_end"].date()),
                "source_file": row.get("source_file", ""),
                "total_credits_used": float(row.get("total_credits_used", 0)),
                "unique_users": int(row.get("unique_users", 0)),
                "credit_active_users": int(row.get("credit_active_users", 0)),
            })
        return records

    def record_upload(self, upload_type: str, filename: str, stats: dict) -> None:
        """Public entry point for recording a sheet upload in the history log."""
        self._log_upload(upload_type, filename, stats)

    def _log_upload(self, upload_type: str, filename: str, stats: dict) -> None:
        path = self.processed_dir / self.UPLOAD_HISTORY_FILE
        try:
            history: list = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        except Exception:
            history = []
        history.insert(0, {
            "ts": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "type": upload_type,
            "filename": filename,
            "stats": stats,
        })
        path.write_text(json.dumps(history, indent=2), encoding="utf-8")

    def get_upload_history(self) -> list[dict]:
        path = self.processed_dir / self.UPLOAD_HISTORY_FILE
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def delete_historical(self) -> bool:
        deleted = False
        for filename in (
            self.HISTORICAL_WEEKLY_SUMMARY,
            self.HISTORICAL_USAGE_CLEANED,
            self.HISTORICAL_USER_SUMMARY,
        ):
            path = self.processed_dir / filename
            if path.exists():
                try:
                    path.unlink()
                    deleted = True
                except Exception:
                    pass
        return deleted

    def delete_week(self, week_start_str: str) -> bool:
        target = pd.to_datetime(week_start_str)
        deleted = False

        for filename in (self.WEEKLY_SUMMARY_ALL, self.WEEKLY_OPERATIONAL_ALL):
            path = self.processed_dir / filename
            if not path.exists():
                continue
            df = pd.read_csv(path)
            df["week_start"] = pd.to_datetime(df["week_start"])
            before = len(df)
            df = df[df["week_start"] != target].reset_index(drop=True)
            if len(df) < before:
                deleted = True
            df.to_csv(path, index=False)

        return deleted

    def status(self) -> dict[str, Any]:
        hist_path = self.processed_dir / self.HISTORICAL_WEEKLY_SUMMARY
        op_path = self.processed_dir / self.WEEKLY_SUMMARY_ALL

        has_historical = hist_path.exists()
        historical_weeks = 0
        if has_historical:
            try:
                hdf = pd.read_csv(hist_path)
                historical_weeks = len(hdf)
            except Exception:
                historical_weeks = 0

        has_operational = op_path.exists()
        operational_weeks = 0
        if has_operational:
            try:
                odf = pd.read_csv(op_path)
                operational_weeks = len(odf)
                if operational_weeks == 0:
                    has_operational = False
            except Exception:
                operational_weeks = 0
                has_operational = False

        return {
            "has_historical": has_historical,
            "historical_weeks": historical_weeks,
            "has_operational": has_operational,
            "operational_weeks": operational_weeks,
        }

    @staticmethod
    def _ts_to_series_path(processed_dir: Path, snapshot_ts: str) -> Path:
        fname = snapshot_ts.replace(":", "-").replace("T", "_") + ".json"
        return processed_dir / "snapshots" / fname

    def get_snapshot_series(self, snapshot_ts: str) -> dict | None:
        import json
        path = self._ts_to_series_path(self.processed_dir, snapshot_ts)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def get_forecast_history(self, limit: int = 30) -> list[dict[str, Any]]:
        path = self.processed_dir / self.FORECAST_HISTORY
        if not path.exists():
            return []
        try:
            df = pd.read_csv(path, dtype=str, keep_default_na=False)
            if df.empty or "snapshot_date" not in df.columns:
                return []
            df = df.sort_values("snapshot_date", ascending=False).head(limit)
            return df.fillna("").to_dict("records")
        except Exception:
            return []

    def delete_snapshot(self, snapshot_ts: str, snapshot_date: str = "", label: str = "") -> tuple[bool, str]:
        path = self.processed_dir / self.FORECAST_HISTORY
        if not path.exists():
            return False, "History file not found."
        try:
            df = pd.read_csv(path, dtype=str, keep_default_na=False)
            before = len(df)
            ts_clean = snapshot_ts if snapshot_ts not in ("nan", "None") else ""
            lbl_clean = label if label not in ("nan", "None") else ""
            if ts_clean and "snapshot_ts" in df.columns:
                df = df[df["snapshot_ts"] != ts_clean]
            else:
                date_mask = df["snapshot_date"] == snapshot_date
                label_col = (
                    df["label"] if "label" in df.columns
                    else pd.Series([""] * len(df), index=df.index)
                )
                df = df[~(date_mask & (label_col == lbl_clean))]
            if len(df) == before:
                return False, "No matching snapshot found."
            tmp = path.with_suffix(".tmp")
            df.reset_index(drop=True).to_csv(tmp, index=False)
            tmp.replace(path)
            if ts_clean:
                json_path = self._ts_to_series_path(self.processed_dir, ts_clean)
                if json_path.exists():
                    try:
                        json_path.unlink()
                    except Exception:
                        pass
            return True, ""
        except Exception as exc:
            return False, str(exc)

    def rename_snapshot(
        self, snapshot_ts: str, snapshot_date: str, old_label: str, new_label: str
    ) -> tuple[bool, str]:
        path = self.processed_dir / self.FORECAST_HISTORY
        if not path.exists():
            return False, "History file not found."
        try:
            df = pd.read_csv(path, dtype=str, keep_default_na=False)
            ts_clean = snapshot_ts if snapshot_ts not in ("nan", "None") else ""
            if ts_clean and "snapshot_ts" in df.columns:
                mask = df["snapshot_ts"] == ts_clean
            else:
                date_mask = df["snapshot_date"] == snapshot_date
                lbl_col = (
                    df["label"] if "label" in df.columns
                    else pd.Series([""] * len(df), index=df.index)
                )
                mask = date_mask & (lbl_col == old_label)
            if not mask.any():
                return False, "No matching snapshot found."
            if "label" not in df.columns:
                df["label"] = ""
            df.loc[mask, "label"] = new_label
            tmp = path.with_suffix(".tmp")
            df.to_csv(tmp, index=False)
            tmp.replace(path)
            if ts_clean:
                json_path = self._ts_to_series_path(self.processed_dir, ts_clean)
                if json_path.exists():
                    try:
                        import json as _json
                        data = _json.loads(json_path.read_text(encoding="utf-8"))
                        data["label"] = new_label
                        jtmp = json_path.with_suffix(".tmp")
                        jtmp.write_text(_json.dumps(data), encoding="utf-8")
                        jtmp.replace(json_path)
                    except Exception:
                        pass
            return True, ""
        except Exception as exc:
            return False, str(exc)

    def set_snapshot_color(
        self, snapshot_ts: str, snapshot_date: str, label: str, color: str
    ) -> tuple[bool, str]:
        path = self.processed_dir / self.FORECAST_HISTORY
        if not path.exists():
            return False, "History file not found."
        try:
            df = pd.read_csv(path, dtype=str, keep_default_na=False)
            ts_clean = snapshot_ts if snapshot_ts not in ("nan", "None") else ""
            if ts_clean and "snapshot_ts" in df.columns:
                mask = df["snapshot_ts"] == ts_clean
            else:
                date_mask = df["snapshot_date"] == snapshot_date
                lbl_col = (
                    df["label"] if "label" in df.columns
                    else pd.Series([""] * len(df), index=df.index)
                )
                mask = date_mask & (lbl_col == label)
            if not mask.any():
                return False, "No matching snapshot found."
            if "color" not in df.columns:
                df["color"] = ""
            df.loc[mask, "color"] = color
            tmp = path.with_suffix(".tmp")
            df.to_csv(tmp, index=False)
            tmp.replace(path)
            if ts_clean:
                json_path = self._ts_to_series_path(self.processed_dir, ts_clean)
                if json_path.exists():
                    try:
                        import json as _json
                        data = _json.loads(json_path.read_text(encoding="utf-8"))
                        data["color"] = color
                        jtmp = json_path.with_suffix(".tmp")
                        jtmp.write_text(_json.dumps(data), encoding="utf-8")
                        jtmp.replace(json_path)
                    except Exception:
                        pass
            return True, ""
        except Exception as exc:
            return False, str(exc)

    def has_forecast_history(self) -> bool:
        return (self.processed_dir / self.FORECAST_HISTORY).exists()
