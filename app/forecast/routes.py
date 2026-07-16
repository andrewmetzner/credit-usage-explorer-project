from __future__ import annotations

import copy

import pandas as _pd
import json

from flask import Blueprint, Response, flash, jsonify, redirect, render_template, request, stream_with_context, url_for

from app.shared.chart_data import usage_type_weekly_json
from app.shared.credit_ledger import sync_credit_ledger
from app.shared.csv_export import csv_response, range_slug
from .models import PriceModel
from .prediction import get_model
from .service import ChartDataBuilder, ForecastingService


def create_forecast_blueprint(services) -> Blueprint:
    pipeline = services.pipeline
    config_svc = services.config_svc
    store = services.store
    bp = Blueprint("forecast", __name__, template_folder="templates", url_prefix="")

    def _get_store_df():
        return store.data.df if store is not None else None

    def _latest_data_date(*frames) -> _pd.Timestamp:
        """Return the newest date actually present in the uploaded data."""
        candidates: list[_pd.Timestamp] = []
        for df, col in frames:
            if df is None or df.empty or col not in df.columns:
                continue
            dates = _pd.to_datetime(df[col], errors="coerce").dropna()
            if not dates.empty:
                candidates.append(dates.max().normalize())
        return max(candidates) if candidates else _pd.Timestamp("today").normalize()

    def _snapshot_row(ts):
        """The forecast_history row for a snapshot timestamp, or None."""
        ts = (ts or "").strip()
        if not ts:
            return None
        for row in pipeline.get_forecast_history(limit=1000):
            if str(row.get("snapshot_ts") or "") == ts:
                return row
        return None

    def _snapshot_status_forecast(row):
        """Reconstruct the frozen ContractStatus / Forecast dicts from a saved
        snapshot row (every field was stored as a column), coerced back to the
        dataclass field types so the page's number displays format correctly.
        These drive the KPI cards in snapshot mode; the chart stays live."""
        import dataclasses

        from app.forecast.service import ContractStatus, Forecast

        def coerce(dc):
            out = {}
            for f in dataclasses.fields(dc):
                raw = row.get(f.name, "")
                ftype = str(f.type)
                if raw in (None, "", "nan", "None"):
                    out[f.name] = (0.0 if ftype == "float" else 0 if ftype == "int"
                                   else False if ftype == "bool" else "")
                elif ftype == "float":
                    try:
                        out[f.name] = float(raw)
                    except (TypeError, ValueError):
                        out[f.name] = 0.0
                elif ftype == "int":
                    try:
                        out[f.name] = int(float(raw))
                    except (TypeError, ValueError):
                        out[f.name] = 0
                elif ftype == "bool":
                    out[f.name] = str(raw).strip().lower() in ("true", "1", "yes")
                else:
                    out[f.name] = raw
            return out

        cs, fc = coerce(ContractStatus), coerce(Forecast)
        # Always recompute from the exact date rather than trusting a stored
        # forecast_exhaustion_week: older snapshots (saved before the "week
        # that STARTS exhausted" fix) have one on file, just computed with the
        # old floor-to-same-Monday rule, so only backfilling when absent would
        # leave those showing the wrong week forever.
        if fc.get('forecast_exhaustion_date'):
            try:
                from datetime import datetime, timedelta
                ed = datetime.fromisoformat(fc['forecast_exhaustion_date']).date()
                days_to_next_monday = (7 - ed.weekday()) % 7
                fc['forecast_exhaustion_week'] = (ed + timedelta(days=days_to_next_monday)).isoformat()
            except Exception:
                fc['forecast_exhaustion_week'] = fc.get('forecast_exhaustion_week') or fc['forecast_exhaustion_date']
        return cs, fc

    def _apply_view_window(svc, data_as_of, exclude_partial):
        """Configure a ForecastingService for the current request's date view.

        Windows arrive via the query string:

          * ``view_from`` / ``view_to`` — a manual "date-range view" that
            rewinds the page: caps the actual line / remaining at ``view_to``
            and anchors the projection there. (Snapshot mode does NOT use this
            — it's a lightweight live-page chart preset, applied client-side.)
          * ``data_from`` / ``data_to`` — the narrower legacy "burn window" that
            only limits which weeks drive the burn RATE (Advanced settings).

        Returns the resolved values the caller threads into the template/chart.
        """
        def _arg(name):
            return request.args.get(name, "").strip() or None

        view_from = _arg("view_from")
        view_to = _arg("view_to")
        data_from, data_to = _arg("data_from"), _arg("data_to")
        burn_from = data_from or view_from
        burn_to = data_to or view_to

        vt = _pd.to_datetime(view_to, errors="coerce") if view_to else _pd.NaT
        effective_as_of = min(data_as_of, vt.normalize()) if not _pd.isna(vt) else data_as_of
        effective_today = vt.normalize() if not _pd.isna(vt) else _pd.Timestamp.today().normalize()
        svc._as_of = effective_as_of
        svc._today = effective_today

        # Cap operational data at the as-of date. A rewound view (view_to set)
        # ALWAYS caps — both granularities — so daily mode rewinds too (else it
        # keeps current weeks and computes today's remaining, not the view's).
        # On the live page it's just the weekly partial-week exclusion.
        if (view_to or exclude_partial) and svc.operational_df is not None and not svc.operational_df.empty:
            svc.operational_df = svc.operational_df[
                svc.operational_df["week_end"] <= effective_as_of
            ].copy()

        if (burn_from or burn_to) and svc.operational_df is not None and not svc.operational_df.empty:
            win = svc.operational_df.copy()
            if burn_from:
                win = win[win["week_start"] >= _pd.to_datetime(burn_from)]
            if burn_to:
                win = win[win["week_end"] <= _pd.to_datetime(burn_to)]
            if not win.empty:
                svc._forecast_op_df = win

        # Rewound to a past as-of: the credit pool must reflect only what had
        # been granted by then. Clamp the service's own config ledger so
        # get_contract_status() computes the historical remaining (otherwise it
        # would subtract usage from the CURRENT purchased total, incl. later
        # grants — the bug that made a past view's headline numbers wrong).
        if view_to:
            from app.shared.credit_ledger import credit_entries_as_of, credit_entries_total

            contract = svc.config.setdefault("contract", {})
            entries = credit_entries_as_of(contract, effective_as_of)
            contract["credit_entries"] = entries
            contract["purchased_credits"] = credit_entries_total(entries)

        return {
            "view_from": view_from, "view_to": view_to,
            "data_from": data_from, "data_to": data_to,
            "as_of": effective_as_of, "today": effective_today,
        }

    def _config_as_of(config: dict, as_of: object) -> dict:
        """A config copy whose credit ledger only includes entries effective
        by `as_of` — so a historical/backfilled snapshot doesn't count
        credits that hadn't been granted yet as of its own date. Used
        wherever a ForecastingService is built to reconstruct the past
        (weekly-snapshot backfill, snapshot resync)."""
        from app.shared.credit_ledger import credit_entries_as_of, credit_entries_total

        cfg = copy.deepcopy(config)
        contract = cfg.setdefault("contract", {})
        entries = credit_entries_as_of(contract, as_of)
        contract["credit_entries"] = entries
        contract["purchased_credits"] = credit_entries_total(entries)
        return cfg

    def _credit_events(contract_start, as_of=None) -> list[dict]:
        """Credit-ledger entries as chart events, each clamped to contract start.

        effective_date is when the entry starts counting toward "available";
        undated or pre-contract entries count from contract start. ``as_of``
        drops entries effective after that date, so a past/snapshot view only
        shows grants that had actually happened by then."""
        from app.shared.credit_ledger import credit_kind_label, normalize_credit_entries

        entries = normalize_credit_entries(config_svc.load_contract().get("contract", {}))
        start = _pd.to_datetime(contract_start, errors="coerce")
        cutoff = _pd.to_datetime(as_of, errors="coerce") if as_of is not None else _pd.NaT
        events = []
        for e in entries:
            dt = _pd.to_datetime(e.get("date"), errors="coerce")
            if _pd.isna(dt) or (not _pd.isna(start) and dt < start):
                dt = start
            if _pd.isna(dt):
                continue
            if not _pd.isna(cutoff) and dt > cutoff:
                continue
            events.append({
                "date": str(e.get("date") or ""),
                "effective_date": str(dt.date()),
                "credits": float(e.get("credits") or 0),
                "kind": str(e.get("kind") or "purchased"),
                "label": f"+{float(e.get('credits') or 0):,.0f} {credit_kind_label(e.get('kind'))}",
                "notes": str(e.get("notes") or ""),
            })
        return events

    def _daily_actual_burndown_json(df, contract_status: dict | None, as_of=None) -> str:
        """Daily actual remaining points for the burndown chart.

        Remaining honors the credit ledger: each purchased/gifted entry only
        counts from its recorded date, so a mid-contract grant appears as a
        step up on that day instead of inflating the whole history.

        `as_of` (the newest date present in the uploaded data) bounds the
        series; it can run past latest_usage_date, which follows the weekly
        summaries and lags freshly-merged daily rows."""
        if df is None or df.empty or "date_partition" not in df.columns or "usage_credits" not in df.columns:
            return "[]"
        if not contract_status:
            return "[]"

        start = _pd.to_datetime(contract_status.get("contract_start_date"), errors="coerce")
        end = _pd.to_datetime(as_of, errors="coerce")
        if _pd.isna(end):
            end = _pd.to_datetime(contract_status.get("latest_usage_date"), errors="coerce")
        purchased = float(contract_status.get("purchased_credits") or 0)
        if _pd.isna(start) or _pd.isna(end) or purchased <= 0:
            return "[]"

        ddf = df[["date_partition", "usage_credits"]].copy()
        ddf["_day"] = _pd.to_datetime(ddf["date_partition"], errors="coerce").dt.normalize()
        ddf["usage_credits"] = _pd.to_numeric(ddf["usage_credits"], errors="coerce").fillna(0.0)
        ddf = ddf.dropna(subset=["_day"])
        ddf = ddf[(ddf["_day"] >= start.normalize()) & (ddf["_day"] <= end.normalize())]
        if ddf.empty:
            return "[]"

        daily = ddf.groupby("_day", as_index=False)["usage_credits"].sum()
        full_days = _pd.DataFrame({"_day": _pd.date_range(start.normalize(), end.normalize(), freq="D")})
        daily = full_days.merge(daily, on="_day", how="left").fillna({"usage_credits": 0.0})
        # Credits available as of each day = sum of ledger entries dated <= day
        # (only entries that had happened by the view's as-of date).
        available = _pd.Series(0.0, index=daily.index)
        for ev in _credit_events(contract_status.get("contract_start_date"), as_of=end):
            ev_day = _pd.to_datetime(ev["effective_date"])
            available = available + (daily["_day"] >= ev_day) * ev["credits"]
        daily["remaining"] = (available - daily["usage_credits"].cumsum()).clip(lower=0.0)
        rows = [
            {"date": str(row["_day"].date()), "remaining": round(float(row["remaining"]), 2)}
            for _, row in daily.iterrows()
        ]
        return json.dumps(rows)

    def _has_stat(row: dict, keys: tuple[str, ...]) -> bool:
        for key in keys:
            val = row.get(key)
            if val is None:
                continue
            sval = str(val).strip().lower()
            if sval and sval not in {"nan", "none", "null"}:
                return True
        return False

    def _has_ml_stats(row: dict) -> bool:
        # Force regeneration for snapshots made before the stabilized ML model;
        # otherwise their saved series can show flat/implausible trend lines.
        if str(row.get("ml_model_version", "")) != "stabilized_v2":
            return False
        return _has_stat(row, ("ml_r_squared", "ml_slope_per_week", "ml_p50_end_balance"))

    def _has_mc_stats(row: dict) -> bool:
        return _has_stat(row, ("mc_exhaustion_prob", "mc_p50_end_balance", "mc_runs"))

    def _build_forecast_context(template_name: str) -> str:
        cost_per_credit = float(request.args.get("cost_per_credit", 0) or 0)
        available_credits = float(request.args.get("available_credits", 0) or 0)
        total_credit_cost = float(request.args.get("total_credit_cost", 0) or 0)

        config = config_svc.load_contract()

        is_preview = False
        preview_keys = [
            "contract_start_date", "contract_end_date", "purchased_credits",
            "price_per_credit", "forecast_mode",
            "historical_weight", "latest_week_weight", "recent_average_weight",
        ]
        for key in preview_keys:
            if request.args.get(key):
                is_preview = True
                if key == "contract_start_date":
                    config["contract"]["contract_start_date"] = request.args.get(key)
                elif key == "contract_end_date":
                    config["contract"]["contract_end_date"] = request.args.get(key)
                elif key == "purchased_credits":
                    config["contract"]["purchased_credits"] = float(request.args.get(key))
                elif key == "price_per_credit":
                    config["pricing"]["current_price_per_credit"] = float(request.args.get(key))
                elif key == "forecast_mode":
                    config["forecast"]["mode"] = request.args.get(key)
                elif key in ("historical_weight", "latest_week_weight", "recent_average_weight"):
                    config["forecast"][key] = float(request.args.get(key))

        hist_df = pipeline.get_historical_weekly_summary()
        op_df = pipeline.get_operational_weekly_summary()
        daily_fallback_df = _get_store_df()
        daily_fallback = daily_fallback_df if op_df is None else None

        forecasting = ForecastingService(config, hist_df, op_df, daily_fallback)

        granularity = request.args.get("granularity", "weekly")
        if granularity not in {"weekly", "daily"}:
            granularity = "weekly"
        exclude_partial = granularity == "weekly"
        data_as_of = _latest_data_date(
            (daily_fallback_df, "date_partition"),
            (op_df, "week_end"),
            (hist_df, "period_end"),
        )
        # "Snapshot mode" is a lightweight client preset on the LIVE page — it
        # cuts the actual at the snapshot's date, overlays that snapshot, and
        # hides the live forecasts, keeping the normal window/axes. The server
        # just renders the live page and passes the snapshot id/date so the
        # client can apply the preset; it does NOT rewind the page.
        snapshot_ts = request.args.get("snapshot", "").strip() or None
        snapshot_row = _snapshot_row(snapshot_ts) if snapshot_ts else None
        snapshot_as_of = str(snapshot_row.get("latest_usage_date") or "").strip() if snapshot_row else None
        snapshot_label = (
            (snapshot_row.get("label") or snapshot_row.get("snapshot_date") or "").strip()
            if snapshot_row else None
        )
        if not snapshot_row:
            snapshot_ts = snapshot_as_of = snapshot_label = None

        # Resolve the manual date-range view (view_from/view_to) and legacy burn
        # window (data_from/data_to). Snapshot mode does NOT drive this — it
        # stays on the live window so its X/Y match the normal forecast.
        win = _apply_view_window(forecasting, data_as_of, exclude_partial)
        data_from, data_to = win["data_from"], win["data_to"]
        view_from, view_to = win["view_from"], win["view_to"]
        effective_as_of = win["as_of"]
        effective_today = win["today"]

        usage_type_weekly = usage_type_weekly_json(_get_store_df())

        # A manual date-range view rewinds the page to a past window, so it
        # behaves like preview (no auto-save; snapshots filtered to the window).
        # Snapshot mode does NOT — it's a live-page preset, so it keeps live
        # data/snapshots (but still shouldn't auto-save while pinned to one).
        view_active = bool(view_from or view_to)
        window_preview = is_preview or view_active or bool(snapshot_ts)
        all_snapshots = pipeline.get_forecast_history(limit=1000)

        def _history_in_window():
            if not view_active:
                return all_snapshots
            lo = str(view_from) if view_from else ""
            hi = str(effective_as_of.date())
            return [r for r in all_snapshots if lo <= str(r.get("snapshot_date", "")) <= hi]

        if not forecasting.has_data():
            return render_template(
                template_name,
                no_data=True,
                price_model=PriceModel(cost_per_credit, available_credits, total_credit_cost),
                contract_status=None,
                forecast=None,
                weekly_chart_data="[]",
                daily_actual_data="[]",
                today=str(effective_today.date()),
                credit_events="[]",
                cumulative_chart_data="[]",
                active_users_data="[]",
                usage_type_weekly=usage_type_weekly,
                pipeline_status=pipeline.status(),
                forecast_history=[],
                is_preview=False,
                saved_contract=config_svc.load_contract(),
                exclude_partial=exclude_partial,
                granularity=granularity,
                data_from=data_from,
                data_to=data_to,
                view_from=view_from,
                view_to=view_to,
                all_snapshots=all_snapshots,
                snapshot_ts=snapshot_ts,
                snapshot_label=snapshot_label,
                snapshot_as_of=snapshot_as_of,
            )

        contract_status = forecasting.get_contract_status()
        forecast_data = forecasting.get_forecast()

        auto_save_mode = config.get("forecast", {}).get("snapshot_auto_save", "daily")
        if not window_preview and auto_save_mode in ("daily", "both"):
            forecasting.save_to_dir(pipeline.processed_dir)

        chart_builder = ChartDataBuilder(forecasting, forecasting.historical_df, forecasting.operational_df)
        weekly_chart_data = chart_builder.weekly_burn_json()
        # A rewound view is weekly-based (snapshots are), so its actual line
        # uses the weekly reconstruction (which ends exactly at the view's
        # remaining) rather than the daily one (which includes partial-week
        # usage past the last complete week and wouldn't line up with the
        # snapshot's frozen forecast anchor). Suppressing the daily series makes
        # the chart fall back to the weekly actual in either granularity.
        daily_actual_data = (
            "[]" if view_active
            else _daily_actual_burndown_json(daily_fallback_df, contract_status, effective_as_of)
        )
        # In a past/snapshot view, only show grants that had happened by then.
        events_as_of = effective_as_of if view_active else None
        credit_events = json.dumps(_credit_events(contract_status.get("contract_start_date"), as_of=events_as_of))
        cumulative_chart_data = chart_builder.cumulative_burn_json()
        contract_start_str = str(contract_status.get("contract_start_date", ""))
        active_users_data = chart_builder.active_users_json(contract_start_str)

        # Snapshot mode: the KPI cards (status, credit position, burn, MC/ML)
        # show the snapshot's FROZEN numbers, while the chart/island keep LIVE
        # values so the window and cut-and-overlay match the live forecast.
        display_cs, display_fc = contract_status, forecast_data
        snapshot_stats = None
        if snapshot_row:
            display_cs, display_fc = _snapshot_status_forecast(snapshot_row)
            def _val(key, cast=float):
                raw = snapshot_row.get(key)
                if raw in (None, "", "nan", "None", "null"):
                    return None
                try:
                    return cast(raw)
                except (TypeError, ValueError):
                    return None
            def _raw(key):
                raw = snapshot_row.get(key)
                return None if raw in (None, "", "nan", "None", "null") else raw

            snapshot_stats = {
                "mc_exhaustion_prob": _val("mc_exhaustion_prob"),
                "mc_p50_end_balance": _val("mc_p50_end_balance"),
                "mc_p10_end_balance": _val("mc_p10_end_balance"),
                "mc_p90_end_balance": _val("mc_p90_end_balance"),
                "mc_runs": _val("mc_runs", int),
                "ml_slope_per_week": _val("ml_slope_per_week"),
                "ml_r_squared": _raw("ml_r_squared"),
                "ml_rmse": _val("ml_rmse"),
                "ml_observations_used": _val("ml_observations_used", int),
                "ml_model_engine": _raw("ml_model_engine"),
                "ml_trend_direction": _raw("ml_trend_direction"),
                "ml_projected_exhaustion_date": _raw("ml_projected_exhaustion_date"),
                "ml_p10_end_balance": _val("ml_p10_end_balance"),
                "ml_p50_end_balance": _val("ml_p50_end_balance"),
                "ml_p90_end_balance": _val("ml_p90_end_balance"),
            }

        return render_template(
            template_name,
            no_data=False,
            price_model=PriceModel(cost_per_credit, available_credits, total_credit_cost),
            contract_status=display_cs,
            forecast=display_fc,
            live_contract_status=contract_status,
            live_forecast=forecast_data,
            snapshot_stats=snapshot_stats,
            weekly_chart_data=weekly_chart_data,
            daily_actual_data=daily_actual_data,
            today=str(effective_today.date()),
            credit_events=credit_events,
            cumulative_chart_data=cumulative_chart_data,
            active_users_data=active_users_data,
            usage_type_weekly=usage_type_weekly,
            pipeline_status=pipeline.status(),
            forecast_history=_history_in_window(),
            is_preview=window_preview,
            saved_contract=config_svc.load_contract(),
            exclude_partial=exclude_partial,
            granularity=granularity,
            data_from=data_from,
            data_to=data_to,
            view_from=view_from,
            view_to=view_to,
            all_snapshots=all_snapshots,
            snapshot_ts=snapshot_ts,
            snapshot_label=snapshot_label,
            snapshot_as_of=snapshot_as_of,
        )

    @bp.route("/forecast", methods=["GET"])
    def forecast_page() -> str:
        if "granularity" not in request.args:
            args = dict(request.args)
            args["granularity"] = "weekly"
            args.pop("exclude_partial", None)
            return redirect(url_for("forecast.forecast_page", **args))
        return _build_forecast_context("forecast.html")

    @bp.route("/forecast/save-config", methods=["POST"])
    def save_forecast_config() -> object:
        contract = config_svc.load_contract()

        if request.form.get("contract_start_date"):
            contract["contract"]["contract_start_date"] = request.form.get("contract_start_date")
        if request.form.get("contract_end_date"):
            contract["contract"]["contract_end_date"] = request.form.get("contract_end_date")
        if request.form.get("purchased_credits"):
            contract["contract"]["purchased_credits"] = float(request.form.get("purchased_credits"))
        if request.form.get("purchased_credits_date"):
            contract["contract"]["purchased_credits_date"] = request.form.get("purchased_credits_date")
        if request.form.get("rollover_allowed"):
            contract["contract"]["rollover_allowed"] = request.form.get("rollover_allowed") == "on"
        if request.form.get("current_price_per_credit"):
            contract["pricing"]["current_price_per_credit"] = float(request.form.get("current_price_per_credit"))
        if request.form.get("next_contract_price_per_credit"):
            contract["pricing"]["next_contract_price_per_credit"] = float(request.form.get("next_contract_price_per_credit"))
        if request.form.get("forecast_mode"):
            contract["forecast"]["mode"] = request.form.get("forecast_mode")
        if request.form.get("recent_average_window_weeks"):
            contract["forecast"]["recent_average_window_weeks"] = int(request.form.get("recent_average_window_weeks"))
        if request.form.get("minimum_weeks_for_recent_average"):
            contract["forecast"]["minimum_weeks_for_recent_average"] = int(request.form.get("minimum_weeks_for_recent_average"))
        if request.form.get("normalize_weights"):
            contract["forecast"]["normalize_weights"] = request.form.get("normalize_weights") == "on"

        for wkey in ("historical_weight", "latest_week_weight", "recent_average_weight"):
            if request.form.get(wkey):
                w = request.form.get(wkey)
                contract["forecast"][wkey] = float(w) if w else None

        if request.form.get("snapshot_auto_save"):
            contract["forecast"]["snapshot_auto_save"] = request.form.get("snapshot_auto_save")

        if request.form.get("monte_carlo_runs"):
            try:
                contract["forecast"]["monte_carlo_runs"] = max(100, min(20000, int(request.form.get("monte_carlo_runs"))))
            except (ValueError, TypeError):
                pass

        sync_credit_ledger(contract["contract"])
        config_svc.save_contract(contract)
        flash("Forecast config saved.", "success")
        next_page = request.form.get("next_page", "settings")
        if next_page == "forecast":
            return redirect(url_for("forecast.forecast_page"))
        return redirect(url_for("settings.settings_page"))

    @bp.route("/forecast/snapshot-settings", methods=["POST"])
    def save_snapshot_settings() -> object:
        contract = config_svc.load_contract()
        contract["forecast"]["snapshot_auto_save"] = request.form.get("snapshot_auto_save", "daily")
        config_svc.save_contract(contract)
        flash("Snapshot auto-save setting updated.", "success")
        return redirect(url_for("forecast.forecast_page"))

    @bp.route("/forecast/history/delete", methods=["POST"])
    def delete_forecast_snapshot() -> object:
        snapshot_ts = request.form.get("snapshot_ts", "")
        snapshot_date = request.form.get("snapshot_date", "")
        label = request.form.get("label", "")
        ok, err = pipeline.delete_snapshot(snapshot_ts, snapshot_date, label)
        if not ok:
            return (err or "Delete failed.", 500)
        return ("", 204)

    @bp.route("/forecast/history/delete-all", methods=["POST"])
    def delete_all_forecast_snapshots() -> object:
        count = pipeline.delete_all_snapshots()
        if count:
            flash(f"Deleted all {count} snapshot{'s' if count != 1 else ''}.", "success")
        else:
            flash("No snapshots to delete.", "info")
        return redirect(url_for("forecast.forecast_page"))

    @bp.route("/forecast/snapshot", methods=["POST"])
    def save_forecast_snapshot() -> object:
        config = copy.deepcopy(config_svc.load_contract())

        if request.form.get("snap_purchased_credits"):
            config["contract"]["purchased_credits"] = float(request.form["snap_purchased_credits"])
        if request.form.get("snap_contract_start_date"):
            config["contract"]["contract_start_date"] = request.form["snap_contract_start_date"]
        if request.form.get("snap_contract_end_date"):
            config["contract"]["contract_end_date"] = request.form["snap_contract_end_date"]
        if request.form.get("snap_forecast_mode"):
            config["forecast"]["mode"] = request.form["snap_forecast_mode"]
        for wkey in ("historical_weight", "recent_average_weight", "latest_week_weight"):
            val = request.form.get(f"snap_{wkey}", "").strip()
            if val:
                config["forecast"][wkey] = float(val)

        hist_df = pipeline.get_historical_weekly_summary()
        op_df = pipeline.get_operational_weekly_summary()
        daily_fallback_df = _get_store_df()
        daily_fallback = daily_fallback_df if op_df is None else None
        svc = ForecastingService(config, hist_df, op_df, daily_fallback)
        label = request.form.get("snapshot_label", "").strip()

        if svc.has_data():
            svc.save_to_dir(pipeline.processed_dir, once_per_day=False, label=label)
            flash("Snapshot saved.", "success")
        else:
            flash("No data available to snapshot.", "warning")

        next_page = request.form.get("next_page", "forecast")
        if next_page == "settings":
            return redirect(url_for("settings.settings_page"))
        return redirect(url_for("forecast.forecast_page"))

    @bp.route("/forecast/snapshot/resync", methods=["POST"])
    def resync_forecast_snapshots() -> object:
        """Recompute every saved snapshot against the CURRENT credit ledger.

        Snapshots freeze their numbers the moment they're saved. If a credit
        entry is added afterward — even one backdated to an earlier effective
        date — every snapshot that already existed keeps the old, now-stale
        total forever; nothing else in the app ever revisits them. This walks
        every snapshot and rebuilds it in place (same timestamp/date/label/
        color), using today's ledger but that snapshot's OWN historical data
        window (only the weeks known by its own as-of date), so "what the
        burn history looked like then" stays fixed while "what the ledger
        actually contained by then" gets corrected.
        """
        config = copy.deepcopy(config_svc.load_contract())
        hist_df = pipeline.get_historical_weekly_summary()
        op_df = pipeline.get_operational_weekly_summary()
        if op_df is None or op_df.empty:
            daily_df = _get_store_df()
            if daily_df is not None and not daily_df.empty:
                _tmp = ForecastingService(config, hist_df, None, daily_df)
                if _tmp.operational_df is not None and not _tmp.operational_df.empty:
                    op_df = _tmp.operational_df
                if hist_df is None and _tmp.historical_df is not None:
                    hist_df = _tmp.historical_df

        rows = pipeline.get_forecast_history(limit=1000)
        if not rows:
            flash("No snapshots to resync.", "info")
            return redirect(request.referrer or url_for("settings.settings_page"))
        if op_df is None or op_df.empty:
            flash("No operational data available to rebuild snapshots from.", "warning")
            return redirect(request.referrer or url_for("settings.settings_page"))

        # Cap Monte Carlo like the other batch-generation routes so resyncing
        # many snapshots at once stays responsive.
        cfg_fc = config.setdefault("forecast", {})
        cfg_fc["monte_carlo_runs"] = min(int(cfg_fc.get("monte_carlo_runs", 10000)), 2000)

        op_sorted = op_df.sort_values("week_start").reset_index(drop=True)
        resynced = skipped = errors = 0
        for row in rows:
            ts = str(row.get("snapshot_ts") or "").strip()
            if not ts:
                # No timestamp to preserve identity by — resyncing would mint
                # a new one and silently orphan this row under a new key.
                skipped += 1
                continue
            snap_date = str(row.get("snapshot_date") or "").strip()
            label = str(row.get("label") or "").strip()
            color = str(row.get("color") or "").strip()
            # Two different "as of" axes: how much USAGE DATA had accumulated
            # (latest_usage_date — can lag behind the real calendar), vs. what
            # the CREDIT LEDGER actually held (as of the snapshot's own real
            # or simulated wall-clock date, snapshot_date). A live snapshot
            # saved today, with data lagging a few days, must still count a
            # grant effective yesterday — clamping the ledger to the lagging
            # data date would wrongly erase it.
            data_as_of = _pd.to_datetime(row.get("latest_usage_date"), errors="coerce")
            ledger_as_of = _pd.to_datetime(snap_date, errors="coerce")

            truncated_op = (
                op_sorted[op_sorted["week_end"] <= data_as_of] if not _pd.isna(data_as_of) else op_sorted
            )
            if truncated_op.empty:
                errors += 1
                continue

            try:
                pipeline.delete_snapshot(ts, snap_date, label)
                # Ledger entries dated after this snapshot's own date must not
                # count yet — only what the pool actually held then.
                week_config = _config_as_of(config, ledger_as_of if not _pd.isna(ledger_as_of) else data_as_of)
                svc = ForecastingService(week_config, hist_df, truncated_op.copy())
                if not svc.has_data():
                    errors += 1
                    continue
                svc.save_to_dir(
                    pipeline.processed_dir,
                    once_per_day=False,
                    label=label,
                    snapshot_ts=ts,
                    snapshot_date=snap_date,
                    skip_mc=False,
                    research_role_initial=str(row.get("research_role_initial") or ""),
                    research_role_final=str(row.get("research_role_final") or ""),
                )
                if color:
                    pipeline.set_snapshot_color(ts, snap_date, label, color)
                resynced += 1
            except Exception:
                errors += 1

        parts = [f"{resynced} snapshot{'s' if resynced != 1 else ''} resynced"]
        if skipped:
            parts.append(f"{skipped} skipped (no timestamp)")
        if errors:
            parts.append(f"{errors} failed")
        flash(", ".join(parts) + ".", "success" if resynced else "warning")
        return redirect(request.referrer or url_for("settings.settings_page"))

    @bp.route("/forecast/history/rename", methods=["POST"])
    def rename_forecast_snapshot() -> object:
        snapshot_ts = request.form.get("snapshot_ts", "")
        snapshot_date = request.form.get("snapshot_date", "")
        old_label = request.form.get("old_label", "")
        new_label = request.form.get("new_label", "").strip()
        ok, err = pipeline.rename_snapshot(snapshot_ts, snapshot_date, old_label, new_label)
        if not ok:
            return (err or "Rename failed.", 500)
        return ("", 204)

    @bp.route("/forecast/snapshot/series")
    def get_snapshot_series() -> object:
        snapshot_ts = request.args.get("ts", "")
        if not snapshot_ts:
            return jsonify({"error": "ts parameter required"}), 400
        data = pipeline.get_snapshot_series(snapshot_ts)
        if data is None:
            return jsonify({"error": "No series data for this snapshot"}), 404
        return jsonify(data)

    @bp.route("/forecast/history/color", methods=["POST"])
    def set_snapshot_color() -> object:
        snapshot_ts = request.form.get("snapshot_ts", "")
        snapshot_date = request.form.get("snapshot_date", "")
        label = request.form.get("label", "")
        color = request.form.get("color", "")
        ok, err = pipeline.set_snapshot_color(snapshot_ts, snapshot_date, label, color)
        if not ok:
            return (err or "Color update failed.", 500)
        return ("", 204)

    @bp.route("/forecast/history/set-roles", methods=["POST"])
    def set_snapshot_roles() -> object:
        initial_ts = request.form.get("research_role_initial", "").strip()
        final_ts = request.form.get("research_role_final", "").strip()

        if initial_ts:
            ok, err = pipeline.set_snapshot_roles(snapshot_ts=initial_ts, initial=True)
            if not ok:
                flash(err or "Failed to set initial forecast snapshot.", "warning")
        else:
            ok, err = pipeline.clear_snapshot_role("research_role_initial")
            if not ok:
                flash(err or "Failed to clear initial forecast selection.", "warning")

        if final_ts:
            ok, err = pipeline.set_snapshot_roles(snapshot_ts=final_ts, final=True)
            if not ok:
                flash(err or "Failed to set end forecast snapshot.", "warning")
        else:
            ok, err = pipeline.clear_snapshot_role("research_role_final")
            if not ok:
                flash(err or "Failed to clear end forecast selection.", "warning")

        if not initial_ts and not final_ts:
            flash("Cleared research snapshot bookends.", "success")
        else:
            flash("Research snapshot bookends updated.", "success")
        return redirect(request.referrer or url_for("analytics.storyboard"))

    @bp.route("/forecast/snapshot/generate-weekly", methods=["POST"])
    def generate_weekly_snapshots() -> object:
        config = copy.deepcopy(config_svc.load_contract())
        hist_df = pipeline.get_historical_weekly_summary()
        op_df = pipeline.get_operational_weekly_summary()

        # If no pipeline operational data, derive weekly summaries from the daily store data
        if (op_df is None or op_df.empty) and store is not None:
            daily_df = _get_store_df()
            if daily_df is not None and not daily_df.empty:
                _tmp = ForecastingService(config, hist_df, None, daily_df)
                if _tmp.operational_df is not None and not _tmp.operational_df.empty:
                    op_df = _tmp.operational_df
                if hist_df is None and _tmp.historical_df is not None:
                    hist_df = _tmp.historical_df

        if op_df is None or op_df.empty:
            flash("No data available. Upload a data sheet on the Summary page first.", "warning")
            return redirect(url_for("forecast.forecast_page"))

        # Monte Carlo is optional for weekly batches (it's the slow part). When
        # requested, cap the run count so generating many weeks stays responsive.
        include_mc = request.form.get("include_mc") == "1"
        if include_mc:
            cfg_fc = config.setdefault("forecast", {})
            cfg_fc["monte_carlo_runs"] = min(int(cfg_fc.get("monte_carlo_runs", 10000)), 2000)

        existing_by_label = {h.get("label", ""): h for h in pipeline.get_forecast_history()}
        op_sorted = op_df.sort_values("week_start").reset_index(drop=True)
        generated = skipped = errors = 0

        for i in range(len(op_sorted)):
            row = op_sorted.iloc[i]
            week_start_str = str(_pd.Timestamp(row["week_start"]).date())
            label = f"Week of {week_start_str}"
            snap_ts = f"{week_start_str}T00:00:00"

            existing = existing_by_label.get(label)
            if existing:
                needs_ml = not _has_ml_stats(existing)
                needs_mc = include_mc and not _has_mc_stats(existing)
                if not needs_ml and not needs_mc:
                    skipped += 1
                    continue
                # Regenerate old snapshots that are missing ML/MC statistics so
                # chart overlays and comparison cards have complete model data.
                try:
                    pipeline.delete_snapshot(
                        existing.get("snapshot_ts", snap_ts),
                        existing.get("snapshot_date", week_end_str),
                        existing.get("label", label),
                    )
                except Exception:
                    pass

            # Build a service that only sees data through this week — and a
            # ledger that only counts credits granted by this week, so a
            # later-dated grant (even a real, current one) doesn't leak into
            # a week that predates it.
            truncated_op = op_sorted.iloc[: i + 1].copy()
            try:
                week_config = _config_as_of(config, week_end_str)
                svc = ForecastingService(week_config, hist_df, truncated_op)
                if not svc.has_data():
                    errors += 1
                    continue
                svc.save_to_dir(
                    pipeline.processed_dir,
                    once_per_day=False,
                    label=label,
                    snapshot_ts=snap_ts,
                    snapshot_date=week_end_str,
                    skip_mc=not include_mc,
                )
                existing_by_label[label] = {"label": label, "snapshot_ts": snap_ts, "snapshot_date": week_end_str}
                generated += 1
            except Exception:
                errors += 1

        parts = []
        if generated:
            parts.append(f"{generated} snapshot{'s' if generated != 1 else ''} generated")
        if skipped:
            parts.append(f"{skipped} already existed")
        if errors:
            parts.append(f"{errors} failed")

        level = "success" if generated else ("info" if skipped else "warning")
        flash(", ".join(parts) + "." if parts else "Nothing to generate.", level)
        return redirect(url_for("forecast.forecast_page"))

    @bp.route("/forecast/snapshot/generating")
    def snapshot_generating_page() -> object:
        return render_template("snap_generate.html")

    @bp.route("/forecast/snapshot/generate-all-stream")
    def generate_all_snapshots_stream() -> object:
        def _stream():
            config = copy.deepcopy(config_svc.load_contract())
            hist_df = pipeline.get_historical_weekly_summary()
            op_df = pipeline.get_operational_weekly_summary()
            daily_df = _get_store_df()

            if (op_df is None or op_df.empty) and daily_df is not None and not daily_df.empty:
                _tmp = ForecastingService(config, hist_df, None, daily_df)
                if _tmp.operational_df is not None and not _tmp.operational_df.empty:
                    op_df = _tmp.operational_df
                if hist_df is None and _tmp.historical_df is not None:
                    hist_df = _tmp.historical_df

            if op_df is None or op_df.empty:
                yield f"data: {json.dumps({'error': 'No data available. Upload data first.'})}\n\n"
                return

            cfg_fc = config.setdefault("forecast", {})
            cfg_fc["monte_carlo_runs"] = min(int(cfg_fc.get("monte_carlo_runs", 10000)), 1000)

            op_sorted = op_df.sort_values("week_start").reset_index(drop=True)
            total = len(op_sorted)
            existing_by_label = {h.get("label", ""): h for h in pipeline.get_forecast_history()}
            generated = skipped = errors = 0

            for i in range(total):
                row = op_sorted.iloc[i]
                week_start_str = str(_pd.Timestamp(row["week_start"]).date())
                label = f"Week of {week_start_str}"
                snap_ts = f"{week_start_str}T00:00:00"
                pct = int((i + 1) / total * 100)
                yield f"data: {json.dumps({'progress': pct, 'current': i + 1, 'total': total, 'week': week_end_str})}\n\n"

                existing = existing_by_label.get(label)
                if existing:
                    needs_ml = not _has_ml_stats(existing)
                    needs_mc = not _has_mc_stats(existing)
                    if not needs_ml and not needs_mc:
                        skipped += 1
                        continue
                    try:
                        pipeline.delete_snapshot(
                            existing.get("snapshot_ts", snap_ts),
                            existing.get("snapshot_date", week_end_str),
                            existing.get("label", label),
                        )
                    except Exception:
                        pass

                truncated_op = op_sorted.iloc[: i + 1].copy()
                try:
                    week_config = _config_as_of(config, week_end_str)
                    svc = ForecastingService(week_config, hist_df, truncated_op)
                    if not svc.has_data():
                        errors += 1
                        continue
                    svc.save_to_dir(
                        pipeline.processed_dir,
                        once_per_day=False,
                        label=label,
                        snapshot_ts=snap_ts,
                        snapshot_date=week_end_str,
                        skip_mc=False,
                    )
                    existing_by_label[label] = {"label": label, "snapshot_ts": snap_ts, "snapshot_date": week_end_str}
                    generated += 1
                except Exception:
                    errors += 1

            yield f"data: {json.dumps({'done': True, 'generated': generated, 'skipped': skipped, 'errors': errors})}\n\n"

        return Response(
            stream_with_context(_stream()),
            content_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _forecasting_from_request(config: dict, exclude_partial: bool = True) -> ForecastingService:
        """A ForecastingService configured the way the page sees it: lag-aware
        as-of/today anchors, optional partial-week exclusion, and the date-range
        view / burn window from the current request's query args — so exports
        (details, model-data) match whatever window the page is showing."""
        hist_df = pipeline.get_historical_weekly_summary()
        op_df = pipeline.get_operational_weekly_summary()
        daily_fallback_df = _get_store_df()
        svc = ForecastingService(
            config, hist_df, op_df, daily_fallback_df if op_df is None else None
        )
        data_as_of = _latest_data_date(
            (daily_fallback_df, "date_partition"),
            (op_df, "week_end"),
            (hist_df, "period_end"),
        )
        _apply_view_window(svc, data_as_of, exclude_partial)
        return svc

    @bp.route("/forecast/details-export.csv", methods=["GET"])
    def forecast_details_export_csv() -> object:
        """The Details accordion as CSV — contract & pacing, forecast, and
        model statistics for the current view (burn window respected)."""
        svc = _forecasting_from_request(config_svc.load_contract())
        if not svc.has_data():
            return Response("No data available", status=404, mimetype="text/plain")
        cs = svc.get_contract_status()
        fc = svc.get_forecast()
        label = lambda k: k.replace("_", " ").capitalize()  # noqa: E731
        rows = [("Contract & pacing", label(k), v) for k, v in {**cs}.items()]
        rows += [("Forecast", label(k), v) for k, v in {**fc}.items()]
        try:
            ml = svc._run_ml(cs, fc).metadata or {}
            rows += [
                ("Linear trend (ML)", label(k), v) for k, v in ml.items()
                if k not in ("weekly_predictions", "raw_weekly_predictions")
            ]
        except Exception:
            pass
        try:
            mc = svc._run_mc(cs, fc).metadata or {}
            rows += [("Monte Carlo", label(k), v) for k, v in mc.items()]
        except Exception:
            pass
        return csv_response(
            _pd.DataFrame(rows, columns=["Section", "Metric", "Value"]),
            "forecast_details.csv",
            filters=[
                ("window", range_slug(request.args.get("data_from", "").strip(),
                                      request.args.get("data_to", "").strip())),
            ],
        )

    @bp.route("/forecast/snapshot/export.csv", methods=["GET"])
    def forecast_snapshot_series_export_csv() -> object:
        """One saved snapshot's full time series as CSV: the actual burndown
        as known that week (it ends at the snapshot's data) plus the forecast
        and MC/ML bands that were computed from it."""
        ts = request.args.get("ts", "").strip()
        series = pipeline.get_snapshot_series(ts) if ts else None
        if not series:
            return Response("Snapshot series not found", status=404, mimetype="text/plain")

        cols: dict[str, dict[str, object]] = {}

        def add(name: str, pts, value_key: str = "value") -> None:
            for p in pts or []:
                day = str(p.get("date", ""))[:10]
                if day:
                    cols.setdefault(day, {})[name] = p.get(value_key)

        add("Actual remaining", series.get("actual_burndown"), "remaining")
        add("Forecast remaining", series.get("forecast_burndown"), "remaining")
        mc = series.get("mc") or {}
        add("MC P10", mc.get("p10"))
        add("MC P50", mc.get("p50"))
        add("MC P90", mc.get("p90"))
        ml = series.get("ml") or {}
        add("ML P10", ml.get("p10"))
        add("ML P50", ml.get("p50"))
        add("ML P90", ml.get("p90"))

        names = ["Actual remaining", "Forecast remaining",
                 "MC P10", "MC P50", "MC P90", "ML P10", "ML P50", "ML P90"]
        rows = [{"Date": d, **{n: cols[d].get(n, "") for n in names}} for d in sorted(cols)]
        return csv_response(
            _pd.DataFrame(rows, columns=["Date"] + names),
            "forecast_snapshot_series.csv",
            filters=[
                ("week", str(series.get("snapshot_date", ""))),
                ("label", str(series.get("label", ""))),
            ],
        )

    @bp.route("/forecast/history/export.csv", methods=["GET"])
    def forecast_history_export_csv() -> object:
        """Snapshot history as CSV. Repeated ``key`` args (snapshot_ts, or
        "date|label" for legacy rows) narrow it to the selected snapshots —
        each row is that week's full forecast details."""
        rows = pipeline.get_forecast_history(limit=1000)
        keys = set(request.args.getlist("key"))

        def row_key(h: dict) -> str:
            return h.get("snapshot_ts") or f"{h.get('snapshot_date', '')}|{h.get('label', '')}"

        if keys:
            rows = [h for h in rows if row_key(h) in keys]
        return csv_response(
            _pd.DataFrame(rows),
            "forecast_snapshots.csv",
            filters=[("selected", len(keys) if keys else "")],
        )

    @bp.route("/forecast/model-data", methods=["GET"])
    def model_data() -> object:
        model_id = request.args.get("model", "monte_carlo")
        granularity = request.args.get("granularity", "weekly")
        if granularity not in {"weekly", "daily"}:
            granularity = "weekly"
        exclude_partial = granularity == "weekly"

        config = config_svc.load_contract()
        cfg_runs = int(config.get("forecast", {}).get("monte_carlo_runs", 10000))
        try:
            runs = min(int(request.args.get("runs", cfg_runs) or cfg_runs), 20000)
        except (ValueError, TypeError):
            runs = cfg_runs
        svc = _forecasting_from_request(config, exclude_partial)
        if not svc.has_data():
            return jsonify({"error": "No data available"}), 404

        cs = svc.get_contract_status()
        fc = svc.get_forecast()

        try:
            ctx = svc.build_forecast_context(cs, fc)
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid latest_usage_date"}), 500

        try:
            model = get_model(model_id, runs=runs)
        except (ValueError, TypeError):
            return jsonify({"error": f"Unknown model: {model_id!r}"}), 400

        try:
            result = model.run(ctx)
        except Exception as exc:
            return jsonify({"error": f"Model run failed: {exc}"}), 500
        return jsonify(result.to_json_dict())

    return bp
