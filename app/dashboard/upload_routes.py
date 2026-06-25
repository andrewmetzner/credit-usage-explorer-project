"""Multi-sheet upload + merge route on the `main` blueprint."""
from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
from flask import flash, redirect, request, url_for

from app.shared.data_merge import merge_usage_data

# Columns the ingestion step derives from `usage_type`; dropped before re-merge
# so they get recomputed cleanly rather than duplicated.
DERIVED_COLS = {
    "usage_type_parsed_type", "usage_type_model", "usage_type_date",
    "usage_type_medium", "usage_type_io",
}


def register_upload_routes(bp, services) -> None:
    store = services.store
    pipeline = services.pipeline
    config_svc = services.config_svc

    def _read_upload(file_storage) -> pd.DataFrame:
        """Read an uploaded sheet (xlsx/xls/csv) into a DataFrame from memory."""
        suffix = Path(file_storage.filename).suffix.lower()
        raw = file_storage.read()
        if suffix in (".xlsx", ".xls"):
            return pd.read_excel(io.BytesIO(raw), sheet_name=0)
        try:
            return pd.read_csv(io.BytesIO(raw), encoding="utf-8-sig")
        except UnicodeDecodeError:
            return pd.read_csv(io.BytesIO(raw), encoding="cp1252")

    @bp.route("/upload-data", methods=["POST"])
    def upload_data() -> object:
        from config import CURRENT_DATA_PATH, CURRENT_DATA_PATH_CACHE, DEFAULT_DATA_PATH

        files = [f for f in request.files.getlist("file") if f and f.filename]
        if not files:
            flash("No file selected.", "danger")
            return redirect(url_for("main.summary_page"))

        allowed = {".xlsx", ".xls", ".csv"}
        for f in files:
            suffix = Path(f.filename).suffix.lower()
            if suffix not in allowed:
                flash(f"Unsupported file type '{suffix}' in \"{f.filename}\". Use .xlsx, .xls, or .csv.", "danger")
                return redirect(url_for("main.summary_page"))

        # "Replace" discards current data; otherwise new sheets merge into it.
        replace = request.form.get("replace_existing") == "on"
        has_existing = (
            not replace
            and store.path != DEFAULT_DATA_PATH
            and not store.data.df.empty
        )
        working_df = (
            store.data.df.drop(columns=[c for c in DERIVED_COLS if c in store.data.df.columns], errors="ignore")
            if has_existing else None
        )
        rows_before_all = len(working_df) if working_df is not None else 0

        # Merge every uploaded sheet in turn (order doesn't matter — merge is
        # commutative), tracking how many new records each one contributed.
        per_file: list[dict] = []
        try:
            for f in files:
                rows_before = len(working_df) if working_df is not None else 0
                new_df = _read_upload(f)
                rows_in_file = len(new_df)
                working_df = merge_usage_data(working_df, new_df)
                per_file.append({
                    "filename": f.filename,
                    "rows_in_file": rows_in_file,
                    "rows_added": len(working_df) - rows_before,
                })
        except Exception as exc:
            flash(f"Error processing uploaded data: {exc}", "danger")
            return redirect(url_for("main.summary_page"))

        # Persist the merged result as a single canonical CSV; clear any stale
        # current_data.* siblings (e.g. a previous .xlsx) to avoid ambiguity.
        saved_path = CURRENT_DATA_PATH.with_suffix(".csv")
        saved_path.parent.mkdir(parents=True, exist_ok=True)
        for p in saved_path.parent.glob(CURRENT_DATA_PATH.stem + ".*"):
            if p != saved_path:
                try:
                    p.unlink()
                except Exception:
                    pass
        try:
            working_df.to_csv(saved_path, index=False)
            store.reload(saved_path)
            CURRENT_DATA_PATH_CACHE.parent.mkdir(parents=True, exist_ok=True)
            CURRENT_DATA_PATH_CACHE.write_text(str(saved_path))
        except Exception as exc:
            flash(f"Error saving merged data: {exc}", "danger")
            return redirect(url_for("main.summary_page"))

        total = len(store.data.df)
        total_added = total - rows_before_all

        # Record each sheet in the upload log (shown in Settings).
        for pf in per_file:
            try:
                pipeline.record_upload("data_sheet", pf["filename"], {
                    "rows_in_file": pf["rows_in_file"],
                    "rows_added": pf["rows_added"],
                    "total_rows": total,
                    "mode": "replace" if replace else "append",
                })
            except Exception:
                pass

        # User-facing summary
        if len(files) == 1:
            pf = per_file[0]
            if has_existing and pf["rows_added"] > 0:
                flash(f"Data merged: {pf['rows_added']:,} new records added from "
                      f"\"{pf['filename']}\" ({total:,} total).", "success")
            elif has_existing:
                flash(f"No new records added — \"{pf['filename']}\" fully overlaps "
                      f"existing data ({total:,} total).", "info")
            else:
                flash(f"Data loaded: {total:,} records from \"{pf['filename']}\".", "success")
        elif has_existing:
            flash(f"{len(files)} sheets merged: {total_added:,} new records added "
                  f"({total:,} total).", "success")
        else:
            flash(f"{len(files)} sheets loaded: {total:,} total records.", "success")

        try:
            cfg = config_svc.load_contract()
            if cfg.get("contract", {}).get("contract_start_date"):
                return redirect(url_for("forecast.snapshot_generating_page"))
        except Exception:
            pass

        return redirect(url_for("main.summary_page"))
