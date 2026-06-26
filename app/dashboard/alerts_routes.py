"""Alerts page, notifications, and alert-rule routes on the `main` blueprint.

The page manages alert *rules*; the outlier records they match are shown by the
advanced outlier search (analytics.user_cards_page?mode=advanced).
"""
from __future__ import annotations

from flask import flash, jsonify, redirect, render_template, request, url_for

from app.shared.alert_rules import AlertRule
from app.shared.alerts import compute_alerts, evaluate_rules


def register_alerts_routes(bp, services) -> None:
    store = services.store
    config_svc = services.config_svc

    @bp.route("/notifications", methods=["GET"])
    def notifications_page() -> str:
        # nav_alerts is supplied by the app-wide context processor.
        return render_template("notifications.html")

    @bp.route("/alerts/read", methods=["POST"])
    def mark_alerts_read() -> object:
        """Persist which navbar alerts the user has read. Body: {"all": true}
        or {"ids": [...]}. Returns the resulting unread count + severity so the
        client can repaint the badge without a reload."""
        payload = request.get_json(silent=True) or {}
        active = compute_alerts(services)
        active_ids = {a["id"] for a in active}
        ids = active_ids if payload.get("all") else \
            [i for i in (payload.get("ids") or []) if i in active_ids]
        read = config_svc.mark_read_alerts(ids, active_ids)
        unread_levels = [a["level"] for a in active if a["id"] not in read]
        sev = ("danger" if "danger" in unread_levels
               else "warning" if "warning" in unread_levels else "info")
        return jsonify({"unread_count": len(unread_levels), "sev": sev})

    @bp.route("/alerts", methods=["GET"])
    def alerts_page() -> str:
        """Alert-rule management. The outlier records themselves are shown by the
        advanced outlier search (analytics.user_cards_page?mode=advanced)."""
        df = store.data.df

        all_models_list: list[str] = (
            sorted(df["usage_type_model"].dropna().unique().tolist())
            if "usage_type_model" in df.columns else []
        )
        all_types_list: list[str] = (
            sorted(df["usage_type_parsed_type"].dropna().unique().tolist())
            if "usage_type_parsed_type" in df.columns else []
        )

        # Custom alert rules + their current trigger status
        alert_rules = config_svc.load_alert_rules()
        rule_hits = {
            a["id"].split("rule:", 1)[-1]: a["detail"]
            for a in evaluate_rules(df, alert_rules)
        }

        return render_template(
            "alerts.html",
            all_models_list=all_models_list,
            all_types_list=all_types_list,
            alert_rules=alert_rules,
            rule_hits=rule_hits,
        )

    @bp.route("/alerts/rules/add", methods=["POST"])
    def add_alert_rule() -> object:
        rules = config_svc.load_alert_rules()
        try:
            # from_dict validates/normalizes (metric, numeric coercion, id).
            rules.append(AlertRule.from_dict({
                "name": request.form.get("name", ""),
                "metric": request.form.get("metric", "per_user_window"),
                "threshold": request.form.get("threshold"),
                "window_days": request.form.get("window_days"),
                "usage_type": request.form.get("usage_type", ""),
                "model": request.form.get("model", ""),
                "enabled": True,
            }))
            config_svc.save_alert_rules(rules)
            flash("Alert rule added.", "success")
        except (ValueError, TypeError) as exc:
            flash(f"Could not add rule: {exc}", "danger")
        return redirect(url_for("main.alerts_page"))

    @bp.route("/alerts/rules/delete/<rule_id>", methods=["POST"])
    def delete_alert_rule(rule_id: str) -> object:
        rules = [r for r in config_svc.load_alert_rules() if r.id != rule_id]
        config_svc.save_alert_rules(rules)
        flash("Alert rule removed.", "success")
        return redirect(url_for("main.alerts_page"))

    @bp.route("/alerts/rules/toggle/<rule_id>", methods=["POST"])
    def toggle_alert_rule(rule_id: str) -> object:
        rules = config_svc.load_alert_rules()
        for r in rules:
            if r.id == rule_id:
                r.enabled = not r.enabled  # attribute assignment (Mapping is read-only)
        config_svc.save_alert_rules(rules)
        return redirect(url_for("main.alerts_page"))
