"""Dashboard blueprint (`main`): core pages + registration of the concern modules.

The setup wizard, diagnostics, outliers/alerts, and upload routes live in their
own modules and register onto this same `main` blueprint, so every existing
`url_for("main.*")` reference stays valid while each concern is editable in
isolation.
"""
from __future__ import annotations

import pandas as pd
from flask import Blueprint, redirect, render_template, request, url_for

from app.shared.chart_data import usage_type_weekly_json
from app.shared.data_store import CreditUsageData
from app.forecast.service import ChartDataBuilder
from .service import compute_summary_metrics, compute_weekly_trend
from .setup_routes import register_setup_routes
from .diagnostics_routes import register_diagnostics_routes
from .outliers_routes import register_outliers_routes
from .upload_routes import register_upload_routes


def create_dashboard_blueprint(services) -> Blueprint:
    store = services.store
    pipeline = services.pipeline
    config_svc = services.config_svc
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

        metrics = compute_summary_metrics(df)
        weekly_trend = compute_weekly_trend(df)

        forecast_snapshot = None
        active_users_data = "[]"
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
                chart_builder = ChartDataBuilder(svc, svc.historical_df, svc.operational_df)
                contract_start_str = str(cs.get("contract_start_date", ""))
                active_users_data = chart_builder.active_users_json(contract_start_str)
        except Exception:
            pass

        return render_template(
            "summary.html",
            metrics=metrics,
            weekly_trend=weekly_trend,
            usage_type_weekly=usage_type_weekly_json(df),
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

    # ── Concern modules register their routes onto this same blueprint ──
    register_setup_routes(bp, services)
    register_diagnostics_routes(bp, services)
    register_outliers_routes(bp, services)
    register_upload_routes(bp, services)

    return bp
