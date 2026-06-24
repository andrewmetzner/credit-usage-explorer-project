"""Outliers, notifications, and alert-rule routes on the `main` blueprint."""
from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for

from app.shared.alert_rules import AlertRule
from app.shared.alerts import evaluate_rules
from .service import OUTLIER_VIEWS, compute_outliers


def register_outliers_routes(bp, services) -> None:
    store = services.store
    config_svc = services.config_svc

    @bp.route("/notifications", methods=["GET"])
    def notifications_page() -> str:
        # nav_alerts is supplied by the app-wide context processor.
        return render_template("notifications.html")

    @bp.route("/outliers", methods=["GET"])
    def outliers_page() -> str:
        df = store.data.df

        metric = request.args.get("metric", "per_user_window").strip()
        if metric not in OUTLIER_VIEWS:
            metric = "per_user_window"
        credit_threshold = float(request.args.get("credit_threshold", 100) or 100)
        model_filter = request.args.get("model_filter", "").strip()
        usage_type_filter = request.args.get("usage_type_filter", "").strip()
        lookback_days = int(request.args.get("lookback_days", 7) or 7)
        start_date = request.args.get("start_date", "").strip()
        end_date = request.args.get("end_date", "").strip()

        all_models_list: list[str] = (
            sorted(df["usage_type_model"].dropna().unique().tolist())
            if "usage_type_model" in df.columns else []
        )
        all_types_list: list[str] = (
            sorted(df["usage_type_parsed_type"].dropna().unique().tolist())
            if "usage_type_parsed_type" in df.columns else []
        )
        outlier_rows, outlier_count, lookback_start_date, lookback_end_date, outlier_columns = compute_outliers(
            df, metric, credit_threshold, lookback_days,
            start_date=start_date, end_date=end_date,
            usage_type_filter=usage_type_filter, model_filter=model_filter,
        )
        use_date_range = bool(start_date or end_date)

        # Custom alert rules + their current trigger status
        alert_rules = config_svc.load_alert_rules()
        rule_hits = {
            a["id"].split("rule:", 1)[-1]: a["detail"]
            for a in evaluate_rules(df, alert_rules)
        }

        return render_template(
            "outliers.html",
            metric=metric,
            outlier_views=OUTLIER_VIEWS,
            outlier_rows=outlier_rows,
            outlier_columns=outlier_columns,
            outlier_count=outlier_count,
            credit_threshold=credit_threshold,
            model_filter=model_filter,
            usage_type_filter=usage_type_filter,
            all_models_list=all_models_list,
            all_types_list=all_types_list,
            lookback_days=lookback_days,
            start_date=start_date,
            end_date=end_date,
            use_date_range=use_date_range,
            lookback_start_date=lookback_start_date,
            lookback_end_date=lookback_end_date,
            alert_rules=alert_rules,
            rule_hits=rule_hits,
        )

    @bp.route("/outliers/rules/add", methods=["POST"])
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
        return redirect(url_for("main.outliers_page"))

    @bp.route("/outliers/rules/delete/<rule_id>", methods=["POST"])
    def delete_alert_rule(rule_id: str) -> object:
        rules = [r for r in config_svc.load_alert_rules() if r.id != rule_id]
        config_svc.save_alert_rules(rules)
        flash("Alert rule removed.", "success")
        return redirect(url_for("main.outliers_page"))

    @bp.route("/outliers/rules/toggle/<rule_id>", methods=["POST"])
    def toggle_alert_rule(rule_id: str) -> object:
        rules = config_svc.load_alert_rules()
        for r in rules:
            if r.id == rule_id:
                r.enabled = not r.enabled  # attribute assignment (Mapping is read-only)
        config_svc.save_alert_rules(rules)
        return redirect(url_for("main.outliers_page"))
