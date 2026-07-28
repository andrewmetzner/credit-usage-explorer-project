from __future__ import annotations

from datetime import date
from io import StringIO
import re

import pandas as pd
from flask import Blueprint, Response, flash, redirect, render_template, request, url_for

from .service import (
    build_optimization_result,
    highest_cap_tier,
    is_codex_access_tier,
    tier_caps,
)

# Optional: pushes a manual/recommended tier change through to the matching
# ChatGPT group via the standalone Admin API explorer package. Wrapped so a
# machine with no aauth/ key configured still works fine -- tier changes just
# stay local, same as before this existed. See openai_admin_API/README.md.
try:
    from openai_admin_API import endpoints as _admin_ep
    from openai_admin_API.client import AdminApiClient, AdminApiError, load_write_workspace_id
    _ADMIN_API_AVAILABLE = True
except ImportError:
    _ADMIN_API_AVAILABLE = False


class _AdminApiTierPusher:
    """Builds its Admin API client + group-name lookup ONCE per request
    (not once per user), then pushes each tier change as a best-effort
    group move. Never raises out of .push() — a failure here must never
    block or roll back the LOCAL tier change, which has already happened
    by the time this runs. .push() returns a short status string for an
    optional flash, or None to stay silent (the common case in bulk apply)."""

    def __init__(self) -> None:
        self.client = None
        self.workspace_id = ""
        self.groups_by_name: dict[str, str] = {}
        self.error: str | None = None
        if not _ADMIN_API_AVAILABLE:
            return
        self.workspace_id = load_write_workspace_id()
        if not self.workspace_id:
            return
        try:
            self.client = AdminApiClient()
            self.groups_by_name = {g["name"]: g["id"] for g in _admin_ep.list_groups(self.client, self.workspace_id)}
        except AdminApiError as exc:
            self.error = str(exc)
            self.client = None

    def push(self, email: str, tier: str) -> str | None:
        if not self.client:
            return None
        try:
            user_id = _admin_ep.find_user_id_by_email(self.client, self.workspace_id, email)
            if not user_id:
                return f"{email}: not found in the ChatGPT workspace directory — group unchanged there."
            result = _admin_ep.move_user_to_group_by_name(self.client, self.workspace_id, user_id, tier, self.groups_by_name)
            if result["added_to"] or result["removed_from"]:
                return f"{email}: also moved to the '{tier}' ChatGPT group."
            return None
        except AdminApiError as exc:
            return f"{email}: tier updated locally, but couldn't sync to ChatGPT ({exc})."

# Map each sortable table column to the underlying recommendations DataFrame
# column. Priority sorts by the numeric action rank so the most actionable rows
# lead, rather than alphabetically by label.
RECOMMENDATION_SORT_COLUMNS = {
    "user": "latest_name",
    "current_tier": "latest_governance_tier",
    "recommended": "recommended_tier",
    "action": "recommended_action",
    "priority": "action_priority_rank",
    "latest_util": "latest_cap_utilization",
    "avg_weekly": "avg_weekly_credits_used",
    "cap_change": "recommended_cap_change",
    "trend": "pressure_trend",
}


