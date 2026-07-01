from __future__ import annotations

from datetime import date
from io import StringIO
import re

import pandas as pd
from flask import Blueprint, Response, render_template, request

from .service import build_optimization_result


def create_optimization_blueprint(services) -> Blueprint:
    store = services.store
    config_svc = services.config_svc
    bp = Blueprint("optimization", __name__, template_folder="templates", url_prefix="")

    def _result():
        return build_optimization_result(store.data.df, config_svc.load_tiers())

    def _slug(value: object, max_chars: int = 36) -> str:
        text = str(value or "").strip().lower()
        text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
        return text[:max_chars].strip("-")

    def _range_label(min_value: str, max_value: str) -> str:
        min_value = str(min_value or "").strip()
        max_value = str(max_value or "").strip()
        if not min_value and not max_value:
            return ""
        return f"{min_value or '0'}-to-{max_value or 'max'}"

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
            series = pd.to_numeric(df[col], errors="coerce")
            df = df[series >= float(val)] if key.startswith("min") else df[series <= float(val)]

        return df, state

    def _csv_response(df: pd.DataFrame, name: str, filters: dict) -> Response:
        parts = [name]
        for key in ("q", "action", "priority", "current_tier", "recommended_tier"):
            val = _slug(filters.get(key, ""))
            if val:
                parts.append(f"{_slug(key, 16)}-{val}")
        util_range = _range_label(filters.get("min_util", ""), filters.get("max_util", ""))
        credit_range = _range_label(filters.get("min_avg_credits", ""), filters.get("max_avg_credits", ""))
        if util_range:
            parts.append(f"util-{_slug(util_range)}")
        if credit_range:
            parts.append(f"avgcredits-{_slug(credit_range)}")
        parts.append(date.today().isoformat())
        filename = "_".join(parts)
        if len(filename) > 145:
            filename = f"{filename[:134].rstrip('-_')}_{date.today().isoformat()}"
        bio = StringIO()
        df.to_csv(bio, index=False)
        return Response(
            bio.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
        )

    @bp.route("/optimization", methods=["GET"])
    def optimization_page() -> str:
        result = _result()
        recommendations, filters = _filter_recommendations(result)

        source = result.recommendations
        actions = sorted(source["recommended_action"].dropna().unique().tolist()) if not source.empty else []
        priorities = sorted(source["review_priority"].dropna().unique().tolist()) if not source.empty else []
        current_tiers = sorted(source["latest_governance_tier"].dropna().unique().tolist()) if not source.empty else []
        recommended_tiers = sorted(source["recommended_tier"].dropna().unique().tolist()) if not source.empty else []
        actionable = int(source["review_priority"].isin(["ACTIONABLE"]).sum()) if not source.empty else 0

        return render_template(
            "optimization.html",
            result=result,
            recommendations=recommendations.head(250).to_dict(orient="records") if not recommendations.empty else [],
            recommendation_count=len(recommendations),
            actions=actions,
            priorities=priorities,
            current_tiers=current_tiers,
            recommended_tiers=recommended_tiers,
            filters=filters,
            actionable=actionable,
            rec_summary=result.recommendation_summary.to_dict(orient="records") if not result.recommendation_summary.empty else [],
            tier_summary=result.tier_summary.to_dict(orient="records") if not result.tier_summary.empty else [],
        )

    @bp.route("/optimization/export.csv", methods=["GET"])
    def optimization_export_csv() -> Response:
        result = _result()
        recommendations, filters = _filter_recommendations(result)
        return _csv_response(recommendations, "optimization_recommendations", filters)

    return bp

