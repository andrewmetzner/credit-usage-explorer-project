"""In-app alert computation.

`compute_alerts` evaluates the current data + forecast on each page load and
returns a small list of alert dicts that the navbar bell renders. Everything is
wrapped defensively so a bad/empty dataset can never break page rendering.

Alert dict shape:
    {"id": str,                       # stable per condition (drives read/unread)
     "level": "danger"|"warning"|"info",
     "title": str, "detail": str,
     "link_endpoint": str | None,
     "link_args": dict}               # query args for url_for (deep-link)
"""
from __future__ import annotations

import pandas as pd

# Default weekly per-user burn that counts as a "heavy user" alert.
DEFAULT_OUTLIER_THRESHOLD = 1000.0
STALE_DATA_DAYS = 10


def evaluate_rules(df, rules: list) -> list[dict]:
    """Turn user-defined alert rules into alert dicts.

    Metrics:
      per_record       — single records (prompts) over the threshold
      per_user_day     — a user's single-day total over the threshold
      per_user_window  — a user's total over the window exceeds the threshold
    """
    out: list[dict] = []
    if (
        df is None or df.empty
        or "usage_credits" not in df.columns
        or "date_partition" not in df.columns
    ):
        return out

    dates = pd.to_datetime(df["date_partition"], errors="coerce")
    if not dates.notna().any():
        return out
    max_date = dates.max()
    credits = pd.to_numeric(df["usage_credits"], errors="coerce").fillna(0.0)
    user_col = "email" if "email" in df.columns else None

    for rule in rules:
        if not rule.get("enabled", True):
            continue
        metric = rule.get("metric", "per_user_window")
        threshold = float(rule.get("threshold", 1000))
        window = int(rule.get("window_days", 7))
        name = rule.get("name") or "Alert rule"
        rid = rule.get("id", "rule")
        cutoff = max_date - pd.Timedelta(days=window)

        # Window + optional usage-type / model scope.
        rule_mask = dates >= cutoff
        utype = (rule.get("usage_type") or "").strip()
        rmodel = (rule.get("model") or "").strip()
        if utype and "usage_type_parsed_type" in df.columns:
            rule_mask = rule_mask & (df["usage_type_parsed_type"] == utype)
        if rmodel and "usage_type_model" in df.columns:
            rule_mask = rule_mask & (df["usage_type_model"] == rmodel)

        count = 0
        unit = ""

        if metric == "per_record":
            count = int((credits[rule_mask] > threshold).sum())
            unit = "prompt"
        elif metric == "per_user_day" and user_col:
            sub = pd.DataFrame({
                "u": df.loc[rule_mask, user_col].values,
                "c": credits[rule_mask].values,
                "d": dates[rule_mask].dt.date.values,
            })
            day_sums = sub.groupby(["u", "d"])["c"].sum()
            count = int((day_sums > threshold).sum())
            unit = "user-day"
        elif user_col:  # per_user_window
            metric = "per_user_window"
            user_sums = credits[rule_mask].groupby(df.loc[rule_mask, user_col]).sum()
            count = int((user_sums > threshold).sum())
            unit = "user"

        if count > 0:
            plural = "s" if count != 1 else ""
            scope = (f" on {utype}" if utype else "") + (f" / {rmodel}" if rmodel else "")
            link_args = {"metric": metric, "credit_threshold": int(threshold), "lookback_days": window}
            if utype:
                link_args["usage_type_filter"] = utype
            if rmodel:
                link_args["model_filter"] = rmodel
            out.append({
                "id": f"rule:{rid}",
                "level": "warning",
                "title": f"{name}",
                "detail": f"{count:,} {unit}{plural} over {threshold:,.0f} credits{scope} "
                          f"({cutoff.date()} → {max_date.date()}).",
                # Click-through opens the matching outlier view, pre-filtered.
                "link_endpoint": "main.outliers_page",
                "link_args": link_args,
            })
    return out


def compute_alerts(services) -> list[dict]:
    alerts: list[dict] = []
    config_svc = services.config_svc

    try:
        df = services.store.data.df
    except Exception:
        df = None

    cfg = {}
    try:
        cfg = config_svc.load_contract()
    except Exception:
        cfg = {}

    # 1. Stale data
    if df is not None and not df.empty and "date_partition" in df.columns:
        dates = pd.to_datetime(df["date_partition"], errors="coerce").dropna()
        if not dates.empty:
            latest = dates.max().normalize()
            age = (pd.Timestamp.now().normalize() - latest).days
            if age > STALE_DATA_DAYS:
                alerts.append({
                    "id": "stale-data",
                    "level": "info",
                    "title": "Data may be stale",
                    "detail": f"Latest usage is {age} days old ({latest.date()}). Upload a newer sheet.",
                    "link_endpoint": "main.summary_page",
                    "link_args": {},
                })

    # 2. User-defined alert rules
    try:
        alerts += evaluate_rules(df, config_svc.load_alert_rules())
    except Exception:
        pass

    # 3. Forecast pacing / exhaustion (deterministic only — cheap, no Monte Carlo)
    try:
        svc = services.build_forecasting_service(cfg)
        if svc.has_data():
            cs = svc.get_contract_status()
            fc = svc.get_forecast()
            if fc.get("forecast_status") == "EXHAUSTION_RISK":
                when = fc.get("forecast_exhaustion_date") or "before contract end"
                alerts.append({
                    "id": "exhaustion-risk",
                    "level": "danger",
                    "title": "Exhaustion risk",
                    "detail": f"Credits projected to run out around {when} at the current burn rate.",
                    "link_endpoint": "forecast.forecast_page",
                    "link_args": {},
                })
            if cs.get("pacing_status") == "OVERBURNING":
                alerts.append({
                    "id": "overburning",
                    "level": "warning",
                    "title": "Overburning",
                    "detail": f"Burn pace is {cs.get('burn_pace_ratio', 0):.2f}x the contract pace.",
                    "link_endpoint": "forecast.forecast_page",
                    "link_args": {},
                })
    except Exception:
        pass

    return alerts
