from __future__ import annotations

import json
from urllib.parse import urlencode

import pandas as pd
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from app.dashboard.service import DEFAULT_RECORD_COLUMNS, build_record_view, record_column_meta
from app.shared.chart_data import usage_type_weekly_json
from app.shared.csv_export import csv_response, labeled_export_df, range_slug
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

    def _tier_options(d: CreditUsageData) -> list[str]:
        return services.governance.tier_search_options(d.df)

    def _tier_search(df: pd.DataFrame, tier_query: str) -> pd.Series:
        return services.governance.tier_search_column(df, tier_query)

    def _apply_user_search_filters(df: pd.DataFrame, name_query: str, email_query: str, tier_query: str) -> pd.DataFrame:
        """The name/email/tier free-text search shared by the basic user
        aggregation, the user-cards page, and its CSV export — so the three
        stay in agreement about what "matches" means."""
        if name_query and "name" in df.columns:
            df = df[df["name"].astype(str).str.contains(name_query, case=False, na=False, regex=False)]
        if email_query and "email" in df.columns:
            df = df[df["email"].astype(str).str.contains(email_query, case=False, na=False, regex=False)]
        if tier_query and "email" in df.columns:
            df = df[_tier_search(df, tier_query)]
        return df

    def _leaderboard_filtered_df(d: CreditUsageData) -> pd.DataFrame:
        usage_type_filter = request.args.get("usage_type_filter", "")
        model_filter = request.args.get("model_filter", "")
        tier_filter = request.args.get("tier_filter", "").strip()
        tier_exact = request.args.get("tier_exact", "").strip()
        start_date = request.args.get("start_date", "")
        end_date = request.args.get("end_date", "")
        min_credits = request.args.get("min_credits", "").strip()
        max_credits = request.args.get("max_credits", "").strip()
        zero_credits = request.args.get("zero_credits", "")

        df = d.df.copy()
        df = d.filter_by_date(df, start_date, end_date)

        if usage_type_filter and "usage_type_parsed_type" in df.columns:
            df = df[df["usage_type_parsed_type"] == usage_type_filter]
        if model_filter and "usage_type_model" in df.columns:
            df = df[df["usage_type_model"] == model_filter]
        if tier_filter and "email" in df.columns:
            df = df[_tier_search(df, tier_filter)]
        if tier_exact and "email" in df.columns:
            # Exact match on each row's *current* resolved tier — the "Who's
            # in this tier" drill from the By-tier board uses this so it
            # shows exactly the people counted in that tier's total, not
            # everyone who ever held the tier (that broader search is what
            # tier_filter above is for).
            df = df[services.governance.tier_column(df) == tier_exact]

        df = d.filter_by_credits(df, min_credits, max_credits, zero_credits)
        if "email" in df.columns:
            df = df.copy()
            df["tier"] = services.governance.tier_column(df)
        return df

    def _aggregate_basic_users(d: CreditUsageData) -> pd.DataFrame:
        name_query = request.args.get("name_query", "").strip()
        email_query = request.args.get("email_query", "").strip()
        tier_query = request.args.get("tier_query", "").strip()
        date_field = request.args.get("date_field", "date_partition")
        start_date = request.args.get("start_date", "")
        end_date = request.args.get("end_date", "")
        min_credits = request.args.get("min_credits", "").strip()
        max_credits = request.args.get("max_credits", "").strip()
        zero_credits = request.args.get("zero_credits", "")
        sort_by = request.args.get("sort", "credits").strip()
        if sort_by not in {"credits", "alpha"}:
            sort_by = "credits"

        df = d.df.copy()
        df = _apply_user_search_filters(df, name_query, email_query, tier_query)
        if date_field and (start_date or end_date):
            df = d.filter_by_date(df, start_date, end_date, col=date_field)
        df = d.filter_by_credits(df, min_credits, max_credits, zero_credits)

        group_cols = [c for c in ["name", "email"] if c in df.columns]
        if not group_cols:
            return pd.DataFrame()
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
        )
        if sort_by == "alpha":
            label_col = "name" if "name" in agg.columns else "email"
            agg["_sort_key"] = agg[label_col].fillna("").astype(str).str.lower()
            if label_col == "name" and "email" in agg.columns:
                agg.loc[agg["_sort_key"] == "", "_sort_key"] = agg["email"].fillna("").astype(str).str.lower()
            agg = agg.sort_values("_sort_key").drop(columns="_sort_key")
        else:
            agg = agg.sort_values("total_credits", ascending=False)
        return agg

    @bp.route("/leaderboard", methods=["GET"])
    def leaderboard_page() -> str:
        d = data()
        active_tab = request.args.get("active_tab", "users")
        usage_type_filter = request.args.get("usage_type_filter", "")
        model_filter = request.args.get("model_filter", "")
        tier_filter = request.args.get("tier_filter", "").strip()
        tier_exact = request.args.get("tier_exact", "").strip()
        start_date = request.args.get("start_date", "")
        end_date = request.args.get("end_date", "")
        top_n = result_limit("top_n", 25, 5)
        min_credits = request.args.get("min_credits", "").strip()
        max_credits = request.args.get("max_credits", "").strip()
        zero_credits = request.args.get("zero_credits", "")

        df = _leaderboard_filtered_df(d)

        all_usage_types = (
            sorted(d.df["usage_type_parsed_type"].dropna().unique().tolist())
            if "usage_type_parsed_type" in d.df.columns else []
        )
        all_models = (
            sorted(d.df["usage_type_model"].dropna().unique().tolist())
            if "usage_type_model" in d.df.columns else []
        )
        all_tiers = _tier_options(d)

        # Tab switches are full page loads, so only the active tab's board is
        # computed; the hidden panels render from empty lists for free.
        lb = Leaderboards(df, top_n)
        board_builders = {
            "users": ("lb_users", lb.by_user),
            "users_by_type": ("lb_users_by_type", lb.by_user_type),
            "models": ("lb_models", lb.by_model),
            "usage_types": ("lb_usage_types", lb.by_usage_type),
            "tiers": ("lb_tiers", lb.by_tier),
            "biggest_single": ("lb_biggest_single", lb.biggest_single),
            "daily": ("lb_daily", lb.daily),
            "weekly": ("lb_weekly", lb.weekly),
            "monthly": ("lb_monthly", lb.monthly),
            "yearly": ("lb_yearly", lb.yearly),
        }
        boards: dict[str, list] = {var: [] for var, _ in board_builders.values()}
        active_var, build_active = board_builders.get(active_tab, board_builders["users"])
        boards[active_var] = build_active()

        base_params = {k: v for k, v in {
            "usage_type_filter": usage_type_filter,
            "model_filter": model_filter,
            "tier_filter": tier_filter,
            "tier_exact": tier_exact,
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
            tier_filter=tier_filter,
            tier_exact=tier_exact,
            start_date=start_date,
            end_date=end_date,
            top_n=top_n,
            max_result_limit=max_result_limit,
            all_usage_types=all_usage_types,
            all_models=all_models,
            all_tiers=all_tiers,
            **boards,
            base_query=urlencode(base_params),
            min_credits=min_credits,
            max_credits=max_credits,
            zero_credits=zero_credits,
        )

    @bp.route("/leaderboard/export.csv", methods=["GET"])
    def leaderboard_export_csv() -> object:
        d = data()
        active_tab = request.args.get("active_tab", "users")
        top_n = result_limit("top_n", 25, 5)
        lb = Leaderboards(_leaderboard_filtered_df(d), top_n)

        boards: dict[str, tuple[list[dict], list[tuple[str, str]]]] = {
            "users": (
                lb.by_user(),
                [
                    ("Rank", "_rank"), ("Name", "name"), ("Email", "email"),
                    ("Credits", "total_credits"), ("Records", "rows"),
                    ("Token/msg tk", "total_tokens"), ("Token/msg msg", "total_messages"),
                ],
            ),
            "users_by_type": (
                lb.by_user_type(),
                [
                    ("Rank", "_rank"), ("Name", "name"), ("Email", "email"),
                    ("Usage Type", "usage_type_parsed_type"), ("Credits", "total_credits"),
                    ("Records", "rows"),
                ],
            ),
            "models": (
                lb.by_model(),
                [
                    ("Rank", "_rank"), ("Model", "usage_type_model"),
                    ("Credits", "total_credits"), ("Records", "rows"),
                    ("Unique users", "unique_users"),
                ],
            ),
            "usage_types": (
                lb.by_usage_type(),
                [
                    ("Rank", "_rank"), ("Usage Type", "usage_type_parsed_type"),
                    ("Credits", "total_credits"), ("Records", "rows"),
                    ("Unique users", "unique_users"),
                ],
            ),
            "tiers": (
                lb.by_tier(),
                [
                    ("Rank", "_rank"), ("Tier", "tier"),
                    ("Credits", "total_credits"), ("Records", "rows"),
                    ("Unique users", "unique_users"),
                ],
            ),
            "biggest_single": (
                lb.biggest_single(),
                [
                    ("Rank", "_rank"), ("Name", "name"), ("Email", "email"),
                    ("Credits", "usage_credits"), ("Usage Type", "usage_type_parsed_type"),
                    ("Model", "usage_type_model"), ("IO", "usage_type_io"),
                    ("(Token/msg)", "usage_quantity"), ("Units", "usage_units"),
                    ("Date", "date_partition"), ("Raw usage type", "usage_type"),
                ],
            ),
            "yearly": (
                lb.yearly(),
                [
                    ("Rank", "_rank"), ("Year", "year"), ("Credits", "total_credits"),
                    ("Records", "rows"), ("Unique users", "unique_users"),
                ],
            ),
            "monthly": (
                lb.monthly(),
                [
                    ("Rank", "_rank"), ("Month", "month"), ("Credits", "total_credits"),
                    ("Records", "rows"), ("Unique users", "unique_users"),
                ],
            ),
            "weekly": (
                lb.weekly(),
                [
                    ("Rank", "_rank"), ("Week of", "week"), ("Credits", "total_credits"),
                    ("Records", "rows"), ("Unique users", "unique_users"),
                ],
            ),
            "daily": (
                lb.daily(),
                [
                    ("Rank", "_rank"), ("Date", "date_partition"), ("Credits", "total_credits"),
                    ("Records", "rows"), ("Unique users", "unique_users"),
                ],
            ),
        }
        rows, columns = boards.get(active_tab, boards["users"])
        export_rows = [{**row, "_rank": idx} for idx, row in enumerate(rows, start=1)]
        return csv_response(labeled_export_df(export_rows, columns),
                            f"leaderboard_{active_tab}.csv", filters=[
                                ("type", request.args.get("usage_type_filter", "")),
                                ("model", request.args.get("model_filter", "")),
                                ("dates", range_slug(request.args.get("start_date", ""),
                                                     request.args.get("end_date", ""))),
                                ("credits", range_slug(request.args.get("min_credits", "").strip(),
                                                       request.args.get("max_credits", "").strip(), "0", "max")),
                                ("zero", "only" if request.args.get("zero_credits") == "1" else ""),
                                ("top", top_n if top_n != 25 else ""),
                            ])

    def _user_summary_state() -> dict:
        """Filtered records for one user plus the query params that shaped
        them — shared by the user-summary page and its CSV export."""
        d = data()
        name = request.args.get("name", "")
        email = request.args.get("email", "")
        date_field = request.args.get("date_field", "date_partition")
        start_date = request.args.get("start_date", "")
        end_date = request.args.get("end_date", "")
        sort_by = request.args.get("sort_by", "")
        sort_order = request.args.get("sort_order", "asc")
        is_form_submission = bool(request.args.get("fs", ""))
        models_explicitly_set = bool(request.args.get("mfs", ""))
        min_credits = request.args.get("min_credits", "").strip()
        max_credits = request.args.get("max_credits", "").strip()
        zero_credits = request.args.get("zero_credits", "")

        df = d.df.copy()
        if name and "name" in df.columns:
            df = df[df["name"].astype(str).str.contains(name.strip(), case=False, na=False, regex=False)]
        if email and "email" in df.columns:
            df = df[df["email"].astype(str).str.contains(email.strip(), case=False, na=False, regex=False)]
        if date_field and (start_date or end_date):
            df = d.filter_by_date(df, start_date, end_date, col=date_field)
        df = d.filter_by_credits(df, min_credits, max_credits, zero_credits)

        # Records scoped to this user only (before usage-type/model filters), so
        # the narrative "stories" see the full picture across tools.
        user_scope_df = df.copy()

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
        if not is_form_submission or not models_explicitly_set:
            filter_models = user_models
        else:
            valid = [m for m in requested_models if m in user_models]
            # Model checkboxes only submit when rendered: a model whose usage
            # type was just re-checked wasn't on the submitting form at all,
            # so it defaults to selected instead of counting as deliberately
            # unchecked. models_shown lists what the form actually displayed.
            shown = set(request.args.getlist("models_shown"))
            if shown:
                valid += [m for m in user_models if m not in shown and m not in valid]
            filter_models = valid if (not requested_models or valid) else user_models
        if "usage_type_model" in df.columns:
            df = df[df["usage_type_model"].isin(filter_models)]

        if sort_by and sort_by in df.columns:
            df = df.sort_values(by=sort_by, ascending=(sort_order == "asc"))

        hidden_by_default = {"name", "email", "account_id", "account_user_id", "public_id"}
        selected_fields = request.args.getlist("selected_fields")
        if not selected_fields:
            selected_fields = [
                col for col in DEFAULT_RECORD_COLUMNS
                if col in df.columns and col not in hidden_by_default
            ]
            display_columns = selected_fields
        else:
            display_columns = [c for c in selected_fields if c in df.columns]

        return {
            "d": d,
            "df": df,
            "user_scope_df": user_scope_df,
            "name": name,
            "email": email,
            "date_field": date_field,
            "start_date": start_date,
            "end_date": end_date,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "is_form_submission": is_form_submission,
            "min_credits": min_credits,
            "max_credits": max_credits,
            "zero_credits": zero_credits,
            "user_types": user_types,
            "filter_types": filter_types,
            "user_models": user_models,
            "filter_models": filter_models,
            "selected_fields": selected_fields,
            "display_columns": display_columns,
        }

    @bp.route("/user-summary", methods=["GET"])
    def user_summary() -> str:
        state = _user_summary_state()
        d = state["d"]
        df = state["df"]
        user_scope_df = state["user_scope_df"]
        name = state["name"]
        email = state["email"]
        date_field = state["date_field"]
        start_date = state["start_date"]
        end_date = state["end_date"]
        sort_by = state["sort_by"]
        sort_order = state["sort_order"]
        is_form_submission = state["is_form_submission"]
        min_credits = state["min_credits"]
        max_credits = state["max_credits"]
        zero_credits = state["zero_credits"]
        user_types = state["user_types"]
        filter_types = state["filter_types"]
        user_models = state["user_models"]
        filter_models = state["filter_models"]
        selected_fields = state["selected_fields"]
        display_columns = state["display_columns"]
        active_tab = request.args.get("active_tab", "overview")

        # Header display name: the query param when given, else the latest
        # non-empty name in this user's records — email-only deep links
        # (alerts, story pages, hand-typed URLs) land here without a name.
        display_name = name
        if not display_name and "name" in user_scope_df.columns and len(user_scope_df):
            known = user_scope_df["name"].dropna().astype(str).str.strip()
            known = known[known != ""]
            if not known.empty:
                display_name = known.iloc[-1]

        record_columns, rows_data = build_record_view(df, display_columns)
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
        display_column_options = [record_column_meta(col) for col in display_headers]

        summ_usage_type = make_summary("usage_type_parsed_type")
        summ_model = make_summary("usage_type_model")
        summ_io = make_summary("usage_type_io")
        summ_raw = make_summary("usage_type")

        # The user's credit limits (governance tier via the optimization
        # engine) — shown in the hero as their current allowance. Not drawn
        # as a per-week line on the chart: their tier may have changed over
        # time and we have no reliable date for when a past tier applied
        # (the tier-change log only has import dates, not effective dates),
        # so a flat "current cap" line across historical weeks would
        # misrepresent what applied then.
        user_tier_display = ""
        user_cap_weekly = 0.0
        user_cap_monthly = 0.0
        try:
            gov = services.governance
            user_tier_display = gov.tier_for(email) if email else "Baseline"
            user_cap_weekly = float(
                gov.weekly_caps(week_start=pd.Timestamp.today()).get(user_tier_display) or 0
            )
            user_cap_monthly = float(gov.monthly_cap_for(user_tier_display) or 0)
        except Exception:
            pass

        user_tier_history_list = services.governance.tier_history_map().get(
            str(email or "").strip().lower(), []
        )
        user_was_summer_intern = "2026 Summer Interns" in user_tier_history_list
        # Matches any "... AI Jam Captain(s)" group label so this keeps working
        # across future tierlist imports (e.g. a "2027 Summer AI Jam Captains").
        user_was_ai_jam_captain = any(
            "ai jam captain" in str(t).lower() for t in user_tier_history_list
        )

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

        # Everything about the user themself (recommendation, tier history,
        # stories, triggering alerts, tier-change log) — see summary_context.py.
        from .summary_context import build_user_summary_context

        user_ctx = build_user_summary_context(services, d.df, user_scope_df, email, name)
        optimization_page_available = "optimization.optimization_page" in current_app.view_functions

        # Did any monthly-era month actually blow through its cap? Drives the
        # chart's default: the monthly-cap line only shows when it mattered.
        user_monthly_cap_exceeded = any(
            float(m.get("pct", 0) or 0) >= 1.0 and not m.get("weekly_regime")
            for m in user_ctx.get("user_month_history", [])
        )

        return render_template(
            "user_summary.html",
            name=name,
            display_name=display_name,
            email=email,
            rows=len(df),
            total_credits=total_credits,
            totals_by_unit=totals_by_unit,
            rows_data=rows_data,
            display_columns=display_columns,
            display_headers=display_headers,
            display_column_options=display_column_options,
            record_columns=record_columns,
            headers=display_headers,
            date_field=date_field,
            start_date=start_date,
            end_date=end_date,
            min_credits=min_credits,
            max_credits=max_credits,
            zero_credits=zero_credits,
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
            user_tier_display=user_tier_display,
            user_cap_weekly=user_cap_weekly,
            user_cap_monthly=user_cap_monthly,
            user_was_summer_intern=user_was_summer_intern,
            user_was_ai_jam_captain=user_was_ai_jam_captain,
            user_monthly_cap_exceeded=user_monthly_cap_exceeded,
            user_usage_type_weekly=usage_type_weekly_json(df),
            type_chart_json=type_chart_json,
            optimization_page_available=optimization_page_available,
            tier_editing_locked=config_svc.is_tier_editing_locked(),
            user_notes=config_svc.load_user_notes().get((email or "").strip().lower(), []),
            **user_ctx,
        )

    @bp.route("/user-summary/export.csv", methods=["GET"])
    def user_summary_export_csv() -> object:
        state = _user_summary_state()
        columns, rows = build_record_view(state["df"], state["display_columns"])
        types_narrowed = len(state["filter_types"]) < len(state["user_types"])
        models_narrowed = len(state["filter_models"]) < len(state["user_models"])
        sort = f"{state['sort_by']}_{state['sort_order']}" if state["sort_by"] else ""
        return csv_response(labeled_export_df(rows, columns), "user_summary_records.csv", filters=[
            ("user", state["email"] or state["name"]),
            ("dates", range_slug(state["start_date"], state["end_date"])),
            ("credits", range_slug(state["min_credits"], state["max_credits"], "0", "max")),
            ("zero", "only" if state["zero_credits"] == "1" else ""),
            ("types", f"{len(state['filter_types'])}of{len(state['user_types'])}" if types_narrowed else ""),
            ("models", f"{len(state['filter_models'])}of{len(state['user_models'])}" if models_narrowed else ""),
            ("sort", sort),
        ])

    @bp.route("/user-summary/optimization-export.csv", methods=["GET"])
    def user_summary_optimization_export_csv() -> object:
        """This user's weekly cap-utilization history. The Optimization card's
        table shows only the latest 12 weeks; the export carries all of them."""
        from app.optimization.service import build_optimization_result

        d = data()
        email = request.args.get("email", "").strip()
        name = request.args.get("name", "").strip()
        gov = services.governance
        opt = build_optimization_result(d.df, gov.tier_config(), gov.resolved_assignments())
        hist = opt.user_week_history
        if hist is None or hist.empty:
            hist = pd.DataFrame()
        elif email and "email" in hist.columns:
            hist = hist[hist["email"].astype(str).str.lower() == email.lower()]
        elif name and "latest_name" in hist.columns:
            hist = hist[hist["latest_name"].astype(str).str.contains(name, case=False, na=False, regex=False)]
        else:
            hist = pd.DataFrame()

        cols = [
            ("Week start", "week_start"), ("Week end", "week_end"),
            ("Tier", "governance_tier"), ("Credits used", "credits_used"),
            ("Weekly cap", "weekly_credit_cap"), ("Utilization", "cap_utilization"),
            ("Remaining", "remaining_weekly_credits"), ("Pressure", "pressure_flag"),
        ]
        rows = []
        if not hist.empty:
            for _, r in hist.sort_values("week_start", ascending=False).iterrows():
                row = {}
                for label, key in cols:
                    v = r.get(key, "")
                    if key in ("week_start", "week_end") and hasattr(v, "date"):
                        v = str(v.date())
                    elif key == "cap_utilization" and v != "":
                        v = round(float(v), 4)
                    elif key in ("credits_used", "weekly_credit_cap", "remaining_weekly_credits") and v != "":
                        v = round(float(v), 2)
                    row[label] = v
                rows.append(row)
        return csv_response(
            pd.DataFrame(rows, columns=[label for label, _ in cols]),
            "user_optimization_history.csv",
            filters=[("user", email or name)],
        )

    @bp.route("/user-summary/note", methods=["POST"])
    def add_user_note() -> object:
        import uuid
        from datetime import date
        email = request.form.get("email", "").strip()
        title = request.form.get("title", "").strip()
        text = request.form.get("text", "").strip()
        tone = request.form.get("tone", "info").strip().lower()
        if tone not in {"info", "notable", "alert"}:
            tone = "info"
        if not email:
            flash("A user email is required to pin a note.", "danger")
        elif not (title or text):
            flash("Add a title or note text.", "danger")
        else:
            config_svc.add_user_note(email, {
                "id": uuid.uuid4().hex[:8],
                "title": title or "Note",
                "text": text,
                "tone": tone,
                "created": date.today().isoformat(),
            })
            flash("Note pinned.", "success")
        return redirect(url_for("analytics.user_summary", email=email))

    @bp.route("/user-summary/note/delete", methods=["POST"])
    def delete_user_note() -> object:
        email = request.form.get("email", "").strip()
        config_svc.delete_user_note(email, request.form.get("note_id", ""))
        flash("Note removed.", "success")
        return redirect(url_for("analytics.user_summary", email=email))

    def _build_storyboard_timeline(d: CreditUsageData, gov) -> list[dict]:
        """The contract's narrative spine: start/end, credit grants, the
        cap-era switch, each 100k the balance fell through, the research
        forecasts, and the live forecast's own projected exhaustion.
        Defensive — a timeline failure must not break the page/export."""
        all_snapshots = services.pipeline.get_forecast_history(limit=1000)
        contract_config = config_svc.load_contract()
        live_forecast = None
        try:
            fsvc = services.build_forecasting_service(contract_config, anchor=False)
            if fsvc.has_data():
                live_forecast = fsvc.get_forecast()
        except Exception:
            live_forecast = None
        try:
            from app.analytics.stories import build_contract_timeline

            return build_contract_timeline(
                d.df,
                contract_config.get("contract", {}),
                gov.tier_config(),
                all_snapshots,
                live_forecast=live_forecast,
            )
        except Exception:
            return []

    @bp.route("/storyboard/timeline/export.csv", methods=["GET"])
    def storyboard_timeline_export_csv() -> object:
        d = data()
        timeline = _build_storyboard_timeline(d, services.governance)
        rows = [
            {
                "Date": e.get("date", ""),
                "Kind": e.get("kind", ""),
                "Title": e.get("title", ""),
                "Detail": e.get("detail", ""),
                "Tone": e.get("tone", ""),
            }
            for e in timeline
        ]
        return csv_response(
            pd.DataFrame(rows, columns=["Date", "Kind", "Title", "Detail", "Tone"]),
            "contract_timeline.csv",
        )

    @bp.route("/storyboard", methods=["GET"])
    def storyboard() -> str:
        """Org-wide stories hub: every story condition with its current top
        matches, the configured story-alert rules and their live status, and
        all pinned analyst notes — one place to see 'what's the story' today."""
        from app.analytics.stories import STORY_ALERT_METRICS, evaluate_story_rules, story_metric_matches

        d = data()
        gov = services.governance
        cap_by_email, default_cap = gov.monthly_cap_by_email()
        change_date = gov.tier_config().get("cap_period_change_date")
        reference_date = None
        if "date_partition" in d.df.columns:
            reference_date = pd.to_datetime(d.df["date_partition"], errors="coerce").max()

        try:
            days = max(int(request.args.get("days", 30) or 30), 1)
        except (TypeError, ValueError):
            days = 30

        tiers = gov.resolved_assignments()
        boards = []
        for metric, label in STORY_ALERT_METRICS.items():
            matches = story_metric_matches(
                d.df, metric, days, cap_by_email, default_cap, reference_date,
                cap_change_date=change_date,
            )
            for m in matches:
                m["tier"] = tiers.get(m["email"], "Baseline")
            boards.append({
                "metric": metric,
                "label": label.replace("N days", f"{days} days"),
                "count": len(matches),
                "top": matches[:6],
            })

        # Configured story rules + live triggering status (same eval the bell uses).
        story_rules = config_svc.load_story_alert_rules()
        hits = {
            a["id"].split("story:", 1)[-1]: a["detail"]
            for a in evaluate_story_rules(
                d.df, story_rules, cap_by_email, default_cap, reference_date,
                cap_change_date=change_date,
            )
        }

        # Pinned analyst notes across all users, newest first.
        notes = []
        for note_email, user_notes in config_svc.load_user_notes().items():
            for n in user_notes:
                notes.append({**n, "email": note_email, "tier": tiers.get(note_email, "Baseline")})
        notes.sort(key=lambda n: str(n.get("created", "")), reverse=True)

        all_snapshots = services.pipeline.get_forecast_history(limit=1000)
        timeline = _build_storyboard_timeline(d, gov)

        return render_template(
            "storyboard.html",
            boards=boards,
            days=days,
            story_rules=story_rules,
            story_hits=hits,
            story_metrics=STORY_ALERT_METRICS,
            notes=notes,
            timeline=timeline,
            forecast_history=sorted(all_snapshots, key=lambda h: str(h.get("snapshot_date") or "")),
            reference_date=str(reference_date.date()) if reference_date is not None and not pd.isna(reference_date) else "",
        )

    def _story_matches_data() -> tuple[str, int, list[dict], object]:
        """Metric, window, and matching users from the request args — shared by
        the story-matches page and its CSV export."""
        from app.analytics.stories import STORY_ALERT_METRICS, story_metric_matches

        d = data()
        metric = request.args.get("metric", "burst_cap").strip()
        if metric not in STORY_ALERT_METRICS:
            metric = "burst_cap"
        try:
            days = max(int(request.args.get("days", 30) or 30), 1)
        except (TypeError, ValueError):
            days = 30

        gov = services.governance
        cap_by_email, default_cap = gov.monthly_cap_by_email()
        reference_date = None
        if "date_partition" in d.df.columns:
            reference_date = pd.to_datetime(d.df["date_partition"], errors="coerce").max()

        matches = story_metric_matches(
            d.df, metric, days, cap_by_email, default_cap, reference_date,
            cap_change_date=gov.tier_config().get("cap_period_change_date"),
        )
        tiers = gov.resolved_assignments()
        for m in matches:
            m["tier"] = tiers.get(m["email"], "Baseline")
        return metric, days, matches, reference_date

    @bp.route("/story-matches", methods=["GET"])
    def story_matches() -> str:
        """Everyone who triggered a story metric — the click-through target for
        org-wide story alerts like 'N users burned through their cap in 30 days'."""
        from app.analytics.stories import STORY_ALERT_METRICS

        metric, days, matches, reference_date = _story_matches_data()
        return render_template(
            "story_matches.html",
            metric=metric,
            metric_label=STORY_ALERT_METRICS[metric],
            metric_options=STORY_ALERT_METRICS,
            days=days,
            matches=matches,
            reference_date=str(reference_date.date()) if reference_date is not None and not pd.isna(reference_date) else "",
        )

    @bp.route("/story-matches/export.csv", methods=["GET"])
    def story_matches_export_csv() -> object:
        metric, days, matches, _ = _story_matches_data()
        per_metric = {
            "burst_cap": [
                (f"Peak {days}-day spend", "peak_spend"),
                ("Month's allowance", "monthly_cap"),
                ("% of allowance", "_pct"),
                ("Window ended", "window_end"),
                ("Cap regime", "cap_regime"),
            ],
            "inactive": [("Days inactive", "days_inactive"), ("Last active", "last_active")],
            "pro_codex": [("Same-day Pro+Codex days", "times"), ("Most recent", "last_date")],
        }
        columns = [("Name", "name"), ("Email", "email"), ("Tier", "tier")] + per_metric.get(metric, [])
        export_rows = [
            {**m, "_pct": round(float(m["pct_of_cap"]) * 100, 1)}
            if m.get("pct_of_cap") is not None else m
            for m in matches
        ]
        return csv_response(
            labeled_export_df(export_rows, columns),
            f"story_matches_{metric}.csv",
            filters=[("days", days)],
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
        tier_query = request.args.get("tier_query", "").strip()
        # Default to the canonical usage date so a plain date-range search works
        # without forcing the user to choose a field first.
        date_field = request.args.get("date_field", "date_partition")
        start_date = request.args.get("start_date", "")
        end_date = request.args.get("end_date", "")
        top_n = result_limit("top_n", 50)
        min_credits = request.args.get("min_credits", "").strip()
        max_credits = request.args.get("max_credits", "").strip()
        zero_credits = request.args.get("zero_credits", "")
        sort_by = request.args.get("sort", "credits").strip()
        if sort_by not in {"credits", "alpha"}:
            sort_by = "credits"
        per_page = 25
        try:
            page = max(1, int(request.args.get("page", 1) or 1))
        except (TypeError, ValueError):
            page = 1

        # The name/email/tier search applies in both modes.
        df = d.df.copy()
        df = _apply_user_search_filters(df, name_query, email_query, tier_query)

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
        all_tiers_list = _tier_options(d)

        user_list: list[dict] = []
        outlier_rows: list[dict] = []
        outlier_count = 0
        outlier_columns: list[dict] = []
        window_start = window_end = ""
        total_users = 0
        total_pages = 1

        if mode == "advanced":
            outlier_rows, outlier_count, window_start, window_end, outlier_columns = compute_outliers(
                df, metric, credit_threshold, lookback_days,
                start_date=start_date, end_date=end_date,
                usage_type_filter=adv_usage_type, model_filter=adv_model,
                top_n=top_n,
            )
        else:
            total_agg = _aggregate_basic_users(d)
            total_users = len(total_agg)
            total_pages = max(1, -(-total_users // per_page))
            page = min(page, total_pages)
            start = (page - 1) * per_page
            user_list = total_agg.iloc[start:start + per_page].to_dict(orient="records")

        # Query string (minus `view`/`page`) so the cards/table/list toggle and
        # the pagination links can switch those while preserving the active
        # search/filters.
        base_params = {k: v for k, v in {
            "mode": "basic",
            "name_query": name_query,
            "email_query": email_query,
            "tier_query": tier_query,
            "date_field": date_field,
            "start_date": start_date,
            "end_date": end_date,
            "top_n": top_n if top_n != 50 else "",
            "min_credits": min_credits,
            "max_credits": max_credits,
            "zero_credits": zero_credits,
            "sort": sort_by if sort_by != "credits" else "",
        }.items() if v}

        return render_template(
            "user_cards.html",
            headers=d.columns,
            mode=mode,
            view=view,
            base_query=urlencode(base_params),
            name_query=name_query,
            email_query=email_query,
            tier_query=tier_query,
            all_tiers_list=all_tiers_list,
            date_field=date_field,
            start_date=start_date,
            end_date=end_date,
            top_n=top_n,
            max_result_limit=max_result_limit,
            users=user_list,
            min_credits=min_credits,
            max_credits=max_credits,
            zero_credits=zero_credits,
            sort_by=sort_by,
            page=page,
            per_page=per_page,
            total_users=total_users,
            total_pages=total_pages,
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

    @bp.route("/user-cards/export.csv", methods=["GET"])
    def user_cards_export_csv() -> object:
        """Download the current Users result as CSV."""
        d = data()
        mode = "advanced" if request.args.get("mode", "basic").strip() == "advanced" else "basic"
        if mode != "advanced":
            columns = [
                ("Name", "name"), ("Email", "email"), ("Records", "rows"),
                ("Credits", "total_credits"), ("Quantity", "total_quantity"),
                ("Tokens", "total_tokens"), ("Messages", "total_counts"),
                ("Duration seconds", "total_duration_s"),
            ]
            rows = _aggregate_basic_users(d).to_dict(orient="records")
            export_df = pd.DataFrame(
                [{label: row.get(key, "") for label, key in columns} for row in rows],
                columns=[label for label, _ in columns],
            )
            date_range = (
                f"{request.args.get('start_date', '')}_to_{request.args.get('end_date', '')}"
                if request.args.get("start_date", "") or request.args.get("end_date", "") else ""
            )
            credit_range = (
                f"{request.args.get('min_credits', '').strip() or '0'}_to_"
                f"{request.args.get('max_credits', '').strip() or 'max'}"
                if request.args.get("min_credits", "").strip() or request.args.get("max_credits", "").strip()
                else ""
            )
            return csv_response(export_df, "users.csv", filters=[
                ("name", request.args.get("name_query", "").strip()),
                ("email", request.args.get("email_query", "").strip()),
                ("tier", request.args.get("tier_query", "").strip()),
                ("dates", date_range),
                ("credits", credit_range),
                ("zero", "only" if request.args.get("zero_credits") == "1" else ""),
                ("top", request.args.get("top_n", "") if request.args.get("top_n", "") != "50" else ""),
            ])

        name_query = request.args.get("name_query", "").strip()
        email_query = request.args.get("email_query", "").strip()
        tier_query = request.args.get("tier_query", "").strip()
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
        df = _apply_user_search_filters(df, name_query, email_query, tier_query)

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

        fname = f"outliers_{metric}_{win_start}_to_{win_end}.csv"
        threshold = int(credit_threshold) if credit_threshold == int(credit_threshold) else credit_threshold
        return csv_response(export_df, fname, filters=[
            ("name", name_query),
            ("email", email_query),
            ("tier", tier_query),
            ("type", adv_usage_type),
            ("model", adv_model),
            ("over", threshold),
            ("top", top_n if top_n != 200 else ""),
        ])

    @bp.route("/user-cards/export", methods=["GET"])
    def export_outliers() -> object:
        """Legacy URL: keep old links working, but return CSV now."""
        return user_cards_export_csv()

    return bp
