from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
from flask import Blueprint, flash, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from app.shared.config_service import AppConfig
from app.shared.data_merge import merge_usage_data
from app.shared.data_store import CreditUsageData, DataStore
from app.shared.ingestion import IngestionPipeline
from app.forecast.service import ChartDataBuilder, ForecastingService
from .service import compute_outlier_users, compute_summary_metrics, compute_weekly_trend

DERIVED_COLS = {
    "usage_type_parsed_type", "usage_type_model", "usage_type_date",
    "usage_type_medium", "usage_type_io",
}


def create_dashboard_blueprint(
    store: DataStore,
    pipeline: IngestionPipeline,
    config_svc: AppConfig,
) -> Blueprint:
    bp = Blueprint("main", __name__, template_folder="templates")

    def data() -> CreditUsageData:
        return store.data

    @bp.route("/", methods=["GET"])
    def index() -> str:
        return redirect(url_for("main.summary_page"))

    @bp.route("/summary", methods=["GET"])
    def summary_page() -> str:
        from config import DEFAULT_DATA_PATH

        d = data()
        df = d.df

        credit_threshold = float(request.args.get("credit_threshold", 100) or 100)
        model_filter = request.args.get("model_filter", "").strip()
        lookback_days = int(request.args.get("lookback_days", 7) or 7)

        metrics = compute_summary_metrics(df)
        all_models_list: list[str] = (
            sorted(df["usage_type_model"].dropna().unique().tolist())
            if "usage_type_model" in df.columns else []
        )
        outlier_users, outlier_count, lookback_start_date, lookback_end_date = compute_outlier_users(
            df, credit_threshold, lookback_days, model_filter
        )
        weekly_trend = compute_weekly_trend(df)

        forecast_snapshot = None
        active_users_data = "[]"
        ps = pipeline.status()
        try:
            config = config_svc.load_contract()
            hist_df = pipeline.get_historical_weekly_summary()
            op_df = pipeline.get_operational_weekly_summary()
            daily_fallback = d.df if (hist_df is None and op_df is None) else None
            svc = ForecastingService(config, hist_df, op_df, daily_fallback)
            if svc.has_data():
                cs = svc.get_contract_status()
                fc = svc.get_forecast()
                forecast_snapshot = {
                    "pacing_status": cs["pacing_status"],
                    "burn_pace_ratio": cs["burn_pace_ratio"],
                    "credits_remaining": cs["credits_remaining"],
                    "percent_credits_used": cs["percent_credits_used"],
                    "percent_contract_elapsed": cs["percent_contract_elapsed"],
                    "weeks_remaining": cs["weeks_remaining"],
                    "forecast_status": fc["forecast_status"],
                    "forecast_weekly_burn": fc["forecast_weekly_burn"],
                    "forecast_contract_end_balance": fc["forecast_contract_end_balance"],
                }
                chart_builder = ChartDataBuilder(svc, svc.historical_df, svc.operational_df)
                contract_start_str = str(cs.get("contract_start_date", ""))
                active_users_data = chart_builder.active_users_json(contract_start_str)
        except Exception:
            pass

        return render_template(
            "summary.html",
            metrics=metrics,
            outlier_users=outlier_users,
            outlier_count=outlier_count,
            credit_threshold=credit_threshold,
            model_filter=model_filter,
            lookback_days=lookback_days,
            lookback_start_date=lookback_start_date,
            lookback_end_date=lookback_end_date,
            all_models_list=all_models_list,
            weekly_trend=weekly_trend,
            forecast_snapshot=forecast_snapshot,
            pipeline_status=ps,
            data_source={
                "filename": None if store.path == DEFAULT_DATA_PATH else store.path.name,
                "rows": metrics["total_records"],
            },
            active_users_data=active_users_data,
        )

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
            auto_save_mode = cfg.get("forecast", {}).get("snapshot_auto_save", "daily")
            if auto_save_mode in ("on_upload", "both"):
                hist_df = pipeline.get_historical_weekly_summary()
                op_df = pipeline.get_operational_weekly_summary()
                daily_fallback = store.data.df if (hist_df is None and op_df is None) else None
                svc = ForecastingService(cfg, hist_df, op_df, daily_fallback)
                if svc.has_data():
                    label = (f"Upload: {per_file[0]['filename']}" if len(files) == 1
                             else f"Upload: {len(files)} sheets")
                    svc.save_to_dir(pipeline.processed_dir, once_per_day=False, label=label)
        except Exception:
            pass

        return redirect(url_for("main.summary_page"))

    @bp.route("/records", methods=["GET"])
    def records_page() -> str:
        d = data()
        search_field = request.args.get("search_field", "any")
        search_query = request.args.get("search_query", "").strip()
        start_date = request.args.get("start_date", "")
        end_date = request.args.get("end_date", "")
        min_credits = request.args.get("min_credits", "").strip()
        max_credits = request.args.get("max_credits", "").strip()
        zero_credits = request.args.get("zero_credits", "")

        selected_fields_param = request.args.getlist("selected_fields")
        hidden_by_default = {"account_user_id", "account_id", "public_id"}
        selected_fields = (
            set(selected_fields_param) if selected_fields_param
            else set(d.columns) - hidden_by_default
        )

        df = d.df.copy()
        df = d.filter_by_date(df, start_date, end_date)
        df = d.filter_by_credits(df, min_credits, max_credits, zero_credits)

        if search_query:
            if search_field == "any":
                mask = pd.Series(False, index=df.index)
                for col in df.columns:
                    mask |= df[col].astype(str).str.contains(
                        search_query, case=False, na=False, regex=False
                    )
                df = df[mask]
            elif search_field in df.columns:
                df = df[
                    df[search_field].astype(str).str.contains(
                        search_query, case=False, na=False, regex=False
                    )
                ]

        display_columns = [col for col in d.columns if col in selected_fields]
        if not display_columns:
            display_columns = [col for col in d.columns if col not in hidden_by_default]

        rows = [
            {col: row.get(col) for col in display_columns}
            for row in df.to_dict(orient="records")
        ]

        return render_template(
            "index.html",
            headers=d.columns,
            display_columns=display_columns,
            search_field=search_field,
            search_query=search_query,
            start_date=start_date,
            end_date=end_date,
            rows=rows,
            row_count=len(df),
            selected_fields=selected_fields,
            min_credits=min_credits,
            max_credits=max_credits,
            zero_credits=zero_credits,
        )

    return bp
