from __future__ import annotations

import json
from io import BytesIO
from urllib.parse import urlencode

import pandas as pd
from flask import Blueprint, current_app, render_template, request, send_file

from app.shared.chart_data import usage_type_weekly_json
from app.shared.data_store import CreditUsageData
from app.shared.outliers import OUTLIER_VIEWS, compute_outliers
from .service import Leaderboards


def create_analytics_blueprint(services) -> Blueprint:
    store = services.store
    config_svc = services.config_svc
    bp = Blueprint("analytics", __name__, template_folder="templates", url_prefix="")
    max_result_limit = 10_000

    def data() -> CreditUsageData:
        return store.data

    def result_limit(name: str, default: int, min_value: int = 1) -> int:
        try:
            value = int(request.args.get(name, default) or default)
        except (TypeError, ValueError):
            return default
        return max(min_value, min(value, max_result_limit))

    def _tier_caps() -> dict[str, float]:
        tiers = config_svc.load_tiers().get("tiers") or {}
        caps = {str(name): float(cfg.get("weekly_credit_cap", 0) or 0) for name, cfg in tiers.items()}
        if "Baseline" not in caps:
            caps["Baseline"] = min(caps.values()) if caps else 100.0
        return caps

    def _next_tier(current: str, caps: dict[str, float], direction: int) -> tuple[str, float]:
        ordered = sorted(caps.items(), key=lambda item: item[1])
        names = [name for name, _ in ordered]
        if current not in names:
            current = "Baseline"
        idx = names.index(current)
        idx = max(0, min(len(ordered) - 1, idx + direction))
        return ordered[idx]

    def _pressure_flag(utilization: float) -> str:
        if utilization >= 1.10:
            return "ABOVE_CAP_110_PLUS"
        if utilization >= 1.00:
            return "AT_OR_ABOVE_CAP"
        if utilization >= 0.90:
            return "HIGH_PRESSURE_90_PLUS"
        if utilization >= 0.80:
            return "ELEVATED_PRESSURE_80_PLUS"
        return "NORMAL"

    def _optimization_for_user(records_df: pd.DataFrame, name: str, email: str) -> tuple[dict | None, list[dict], str]:
        required = {"email", "date_partition", "usage_credits"}
        if records_df is None or records_df.empty or not required.issubset(records_df.columns):
            return None, [], ""

        user_df = records_df.copy()
        if email:
            user_df = user_df[user_df["email"].astype(str).str.lower() == email.strip().lower()]
        elif name and "name" in user_df.columns:
            user_df = user_df[user_df["name"].astype(str).str.contains(name.strip(), case=False, na=False, regex=False)]
        else:
            return None, [], ""
        if user_df.empty:
            return None, [], "current project records"

        caps = _tier_caps()
        current_tier = "Baseline"
        current_cap = caps.get(current_tier, min(caps.values()) if caps else 100.0) or 1.0

        wdf = user_df.copy()
        wdf["_date"] = pd.to_datetime(wdf["date_partition"], errors="coerce")
        wdf = wdf.dropna(subset=["_date"])
        if wdf.empty:
            return None, [], "current project records"
        wdf["week_start"] = wdf["_date"] - pd.to_timedelta(wdf["_date"].dt.dayofweek, unit="D")
        wdf["credits_used"] = pd.to_numeric(wdf["usage_credits"], errors="coerce").fillna(0)
        weekly = (
            wdf.groupby("week_start", as_index=False)
            .agg(credits_used=("credits_used", "sum"))
            .sort_values("week_start")
        )
        weekly["week_end"] = weekly["week_start"] + pd.Timedelta(days=6)
        weekly["governance_tier"] = current_tier
        weekly["weekly_credit_cap"] = current_cap
        weekly["cap_utilization"] = weekly["credits_used"] / current_cap
        weekly["pressure_flag"] = weekly["cap_utilization"].apply(_pressure_flag)

        weeks_observed = int(weekly["week_start"].nunique())
        latest_util = float(weekly["cap_utilization"].iloc[-1])
        first_util = float(weekly["cap_utilization"].iloc[0])
        avg_util = float(weekly["cap_utilization"].mean())
        share_over_90 = float((weekly["cap_utilization"] >= 0.90).sum() / max(weeks_observed, 1))

        if weeks_observed < 2:
            action = "MONITOR_MORE_HISTORY_NEEDED"
        elif weeks_observed >= 3 and share_over_90 >= 0.50:
            action = "CONSIDER_MOVE_UP_TIER"
        elif weeks_observed >= 4 and avg_util <= 0.25 and latest_util <= 0.25:
            action = "CONSIDER_MOVE_DOWN_TIER"
        elif latest_util >= 0.90:
            action = "MONITOR_RECENT_SPIKE"
        else:
            action = "NO_CHANGE"

        direction = 1 if action == "CONSIDER_MOVE_UP_TIER" else (-1 if action == "CONSIDER_MOVE_DOWN_TIER" else 0)
        recommended_tier, recommended_cap = _next_tier(current_tier, caps, direction)
        if latest_util - first_util >= 0.20:
            trend = "INCREASING_PRESSURE"
        elif latest_util - first_util <= -0.20:
            trend = "DECREASING_PRESSURE"
        else:
            trend = "STABLE_PRESSURE"

        priority = {
            "CONSIDER_MOVE_UP_TIER": "ACTIONABLE",
            "CONSIDER_MOVE_DOWN_TIER": "ACTIONABLE",
            "MONITOR_RECENT_SPIKE": "MONITOR",
            "MONITOR_MORE_HISTORY_NEEDED": "MONITOR",
            "NO_CHANGE": "INFORMATIONAL",
        }.get(action, "INFORMATIONAL")
        recommendation = {
            "recommended_action": action,
            "review_priority": priority,
            "latest_governance_tier": current_tier,
            "recommended_tier": recommended_tier,
            "latest_weekly_credit_cap": current_cap,
            "recommended_weekly_credit_cap": recommended_cap,
            "latest_cap_utilization": latest_util,
            "pressure_trend": trend,
            "avg_weekly_credits_used": float(weekly["credits_used"].mean()),
            "recommended_cap_change": recommended_cap - current_cap,
        }
        history = weekly.sort_values("week_start", ascending=False).head(12).copy()
        history["week_start"] = history["week_start"].dt.date.astype(str)
        history["week_end"] = history["week_end"].dt.date.astype(str)
        return recommendation, history.to_dict(orient="records"), "current project records"

    @bp.route("/leaderboard", methods=["GET"])
    def leaderboard_page() -> str:
        d = data()
        active_tab = request.args.get("active_tab", "users")
        usage_type_filter = request.args.get("usage_type_filter", "")
        model_filter = request.args.get("model_filter", "")
        start_date = request.args.get("start_date", "")
        end_date = request.args.get("end_date", "")
        top_n = result_limit("top_n", 25, 5)
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

        lb = Leaderboards(df, top_n)
        lb_users = lb.by_user()
        lb_users_by_type = lb.by_user_type()
        lb_models = lb.by_model()
        lb_usage_types = lb.by_usage_type()
        lb_biggest_single = lb.biggest_single()
        lb_daily = lb.daily()
        lb_weekly = lb.weekly()
        lb_monthly = lb.monthly()
        lb_yearly = lb.yearly()

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
            "leaderboard.html",
            active_tab=active_tab,
            usage_type_filter=usage_type_filter,
            model_filter=model_filter,
            start_date=start_date,
            end_date=end_date,
            top_n=top_n,
            max_result_limit=max_result_limit,
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

        optimization_user, optimization_history, optimization_source = _optimization_for_user(d.df, name, email)
        optimization_page_available = "optimization.optimization_page" in current_app.view_functions

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
            user_usage_type_weekly=usage_type_weekly_json(df),
            type_chart_json=type_chart_json,
            optimization_user=optimization_user,
            optimization_history=optimization_history,
            optimization_source=optimization_source,
            optimization_page_available=optimization_page_available,
        )

    @bp.route("/user-cards", methods=["GET"])
    def user_cards_page() -> str:
        d = data()
        mode = "advanced" if request.args.get("mode", "basic").strip() == "advanced" else "basic"
        view = request.args.get("view", "cards").strip()
        if view not in {"cards", "table", "list"}:
            view = "cards"
        name_query = request.args.get("name_query", "").strip()
        email_query = request.args.get("email_query", "").strip()
        # Default to the canonical usage date so a plain date-range search works
        # without forcing the user to choose a field first.
        date_field = request.args.get("date_field", "date_partition")
        start_date = request.args.get("start_date", "")
        end_date = request.args.get("end_date", "")
        top_n = result_limit("top_n", 50)
        min_credits = request.args.get("min_credits", "").strip()
        max_credits = request.args.get("max_credits", "").strip()
        zero_credits = request.args.get("zero_credits", "")

        # The name/email search applies in both modes.
        df = d.df.copy()
        if name_query and "name" in df.columns:
            df = df[df["name"].astype(str).str.contains(name_query, case=False, na=False, regex=False)]
        if email_query and "email" in df.columns:
            df = df[df["email"].astype(str).str.contains(email_query, case=False, na=False, regex=False)]

        # Advanced (outlier) controls + result.
        metric = request.args.get("metric", "per_user_window").strip()
        if metric not in OUTLIER_VIEWS:
            metric = "per_user_window"
        credit_threshold = float(request.args.get("credit_threshold", 100) or 100)
        lookback_days = int(request.args.get("lookback_days", 7) or 7)
        adv_usage_type = request.args.get("usage_type_filter", "").strip()
        adv_model = request.args.get("model_filter", "").strip()

        all_types_list = (
            sorted(d.df["usage_type_parsed_type"].dropna().unique().tolist())
            if "usage_type_parsed_type" in d.df.columns else []
        )
        all_models_list = (
            sorted(d.df["usage_type_model"].dropna().unique().tolist())
            if "usage_type_model" in d.df.columns else []
        )

        user_list: list[dict] = []
        outlier_rows: list[dict] = []
        outlier_count = 0
        outlier_columns: list[dict] = []
        window_start = window_end = ""

        if mode == "advanced":
            outlier_rows, outlier_count, window_start, window_end, outlier_columns = compute_outliers(
                df, metric, credit_threshold, lookback_days,
                start_date=start_date, end_date=end_date,
                usage_type_filter=adv_usage_type, model_filter=adv_model,
                top_n=top_n,
            )
        else:
            if date_field and (start_date or end_date):
                df = d.filter_by_date(df, start_date, end_date, col=date_field)
            df = d.filter_by_credits(df, min_credits, max_credits, zero_credits)
            group_cols = [c for c in ["name", "email"] if c in df.columns]
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

        # Query string (minus `view`) so the cards/table/list toggle can switch
        # the view while preserving the active search/filters.
        base_params = {k: v for k, v in {
            "mode": "basic",
            "name_query": name_query,
            "email_query": email_query,
            "date_field": date_field,
            "start_date": start_date,
            "end_date": end_date,
            "top_n": top_n if top_n != 50 else "",
            "min_credits": min_credits,
            "max_credits": max_credits,
            "zero_credits": zero_credits,
        }.items() if v}

        return render_template(
            "user_cards.html",
            headers=d.columns,
            mode=mode,
            view=view,
            base_query=urlencode(base_params),
            name_query=name_query,
            email_query=email_query,
            date_field=date_field,
            start_date=start_date,
            end_date=end_date,
            top_n=top_n,
            max_result_limit=max_result_limit,
            users=user_list,
            min_credits=min_credits,
            max_credits=max_credits,
            zero_credits=zero_credits,
            # advanced mode
            metric=metric,
            outlier_views=OUTLIER_VIEWS,
            outlier_rows=outlier_rows,
            outlier_columns=outlier_columns,
            outlier_count=outlier_count,
            credit_threshold=credit_threshold,
            lookback_days=lookback_days,
            usage_type_filter=adv_usage_type,
            model_filter=adv_model,
            all_types_list=all_types_list,
            all_models_list=all_models_list,
            window_start=window_start,
            window_end=window_end,
        )

    @bp.route("/user-cards/export", methods=["GET"])
    def export_outliers() -> object:
        """Download the current advanced-search outlier result as an .xlsx,
        for pasting into emails/discussions. Mirrors user_cards_page's advanced
        params so the export matches what's on screen.
        """
        d = data()
        name_query = request.args.get("name_query", "").strip()
        email_query = request.args.get("email_query", "").strip()
        metric = request.args.get("metric", "per_user_window").strip()
        if metric not in OUTLIER_VIEWS:
            metric = "per_user_window"
        credit_threshold = float(request.args.get("credit_threshold", 100) or 100)
        lookback_days = int(request.args.get("lookback_days", 7) or 7)
        top_n = result_limit("top_n", 200)
        adv_usage_type = request.args.get("usage_type_filter", "").strip()
        adv_model = request.args.get("model_filter", "").strip()
        start_date = request.args.get("start_date", "")
        end_date = request.args.get("end_date", "")

        df = d.df.copy()
        if name_query and "name" in df.columns:
            df = df[df["name"].astype(str).str.contains(name_query, case=False, na=False, regex=False)]
        if email_query and "email" in df.columns:
            df = df[df["email"].astype(str).str.contains(email_query, case=False, na=False, regex=False)]

        rows, count, win_start, win_end, columns = compute_outliers(
            df, metric, credit_threshold, lookback_days,
            start_date=start_date, end_date=end_date,
            usage_type_filter=adv_usage_type, model_filter=adv_model,
            top_n=top_n,
        )

        # Labeled DataFrame in the view's column order.
        labels = [c["label"] for c in columns]
        export_df = pd.DataFrame(
            [{c["label"]: r.get(c["key"]) for c in columns} for r in rows],
            columns=labels,
        )

        bio = BytesIO()
        with pd.ExcelWriter(bio, engine="openpyxl") as writer:
            export_df.to_excel(writer, index=False, sheet_name="Outliers")
        bio.seek(0)
        fname = f"outliers_{metric}_{win_start}_to_{win_end}.xlsx"
        return send_file(
            bio, as_attachment=True, download_name=fname,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    return bp
