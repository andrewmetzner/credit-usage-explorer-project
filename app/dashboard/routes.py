"""Dashboard blueprint (`main`): core pages + registration of the concern modules.

The setup wizard, diagnostics, outliers/alerts, and upload routes live in their
own modules and register onto this same `main` blueprint, so every existing
`url_for("main.*")` reference stays valid while each concern is editable in
isolation.
"""
from __future__ import annotations

from urllib.parse import urlencode

import pandas as pd
from flask import Blueprint, redirect, render_template, request, url_for

from app.shared.chart_data import usage_type_weekly_json
from app.shared.csv_export import csv_response
from app.shared.data_store import CreditUsageData
from .service import (
    DEFAULT_RECORD_COLUMNS,
    build_record_view,
    compute_active_users_weekly,
    compute_summary_metrics,
    compute_daily_trend,
    compute_weekly_trend,
    record_column_meta,
)
from .setup_routes import register_setup_routes
from .diagnostics_routes import register_diagnostics_routes
from .alerts_routes import register_alerts_routes
from .upload_routes import register_upload_routes


def create_dashboard_blueprint(services) -> Blueprint:
    store = services.store
    pipeline = services.pipeline
    config_svc = services.config_svc
    bp = Blueprint("main", __name__, template_folder="templates")

    def data() -> CreditUsageData:
        return store.data

    def _records_query_state(d: CreditUsageData):
        search_field = request.args.get("search_field", "any")
        search_query = request.args.get("search_query", "").strip()
        start_date = request.args.get("start_date", "")
        end_date = request.args.get("end_date", "")
        min_credits = request.args.get("min_credits", "").strip()
        max_credits = request.args.get("max_credits", "").strip()
        zero_credits = request.args.get("zero_credits", "")
        usage_type = request.args.get("usage_type", "").strip()
        model = request.args.get("model", "").strip()
        lookback_days = request.args.get("lookback_days", "").strip()
        sort_by = request.args.get("sort_by", "").strip()
        sort_order = request.args.get("sort_order", "asc").strip()
        if sort_order not in {"asc", "desc"}:
            sort_order = "asc"

        # A lookback (from an alert deep-link) resolves to an explicit window off
        # the latest data date, so the date inputs show the range being viewed.
        if lookback_days and not (start_date or end_date) and "date_partition" in d.df.columns:
            dts = pd.to_datetime(d.df["date_partition"], errors="coerce")
            if dts.notna().any() and lookback_days.isdigit():
                days = max(int(lookback_days), 1)
                start_date = str((dts.max() - pd.Timedelta(days=days - 1)).date())
                end_date = str(dts.max().date())

        selected_param = [c for c in request.args.getlist("selected_fields") if c in d.columns]
        selected_fields = (
            [c for c in d.columns if c in selected_param] if selected_param
            else [c for c in DEFAULT_RECORD_COLUMNS if c in d.columns]
        )

        df = d.df.copy()
        df = d.filter_by_date(df, start_date, end_date)
        df = d.filter_by_credits(df, min_credits, max_credits, zero_credits)
        # Usage-type filter runs on the corrected parsed type, so "codex" also
        # catches API rows that arrive labeled as chat.
        if usage_type and "usage_type_parsed_type" in df.columns:
            df = df[df["usage_type_parsed_type"] == usage_type]
        if model and "usage_type_model" in df.columns:
            df = df[df["usage_type_model"] == model]

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

        if sort_by in df.columns:
            sort_key = pd.to_numeric(df[sort_by], errors="coerce")
            if sort_key.notna().any():
                df = df.assign(_sort_key=sort_key).sort_values(
                    "_sort_key", ascending=(sort_order == "asc"), na_position="last"
                ).drop(columns="_sort_key")
            else:
                df = df.sort_values(
                    sort_by, ascending=(sort_order == "asc"), na_position="last",
                    key=lambda s: s.astype(str).str.lower()
                )

        return {
            "df": df,
            "selected_fields": selected_fields,
            "search_field": search_field,
            "search_query": search_query,
            "start_date": start_date,
            "end_date": end_date,
            "min_credits": min_credits,
            "max_credits": max_credits,
            "zero_credits": zero_credits,
            "usage_type": usage_type,
            "model": model,
            "sort_by": sort_by,
            "sort_order": sort_order,
        }

    def _query_url(endpoint: str, **overrides) -> str:
        pairs = []
        args = request.args.copy()
        for key, value in overrides.items():
            args.pop(key, None)
            if value not in (None, ""):
                if isinstance(value, (list, tuple)):
                    for item in value:
                        pairs.append((key, item))
                else:
                    pairs.append((key, value))
        for key in args:
            for value in args.getlist(key):
                if value:
                    pairs.append((key, value))
        qs = urlencode(pairs)
        return f"{url_for(endpoint)}?{qs}" if qs else url_for(endpoint)

    @bp.route("/", methods=["GET"])
    def index() -> str:
        return redirect(url_for("main.summary_page"))

    @bp.route("/summary", methods=["GET"])
    def summary_page() -> str:
        from config import DEFAULT_DATA_PATH

        d = data()
        df = d.df

        metrics = compute_summary_metrics(df)

        # Contract start drives the in-contract / pre-contract split shared by
        # the Summary charts (weekly-burn gray coloring + scope dropdowns).
        contract_start_str = ""
        try:
            contract_start_str = str(
                config_svc.load_contract().get("contract", {}).get("contract_start_date", "") or ""
            )
        except Exception:
            contract_start_str = ""

        weekly_trend = compute_weekly_trend(df, contract_start_str)
        daily_trend = compute_daily_trend(df, contract_start_str)
        # All three Summary charts share one raw-frame week grouping + contract
        # split, so they always cover the same weeks (no straddling-week gap).
        active_users_data = compute_active_users_weekly(df, contract_start_str)

        forecast_snapshot = None
        ps = pipeline.status()
        try:
            config = config_svc.load_contract()
            svc = services.build_forecasting_service(config)
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
        except Exception:
            pass

        return render_template(
            "summary.html",
            metrics=metrics,
            weekly_trend=weekly_trend,
            daily_trend=daily_trend,
            usage_type_weekly=usage_type_weekly_json(df, contract_start=contract_start_str),
            forecast_snapshot=forecast_snapshot,
            pipeline_status=ps,
            data_source={
                "filename": None if store.path == DEFAULT_DATA_PATH else store.path.name,
                "rows": metrics["total_records"],
            },
            active_users_data=active_users_data,
        )

    @bp.route("/records", methods=["GET"])
    def records_page() -> str:
        d = data()
        state = _records_query_state(d)
        df = state["df"]
        selected_fields = state["selected_fields"]
        columns, rows = build_record_view(df, selected_fields)

        def _options(col: str) -> list[str]:
            return (
                sorted(d.df[col].dropna().astype(str).unique().tolist())
                if col in d.df.columns else []
            )

        sort_urls = {}
        for c in columns:
            next_order = (
                "desc"
                if state["sort_by"] == c["key"] and state["sort_order"] == "asc"
                else "asc"
            )
            sort_urls[c["key"]] = _query_url(
                "main.records_page", sort_by=c["key"], sort_order=next_order
            )

        return render_template(
            "index.html",
            toggle_columns=[record_column_meta(c) for c in d.columns],
            columns=columns,
            rows=rows,
            row_count=len(df),
            selected_fields=set(selected_fields),
            headers=d.columns,
            search_field=state["search_field"],
            search_query=state["search_query"],
            start_date=state["start_date"],
            end_date=state["end_date"],
            min_credits=state["min_credits"],
            max_credits=state["max_credits"],
            zero_credits=state["zero_credits"],
            usage_type=state["usage_type"],
            model=state["model"],
            sort_by=state["sort_by"],
            sort_order=state["sort_order"],
            sort_urls=sort_urls,
            export_url=_query_url("main.records_export_csv"),
            usage_type_options=_options("usage_type_parsed_type"),
            model_options=_options("usage_type_model"),
        )

    @bp.route("/records/export.csv", methods=["GET"])
    def records_export_csv() -> object:
        d = data()
        state = _records_query_state(d)
        columns, rows = build_record_view(state["df"], state["selected_fields"])
        export_df = pd.DataFrame(
            [{c["label"]: row.get(c["key"]) for c in columns} for row in rows],
            columns=[c["label"] for c in columns],
        )
        date_range = (
            f"{state['start_date']}_to_{state['end_date']}"
            if state["start_date"] or state["end_date"] else ""
        )
        credit_range = (
            f"{state['min_credits'] or '0'}_to_{state['max_credits'] or 'max'}"
            if state["min_credits"] or state["max_credits"] else ""
        )
        search = (
            f"{state['search_field']}_{state['search_query']}"
            if state["search_query"] else ""
        )
        sort = f"{state['sort_by']}_{state['sort_order']}" if state["sort_by"] else ""
        return csv_response(export_df, "records.csv", filters=[
            ("type", state["usage_type"]),
            ("model", state["model"]),
            ("dates", date_range),
            ("search", search),
            ("credits", credit_range),
            ("zero", "only" if state["zero_credits"] == "1" else ""),
            ("sort", sort),
        ])

    # ── Concern modules register their routes onto this same blueprint ──
    register_setup_routes(bp, services)
    register_diagnostics_routes(bp, services)
    register_alerts_routes(bp, services)
    register_upload_routes(bp, services)

    return bp
