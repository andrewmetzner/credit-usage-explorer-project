from __future__ import annotations

import json
from urllib.parse import urlencode

import pandas as pd
from flask import Blueprint, render_template, request

from app.shared.config_service import AppConfig
from app.shared.data_store import CreditUsageData, DataStore
from app.shared.ingestion import IngestionPipeline
from .service import aggregate_by_period, aggregate_by_period_fmt, aggregate_by_week, agg_credits


def create_analytics_blueprint(
    store: DataStore,
    pipeline: IngestionPipeline,
    config_svc: AppConfig,
) -> Blueprint:
    bp = Blueprint("analytics", __name__, template_folder="templates", url_prefix="")

    def data() -> CreditUsageData:
        return store.data

    @bp.route("/tiers", methods=["GET"])
    def tiers_page() -> str:
        d = data()
        active_tab = request.args.get("active_tab", "users")
        usage_type_filter = request.args.get("usage_type_filter", "")
        model_filter = request.args.get("model_filter", "")
        start_date = request.args.get("start_date", "")
        end_date = request.args.get("end_date", "")
        top_n = int(request.args.get("top_n", 25))
        min_credits = request.args.get("min_credits", "").strip()
        max_credits = request.args.get("max_credits", "").strip()
        zero_credits = request.args.get("zero_credits", "")

        df = d.df.copy()
        df = d.filter_by_date(df, start_date, end_date)

        if usage_type_filter and "usage_type_parsed_type" in df.columns:
            df = df[df["usage_type_parsed_type"] == usage_type_filter]
        if model_filter and "usage_type_model" in df.columns:
            df = df[df["usage_type_model"] == model_filter]

        df = d.filter_by_credits(df, min_credits, max_credits, zero_credits)

        all_usage_types = (
            sorted(d.df["usage_type_parsed_type"].dropna().unique().tolist())
            if "usage_type_parsed_type" in d.df.columns else []
        )
        all_models = (
            sorted(d.df["usage_type_model"].dropna().unique().tolist())
            if "usage_type_model" in d.df.columns else []
        )

        def with_tokens(frame: pd.DataFrame) -> pd.DataFrame:
            frame = frame.copy()
            frame["tokens_qty"] = (
                frame["usage_quantity"].where(frame["usage_units"] == "tokens", 0.0)
                if ("usage_units" in frame.columns and "usage_quantity" in frame.columns)
                else 0.0
            )
            return frame

        lb_users = []
        group_cols = [c for c in ["name", "email"] if c in df.columns]
        if group_cols:
            dft = with_tokens(df)
            agg = (
                dft.groupby(group_cols)
                .agg(
                    rows=("usage_credits", "count"),
                    total_credits=("usage_credits", "sum"),
                    total_tokens=("tokens_qty", "sum"),
                )
                .reset_index()
                .sort_values("total_credits", ascending=False)
                .head(top_n)
            )
            lb_users = agg.to_dict(orient="records")

        lb_users_by_type = []
        type_group = [c for c in ["name", "email", "usage_type_parsed_type"] if c in df.columns]
        if len(type_group) >= 2:
            agg = (
                df.groupby(type_group)
                .agg(rows=("usage_credits", "count"), total_credits=("usage_credits", "sum"))
                .reset_index()
                .sort_values("total_credits", ascending=False)
                .head(top_n)
            )
            lb_users_by_type = agg.to_dict(orient="records")

        lb_models = []
        if "usage_type_model" in df.columns:
            agg_dict: dict = {"rows": ("usage_credits", "count"), "total_credits": ("usage_credits", "sum")}
            if "email" in df.columns:
                agg_dict["unique_users"] = ("email", "nunique")
            agg = (
                df.groupby("usage_type_model").agg(**agg_dict)
                .reset_index().sort_values("total_credits", ascending=False).head(top_n)
            )
            if "unique_users" not in agg.columns:
                agg["unique_users"] = 0
            lb_models = agg.to_dict(orient="records")

        lb_usage_types = []
        if "usage_type_parsed_type" in df.columns:
            agg_dict = {"rows": ("usage_credits", "count"), "total_credits": ("usage_credits", "sum")}
            if "email" in df.columns:
                agg_dict["unique_users"] = ("email", "nunique")
            agg = (
                df.groupby("usage_type_parsed_type").agg(**agg_dict)
                .reset_index().sort_values("total_credits", ascending=False)
            )
            if "unique_users" not in agg.columns:
                agg["unique_users"] = 0
            lb_usage_types = agg.to_dict(orient="records")

        lb_biggest_single = []
        if "usage_credits" in df.columns:
            single_cols = [
                c for c in [
                    "name", "email", "usage_credits", "usage_type_parsed_type",
                    "usage_type_model", "usage_type_io", "usage_quantity",
                    "usage_units", "date_partition", "usage_type",
                ]
                if c in df.columns
            ]
            lb_biggest_single = (
                df[single_cols].sort_values("usage_credits", ascending=False).head(top_n)
                .to_dict(orient="records")
            )

        lb_daily = aggregate_by_period(df, "date_partition", top_n)
        lb_weekly = aggregate_by_week(df, top_n)
        lb_monthly = aggregate_by_period_fmt(df, "M", "%Y-%m", "month", top_n)
        lb_yearly = aggregate_by_period_fmt(df, "Y", "%Y", "year", top_n)

        base_params = {k: v for k, v in {
            "usage_type_filter": usage_type_filter,
            "model_filter": model_filter,
            "start_date": start_date,
            "end_date": end_date,
            "top_n": top_n if top_n != 25 else "",
            "min_credits": min_credits,
            "max_credits": max_credits,
            "zero_credits": zero_credits,
        }.items() if v}

        return render_template(
            "tiers.html",
            active_tab=active_tab,
            usage_type_filter=usage_type_filter,
            model_filter=model_filter,
            start_date=start_date,
            end_date=end_date,
            top_n=top_n,
            all_usage_types=all_usage_types,
            all_models=all_models,
            lb_users=lb_users,
            lb_users_by_type=lb_users_by_type,
            lb_models=lb_models,
            lb_usage_types=lb_usage_types,
            lb_biggest_single=lb_biggest_single,
            lb_daily=lb_daily,
            lb_weekly=lb_weekly,
            lb_monthly=lb_monthly,
            lb_yearly=lb_yearly,
            base_query=urlencode(base_params),
            min_credits=min_credits,
            max_credits=max_credits,
            zero_credits=zero_credits,
        )

    @bp.route("/user-summary", methods=["GET"])
    def user_summary() -> str:
        d = data()
        name = request.args.get("name", "")
        email = request.args.get("email", "")
        date_field = request.args.get("date_field", "date_partition")
        start_date = request.args.get("start_date", "")
        end_date = request.args.get("end_date", "")
        sort_by = request.args.get("sort_by", "")
        sort_order = request.args.get("sort_order", "asc")
        active_tab = request.args.get("active_tab", "overview")
        is_form_submission = bool(request.args.get("fs", ""))
        models_explicitly_set = bool(request.args.get("mfs", ""))

        df = d.df.copy()
        if name and "name" in df.columns:
            df = df[df["name"].astype(str).str.contains(name.strip(), case=False, na=False, regex=False)]
        if email and "email" in df.columns:
            df = df[df["email"].astype(str).str.contains(email.strip(), case=False, na=False, regex=False)]
        if date_field and (start_date or end_date):
            df = d.filter_by_date(df, start_date, end_date, col=date_field)

        user_types = (
            sorted(df["usage_type_parsed_type"].dropna().unique().tolist())
            if "usage_type_parsed_type" in df.columns else []
        )
        requested_types = request.args.getlist("filter_types")
        filter_types = user_types if not is_form_submission else (requested_types or user_types)
        if "usage_type_parsed_type" in df.columns:
            df = df[df["usage_type_parsed_type"].isin(filter_types)]

        user_models = (
            sorted(df["usage_type_model"].dropna().unique().tolist())
            if "usage_type_model" in df.columns else []
        )
        requested_models = request.args.getlist("filter_models")
        if not is_form_submission:
            filter_models = user_models
        elif not models_explicitly_set:
            filter_models = user_models
        else:
            valid = [m for m in requested_models if m in user_models]
            filter_models = valid if (not requested_models or valid) else user_models
        if "usage_type_model" in df.columns:
            df = df[df["usage_type_model"].isin(filter_models)]

        if sort_by and sort_by in df.columns:
            df = df.sort_values(by=sort_by, ascending=(sort_order == "asc"))

        hidden_by_default = {"name", "email", "account_id", "account_user_id", "public_id"}
        selected_fields = request.args.getlist("selected_fields")
        if not selected_fields:
            selected_fields = [col for col in df.columns if col not in hidden_by_default]
            display_columns = selected_fields
        else:
            display_columns = [c for c in selected_fields if c in df.columns]

        rows_data = df[display_columns].fillna("").to_dict(orient="records") if display_columns else []
        total_credits = float(df["usage_credits"].sum()) if "usage_credits" in df.columns else 0.0

        totals_by_unit: dict = {}
        if "usage_units" in df.columns and "usage_quantity" in df.columns:
            for unit in ["tokens", "counts", "duration_s"]:
                totals_by_unit[unit] = float(
                    df.loc[df["usage_units"] == unit, "usage_quantity"].sum()
                )

        def make_summary(group_col: str) -> list[dict]:
            if group_col not in df.columns or len(df) == 0 or "usage_credits" not in df.columns:
                return []
            result = df.groupby(group_col, as_index=False).agg(
                total_credits=("usage_credits", "sum")
            )
            result["rows"] = df.groupby(group_col).size().values
            return result.sort_values("total_credits", ascending=False).to_dict("records")

        first_row = df.iloc[0].fillna("").to_dict() if len(df) > 0 else {}
        hidden_columns = {"account_id", "account_user_id", "public_id", "name", "email"}
        display_headers = [col for col in d.columns if col not in hidden_columns]

        summ_usage_type = make_summary("usage_type_parsed_type")
        summ_model = make_summary("usage_type_model")
        summ_io = make_summary("usage_type_io")
        summ_raw = make_summary("usage_type")

        user_weekly_json = "[]"
        if "date_partition" in df.columns and "usage_credits" in df.columns:
            wdf = df[["date_partition", "usage_credits"]].copy()
            wdf["_d"] = pd.to_datetime(wdf["date_partition"], errors="coerce")
            wdf = wdf.dropna(subset=["_d"])
            if not wdf.empty:
                wdf["_w"] = wdf["_d"] - pd.to_timedelta(wdf["_d"].dt.dayofweek, unit="D")
                agg = wdf.groupby("_w", as_index=False).agg(credits=("usage_credits", "sum")).sort_values("_w")
                user_weekly_json = json.dumps([
                    {"week": str(r["_w"].date()), "credits": round(float(r["credits"]), 2)}
                    for _, r in agg.iterrows()
                ])

        type_chart_json = json.dumps([
            {"label": s.get("usage_type_parsed_type") or "Other",
             "value": round(float(s["total_credits"]), 2)}
            for s in summ_usage_type
            if float(s.get("total_credits", 0)) > 0
        ])

        return render_template(
            "user_summary.html",
            name=name,
            email=email,
            rows=len(df),
            total_credits=total_credits,
            totals_by_unit=totals_by_unit,
            rows_data=rows_data,
            display_columns=display_columns,
            display_headers=display_headers,
            headers=display_headers,
            date_field=date_field,
            start_date=start_date,
            end_date=end_date,
            selected_fields=selected_fields,
            summary_usage_type=summ_usage_type,
            summary_model=summ_model,
            summary_io=summ_io,
            summary_raw=summ_raw,
            user_models=user_models,
            filter_models=filter_models,
            user_types=user_types,
            filter_types=filter_types,
            active_tab=active_tab,
            account_id=first_row.get("account_id", ""),
            account_user_id=first_row.get("account_user_id", ""),
            public_id=first_row.get("public_id", ""),
            sort_by=sort_by,
            sort_order=sort_order,
            is_form_submission=is_form_submission,
            user_weekly_json=user_weekly_json,
            type_chart_json=type_chart_json,
        )

    @bp.route("/user-cards", methods=["GET"])
    def user_cards_page() -> str:
        d = data()
        name_query = request.args.get("name_query", "").strip()
        email_query = request.args.get("email_query", "").strip()
        date_field = request.args.get("date_field", "")
        start_date = request.args.get("start_date", "")
        end_date = request.args.get("end_date", "")
        top_n = int(request.args.get("top_n", 50))
        min_credits = request.args.get("min_credits", "").strip()
        max_credits = request.args.get("max_credits", "").strip()
        zero_credits = request.args.get("zero_credits", "")

        df = d.df.copy()
        if name_query and "name" in df.columns:
            df = df[df["name"].astype(str).str.contains(name_query, case=False, na=False, regex=False)]
        if email_query and "email" in df.columns:
            df = df[df["email"].astype(str).str.contains(email_query, case=False, na=False, regex=False)]
        if date_field and (start_date or end_date):
            df = d.filter_by_date(df, start_date, end_date, col=date_field)
        df = d.filter_by_credits(df, min_credits, max_credits, zero_credits)

        group_cols = [c for c in ["name", "email"] if c in df.columns]
        user_list = []
        if group_cols:
            df = df.copy()
            if "usage_units" in df.columns and "usage_quantity" in df.columns:
                df["tokens_qty"] = df["usage_quantity"].where(df["usage_units"] == "tokens", 0.0)
                df["counts_qty"] = df["usage_quantity"].where(df["usage_units"] == "counts", 0.0)
                df["duration_qty"] = df["usage_quantity"].where(df["usage_units"] == "duration_s", 0.0)
            else:
                df["tokens_qty"] = df["counts_qty"] = df["duration_qty"] = 0.0
            agg = (
                df.groupby(group_cols)
                .agg(
                    rows=("usage_credits", "count"),
                    total_credits=("usage_credits", "sum"),
                    total_quantity=("usage_quantity", "sum"),
                    total_tokens=("tokens_qty", "sum"),
                    total_counts=("counts_qty", "sum"),
                    total_duration_s=("duration_qty", "sum"),
                )
                .reset_index()
                .sort_values("total_credits", ascending=False)
                .head(top_n)
            )
            user_list = agg.to_dict(orient="records")

        return render_template(
            "user_cards.html",
            headers=d.columns,
            name_query=name_query,
            email_query=email_query,
            date_field=date_field,
            start_date=start_date,
            end_date=end_date,
            top_n=top_n,
            users=user_list,
            min_credits=min_credits,
            max_credits=max_credits,
            zero_credits=zero_credits,
        )

    return bp
