from __future__ import annotations

import pandas as pd
from flask import Blueprint, render_template, request

from app.shared.csv_export import csv_response
from .service import build_optimization_result


def create_optimization_blueprint(services) -> Blueprint:
    pipeline = services.pipeline
    config_svc = services.config_svc
    store = services.store
    bp = Blueprint("optimization", __name__, template_folder="templates", url_prefix="")

    def _result():
        return build_optimization_result(
            pipeline.processed_dir,
            store.data.df,
            config_svc.load_tiers(),
        )

    def _range_label(min_value: str, max_value: str) -> str:
        min_value = str(min_value or "").strip()
        max_value = str(max_value or "").strip()
        if not min_value and not max_value:
            return ""
        return f"{min_value or '0'}_to_{max_value or 'max'}"

    def _filter_recommendations(result):
        state = {
            "q": request.args.get("q", "").strip(),
            "action": request.args.get("action", "").strip(),
            "priority": request.args.get("priority", "").strip(),
            "current_tier": request.args.get("current_tier", "").strip(),
            "recommended_tier": request.args.get("recommended_tier", "").strip(),
            "min_util": request.args.get("min_util", "").strip(),
            "max_util": request.args.get("max_util", "").strip(),
            "min_avg_credits": request.args.get("min_avg_credits", "").strip(),
            "max_avg_credits": request.args.get("max_avg_credits", "").strip(),
        }

        df = result.recommendations.copy()
        if df.empty:
            return df, state

        if state["q"]:
            mask = pd.Series(False, index=df.index)
            for col in ("email", "latest_name", "latest_department"):
                if col in df.columns:
                    mask |= df[col].astype(str).str.contains(state["q"], case=False, na=False, regex=False)
            df = df[mask]
        if state["action"] and "recommended_action" in df.columns:
            df = df[df["recommended_action"] == state["action"]]
        if state["priority"] and "review_priority" in df.columns:
            df = df[df["review_priority"] == state["priority"]]
        if state["current_tier"] and "latest_governance_tier" in df.columns:
            df = df[df["latest_governance_tier"] == state["current_tier"]]
        if state["recommended_tier"] and "recommended_tier" in df.columns:
            df = df[df["recommended_tier"] == state["recommended_tier"]]

        for key, col in (
            ("min_util", "latest_cap_utilization"),
            ("max_util", "latest_cap_utilization"),
            ("min_avg_credits", "avg_weekly_credits_used"),
            ("max_avg_credits", "avg_weekly_credits_used"),
        ):
            if not state[key] or col not in df.columns:
                continue
            val = pd.to_numeric(state[key], errors="coerce")
            if pd.isna(val):
                continue
            if key.startswith("min"):
                df = df[pd.to_numeric(df[col], errors="coerce") >= float(val)]
            else:
                df = df[pd.to_numeric(df[col], errors="coerce") <= float(val)]

        return df, state

    @bp.route("/optimization", methods=["GET"])
    def optimization_page() -> str:
        result = _result()
        recommendations, filters = _filter_recommendations(result)

        actions = (
            result.recommendations["recommended_action"].dropna().unique().tolist()
            if not result.recommendations.empty and "recommended_action" in result.recommendations.columns else []
        )
        priorities = (
            result.recommendations["review_priority"].dropna().unique().tolist()
            if not result.recommendations.empty and "review_priority" in result.recommendations.columns else []
        )
        current_tiers = (
            result.recommendations["latest_governance_tier"].dropna().unique().tolist()
            if not result.recommendations.empty and "latest_governance_tier" in result.recommendations.columns else []
        )
        recommended_tiers = (
            result.recommendations["recommended_tier"].dropna().unique().tolist()
            if not result.recommendations.empty and "recommended_tier" in result.recommendations.columns else []
        )

        actionable = 0
        if not result.recommendations.empty:
            actionable = int(result.recommendations["review_priority"].isin(["URGENT", "ACTIONABLE"]).sum())

        return render_template(
            "optimization.html",
            result=result,
            recommendations=recommendations.head(250).to_dict(orient="records") if not recommendations.empty else [],
            recommendation_count=len(recommendations),
            actions=sorted(actions),
            priorities=sorted(priorities),
            current_tiers=sorted(current_tiers),
            recommended_tiers=sorted(recommended_tiers),
            filters=filters,
            actionable=actionable,
            tier_summary=result.tier_summary.to_dict(orient="records") if not result.tier_summary.empty else [],
            rec_summary=result.recommendation_summary.to_dict(orient="records") if not result.recommendation_summary.empty else [],
        )

    @bp.route("/optimization/export.csv", methods=["GET"])
    def optimization_export_csv() -> object:
        result = _result()
        dataset = request.args.get("dataset", "recommendations")
        frames = {
            "recommendations": result.recommendations,
            "user_week_history": result.user_week_history,
            "tier_summary": result.tier_summary,
            "recommendation_summary": result.recommendation_summary,
        }
        if dataset == "recommendations":
            df, filter_state = _filter_recommendations(result)
        else:
            df = frames.get(dataset, result.recommendations)
            filter_state = {}
        if df is None or df.empty:
            df = pd.DataFrame()
        return csv_response(df, f"optimization_{dataset}.csv", filters=[
            ("source", result.source_label),
            ("dataset", dataset),
            ("search", filter_state.get("q", "")),
            ("action", filter_state.get("action", "")),
            ("priority", filter_state.get("priority", "")),
            ("current", filter_state.get("current_tier", "")),
            ("recommended", filter_state.get("recommended_tier", "")),
            ("util", _range_label(filter_state.get("min_util", ""), filter_state.get("max_util", ""))),
            ("avgcredits", _range_label(filter_state.get("min_avg_credits", ""), filter_state.get("max_avg_credits", ""))),
        ])

    return bp
