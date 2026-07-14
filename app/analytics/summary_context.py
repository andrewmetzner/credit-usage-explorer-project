"""Per-user context for the user-summary page.

Builds everything on that page that is *about the user* rather than about the
filtered records table: their optimization recommendation, tier history and
Codex badge, narrative stories, month-pace history, currently-triggering
alerts, and the dated tier-change log. Extracted from the (previously ~900
line) analytics route so the pieces are testable and the route reads as
orchestration.

Every section is defensive: a failure in one (e.g. the optimization build)
degrades that section to empty instead of breaking the page.
"""
from __future__ import annotations

import pandas as pd


def build_user_summary_context(
    services,
    full_df: pd.DataFrame,
    user_scope_df: pd.DataFrame,
    email: str,
    name: str,
) -> dict:
    config_svc = services.config_svc
    ctx: dict = {
        "optimization_user": None,
        "optimization_history": [],
        "optimization_tier_history": [],
        "optimization_tierlist_tier": "",
        "optimization_tier_moves": [],
        "optimization_source": "",
        "optimization_assigned_tier_count": 0,
        "has_codex_access": False,
        "user_stories": [],
        "user_month_history": [],
        "user_triggered_alerts": [],
        "user_tier_changes": [],
        "cap_change_date": "",
    }

    gov = services.governance
    tier_history_key = (email or "").strip().lower()

    def resolve_tier_history(key: str) -> None:
        """Tier history + Codex badge: cheap JSON lookups, filled in first so
        they still render even if the heavier optimization build fails."""
        from app.optimization.service import highest_cap_tier

        history = config_svc.load_user_tier_history().get(key, [])
        ctx["optimization_tier_history"] = history
        # The tierlist value a reset restores: the highest-allotment group.
        ctx["optimization_tierlist_tier"] = (
            highest_cap_tier(history, gov.weekly_caps()) if history else ""
        )
        ctx["has_codex_access"] = gov.has_codex_access(key)
        ctx["optimization_tier_moves"] = [
            {
                "previous_tier": history[idx - 1],
                "new_tier": history[idx],
                "is_current": idx == len(history) - 1,
            }
            for idx in range(1, len(history))
            if history[idx - 1] != history[idx]
        ] if len(history) > 1 else []

    try:
        if tier_history_key:
            resolve_tier_history(tier_history_key)
    except Exception:
        pass

    # ── Optimization recommendation + weekly cap history ────────────────────
    try:
        from app.optimization.service import build_optimization_result

        opt = build_optimization_result(full_df, gov.tier_config(), gov.resolved_assignments())
        ctx["optimization_source"] = opt.source_label
        ctx["optimization_assigned_tier_count"] = len(config_svc.load_user_tiers())

        def match_user(frame: pd.DataFrame) -> pd.DataFrame:
            if frame is None or frame.empty:
                return pd.DataFrame()
            if email and "email" in frame.columns:
                return frame[frame["email"].astype(str).str.lower() == email.lower()]
            if name and "latest_name" in frame.columns:
                return frame[frame["latest_name"].astype(str).str.contains(name, case=False, na=False, regex=False)]
            return pd.DataFrame()

        matches = match_user(opt.recommendations)
        if not matches.empty:
            ctx["optimization_user"] = matches.iloc[0].fillna("").to_dict()

        if not tier_history_key and ctx["optimization_user"]:
            tier_history_key = str(ctx["optimization_user"].get("email", "")).strip().lower()
            if tier_history_key:
                resolve_tier_history(tier_history_key)

        hist = match_user(opt.user_week_history)
        if not hist.empty:
            ctx["optimization_history"] = (
                hist.sort_values("week_start", ascending=False)
                .head(12)
                .fillna("")
                .to_dict(orient="records")
            )
    except Exception:
        pass

    # ── Stories, month-pace history, this user's triggering alerts ──────────
    try:
        from app.analytics.stories import build_month_pace_history, build_user_stories, evaluate_story_rules
        from app.shared.alerts import evaluate_rules

        tcfg = gov.tier_config()
        ctx["cap_change_date"] = str(tcfg.get("cap_period_change_date", "") or "")
        user_tier = "Baseline"
        if ctx["optimization_user"]:
            user_tier = str(ctx["optimization_user"].get("latest_governance_tier") or "Baseline")
        # Reference "now" for recency = the most recent date across all data.
        reference_date = None
        if "date_partition" in full_df.columns:
            reference_date = pd.to_datetime(full_df["date_partition"], errors="coerce").max()

        ctx["user_stories"] = build_user_stories(
            user_scope_df, tcfg, user_tier, reference_date=reference_date
        )
        ctx["user_month_history"] = build_month_pace_history(user_scope_df, tcfg, user_tier)

        # This user's currently-triggering alerts (custom rules + story rules),
        # evaluated against just their own activity, so the conditions shown
        # are actually about them (not an org-wide rule that happens to fire).
        if email:
            key = email.strip().lower()
            monthly_cap = gov.monthly_cap_for(user_tier)
            triggered = list(evaluate_rules(user_scope_df, config_svc.load_alert_rules()))
            story_rules_for_user = [
                r for r in config_svc.load_story_alert_rules()
                if not str(r.get("email", "")).strip()
                or str(r.get("email", "")).strip().lower() == key
            ]
            triggered += evaluate_story_rules(
                user_scope_df, story_rules_for_user, {key: monthly_cap}, monthly_cap, reference_date,
                cap_change_date=ctx["cap_change_date"],
            )
            ctx["user_triggered_alerts"] = triggered
    except Exception:
        ctx["user_stories"] = []
        ctx["user_month_history"] = []
        ctx["user_triggered_alerts"] = []

    # ── Dated tier-change log (oldest-first, append-only) ───────────────────
    try:
        ctx["user_tier_changes"] = list(
            config_svc.load_tier_change_log().get((email or "").strip().lower(), [])
        )
    except Exception:
        ctx["user_tier_changes"] = []

    return ctx