def create_optimization_blueprint(services) -> Blueprint:
    store = services.store
    config_svc = services.config_svc
    bp = Blueprint("optimization", __name__, template_folder="templates", url_prefix="")

    def _result():
        # Codex groups are product access, not credit tiers — resolve them to the
        # user's real governance tier so they don't skew the optimization math.
        gov = services.governance
        return build_optimization_result(store.data.df, gov.tier_config(), gov.resolved_assignments())

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

    def _sort_recommendations(df: pd.DataFrame):
        """Sort by a clicked column (numeric-aware), like the Records table."""
        sort_by = request.args.get("sort_by", "").strip()
        sort_order = request.args.get("sort_order", "asc").strip()
        if sort_order not in {"asc", "desc"}:
            sort_order = "asc"
        col = RECOMMENDATION_SORT_COLUMNS.get(sort_by)
        if not df.empty and col and col in df.columns:
            numeric = pd.to_numeric(df[col], errors="coerce")
            if numeric.notna().any():
                df = df.assign(_sort_key=numeric).sort_values(
                    "_sort_key", ascending=(sort_order == "asc"), na_position="last"
                ).drop(columns="_sort_key")
            else:
                df = df.sort_values(
                    col, ascending=(sort_order == "asc"), na_position="last",
                    key=lambda s: s.astype(str).str.lower(),
                )
        return df, sort_by, sort_order

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
        recommendations, sort_by, sort_order = _sort_recommendations(recommendations)
        tiers = tier_caps(config_svc.load_tiers())
        available_tiers = [
            name for name, _ in sorted(tiers.items(), key=lambda item: item[1])
            if not is_codex_access_tier(name)
        ]
        assigned_tier_count = len(config_svc.load_user_tiers())

        source = result.recommendations
        actions = sorted(source["recommended_action"].dropna().unique().tolist()) if not source.empty else []
        priorities = sorted(source["review_priority"].dropna().unique().tolist()) if not source.empty else []
        current_tiers = sorted(set(available_tiers) | (set(source["latest_governance_tier"].dropna().unique().tolist()) if not source.empty else set()))
        recommended_tiers = sorted(set(available_tiers) | (set(source["recommended_tier"].dropna().unique().tolist()) if not source.empty else set()))
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
            available_tiers=available_tiers,
            tier_editing_locked=config_svc.is_tier_editing_locked(),
            assigned_tier_count=assigned_tier_count,
            sort_by=sort_by,
            sort_order=sort_order,
            return_to=request.full_path,
            rec_summary=result.recommendation_summary.to_dict(orient="records") if not result.recommendation_summary.empty else [],
            tier_summary=result.tier_summary.to_dict(orient="records") if not result.tier_summary.empty else [],
        )

    def _safe_next(default_endpoint: str = "optimization.optimization_page") -> str:
        next_url = request.form.get("next", "") or url_for(default_endpoint)
        if not next_url.startswith("/"):
            next_url = url_for(default_endpoint)
        return next_url

    @bp.route("/optimization/user-tier", methods=["POST"])
    def update_user_tier() -> object:
        email = request.form.get("email", "").strip().lower()
        tier = request.form.get("tier", "").strip()
        next_url = _safe_next()

        if config_svc.is_tier_editing_locked():
            flash("Tier editing is locked. Unlock it in Settings to change tiers.", "warning")
            return redirect(next_url)

        tiers = tier_caps(config_svc.load_tiers())
        assignments = config_svc.load_user_tiers()
        if not email:
            flash("No user selected for tier update.", "warning")
            return redirect(next_url)
        if tier and tier not in tiers:
            flash(f"Tier '{tier}' is not in the current tier policy.", "danger")
            return redirect(next_url)

        if tier:
            assignments[email] = tier
            config_svc.record_tier_change(email, tier, "manual")
            flash(f"Tier updated for {email}.", "success")
        else:
            assignments.pop(email, None)
            config_svc.record_tier_change(email, "Baseline", "manual-reset")
            flash(f"Tier reset to Baseline default for {email}.", "success")
        config_svc.save_user_tiers(assignments)

        # Best-effort push to the matching ChatGPT group ; never blocks or
        # reverts the local change above, which has already been saved.
        push_note = _AdminApiTierPusher().push(email, tier or "Baseline")
        if push_note:
            flash(push_note, "info")
        return redirect(next_url)

    def _apply_recommendations(only_email: str = "") -> tuple[int, list[str]]:
        """Set tier = recommended tier from the current optimization result.

        Applies to one user (only_email) or to every ACTIONABLE recommendation.
        Returns (applied_count, ["email -> tier", ...] sample). Each change is
        recorded in the dated tier-change log with source "recommendation"."""
        rec = _result().recommendations
        if rec is None or rec.empty:
            return 0, []
        rows = rec
        if only_email:
            rows = rec[rec["email"].astype(str).str.lower() == only_email]
        else:
            rows = rec[
                (rec["review_priority"] == "ACTIONABLE")
                & (rec["recommended_tier"].astype(str) != rec["latest_governance_tier"].astype(str))
            ]
        assignments = config_svc.load_user_tiers()
        valid_tiers = tier_caps(config_svc.load_tiers())
        applied: list[str] = []
        pushed_emails: list[str] = []
        for _, row in rows.iterrows():
            email = str(row.get("email", "")).strip().lower()
            target = str(row.get("recommended_tier", "")).strip()
            if not email or not target or target not in valid_tiers:
                continue
            if str(row.get("latest_governance_tier", "")) == target and not only_email:
                continue
            assignments[email] = target
            config_svc.record_tier_change(email, target, "recommendation")
            applied.append(f"{email} -> {target}")
            pushed_emails.append((email, target))
        if applied:
            config_svc.save_user_tiers(assignments)
            # Best-effort push, built once for the whole batch (not once per
            # user) -- see _AdminApiTierPusher. Failures for individual users
            # are swallowed here (already logged inside the pusher's own
            # AdminApiClient); this must never undo the local changes above.
            pusher = _AdminApiTierPusher()
            for email, target in pushed_emails:
                pusher.push(email, target)
        return len(applied), applied

    @bp.route("/optimization/user-tier/apply-recommendation", methods=["POST"])
    def apply_recommendation() -> object:
        email = request.form.get("email", "").strip().lower()
        next_url = _safe_next()
        if config_svc.is_tier_editing_locked():
            flash("Tier editing is locked. Unlock it in Settings to change tiers.", "warning")
            return redirect(next_url)
        if not email:
            flash("No user selected.", "warning")
            return redirect(next_url)
        count, applied = _apply_recommendations(only_email=email)
        if count:
            flash(f"Applied recommendation: {applied[0]}.", "success")
        else:
            flash(f"No applicable recommendation for {email}.", "warning")
        return redirect(next_url)

    @bp.route("/optimization/user-tier/apply-all-recommendations", methods=["POST"])
    def apply_all_recommendations() -> object:
        next_url = _safe_next()
        if config_svc.is_tier_editing_locked():
            flash("Tier editing is locked. Unlock it in Settings to change tiers.", "warning")
            return redirect(next_url)
        count, applied = _apply_recommendations()
        if count:
            sample = "; ".join(applied[:5]) + ("; ..." if count > 5 else "")
            flash(f"Applied {count} recommended tier change(s): {sample}", "success")
        else:
            flash("No actionable tier recommendations to apply.", "info")
        return redirect(next_url)

    @bp.route("/optimization/user-tier/reset", methods=["POST"])
    def reset_user_tier() -> object:
        """Restore a user's tier to the value from the imported tierlist.

        Manual tier changes only overwrite ``user_tier_assignments.json``; the
        tierlist import also records ``user_tier_history.json`` (the user's
        groups), so the reset re-picks their HIGHEST-allotment group.
        """
        email = request.form.get("email", "").strip().lower()
        next_url = _safe_next()
        if not email:
            flash("No user selected for tier reset.", "warning")
            return redirect(next_url)

        tiers = tier_caps(config_svc.load_tiers())
        assignments = config_svc.load_user_tiers()
        history = config_svc.load_user_tier_history().get(email, [])
        tierlist_tier = highest_cap_tier(history, tiers) if history else ""

        if tierlist_tier and tierlist_tier in tiers:
            assignments[email] = tierlist_tier
            config_svc.save_user_tiers(assignments)
            config_svc.record_tier_change(email, tierlist_tier, "reset-to-tierlist")
            flash(f"Tier for {email} reset to tierlist value: {tierlist_tier}.", "success")
        elif tierlist_tier:
            assignments.pop(email, None)
            config_svc.save_user_tiers(assignments)
            config_svc.record_tier_change(email, "Baseline", "reset-to-tierlist")
            flash(
                f"Tierlist tier '{tierlist_tier}' for {email} is not in the current "
                f"policy; reset to Baseline default.",
                "warning",
            )
        else:
            assignments.pop(email, None)
            config_svc.save_user_tiers(assignments)
            config_svc.record_tier_change(email, "Baseline", "reset-to-tierlist")
            flash(f"No tierlist entry for {email}; reset to Baseline default.", "success")
        return redirect(next_url)

    @bp.route("/optimization/user-tier/reset-all", methods=["POST"])
    def reset_all_user_tiers() -> object:
        """Discard every manual tier override and rebuild assignments from the
        imported tierlist history (each user's highest-allotment group)."""
        next_url = _safe_next("settings.settings_page")
        tiers = tier_caps(config_svc.load_tiers())
        histories = config_svc.load_user_tier_history()
        previous = config_svc.load_user_tiers()
        rebuilt = {
            email: highest_cap_tier(hist, tiers)
            for email, hist in histories.items()
            if hist and highest_cap_tier(hist, tiers) in tiers
        }
        config_svc.save_user_tiers(rebuilt)
        for email, tier in rebuilt.items():
            if previous.get(email) != tier:
                config_svc.record_tier_change(email, tier, "reset-all-to-tierlist")
        flash(
            f"Reset all tier assignments to the tierlist: {len(rebuilt):,} user(s) "
            f"restored, manual overrides discarded.",
            "success",
        )
        return redirect(next_url)

    @bp.route("/optimization/export.csv", methods=["GET"])
    def optimization_export_csv() -> Response:
        result = _result()
        recommendations, filters = _filter_recommendations(result)
        recommendations, _, _ = _sort_recommendations(recommendations)
        return _csv_response(recommendations, "optimization_recommendations", filters)

    return bp

