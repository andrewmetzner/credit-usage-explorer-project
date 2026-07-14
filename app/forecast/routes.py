from __future__ import annotations

import copy

import pandas as _pd
import json

from flask import Blueprint, Response, flash, jsonify, redirect, render_template, request, stream_with_context, url_for

from app.shared.chart_data import usage_type_weekly_json
from app.shared.credit_ledger import sync_credit_ledger
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

    def _credit_events(contract_start) -> list[dict]:
        """Credit-ledger entries as chart events, each clamped to contract start.

        effective_date is when the entry starts counting toward "available";
        undated or pre-contract entries count from contract start."""
        from app.shared.credit_ledger import credit_kind_label, normalize_credit_entries

        entries = normalize_credit_entries(config_svc.load_contract().get("contract", {}))
        start = _pd.to_datetime(contract_start, errors="coerce")
        events = []
        for e in entries:
            dt = _pd.to_datetime(e.get("date"), errors="coerce")
            if _pd.isna(dt) or (not _pd.isna(start) and dt < start):
                dt = start
            if _pd.isna(dt):
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
        # Credits available as of each day = sum of ledger entries dated <= day.
        available = _pd.Series(0.0, index=daily.index)
        for ev in _credit_events(contract_status.get("contract_start_date")):
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
        forecasting._as_of = data_as_of
        # Live view: anchor projections at today (lag-aware). Snapshot
        # backfill constructs its own service and leaves this unset.
        forecasting._today = _pd.Timestamp.today().normalize()
        if exclude_partial:
            if forecasting.operational_df is not None and not forecasting.operational_df.empty:
                forecasting.operational_df = forecasting.operational_df[
                    forecasting.operational_df["week_end"] <= data_as_of
                ].copy()

        # Date window for burn-rate calculation (does not affect credits_remaining / weeks_remaining)
        data_from = request.args.get("data_from", "").strip() or None
        data_to   = request.args.get("data_to",   "").strip() or None
        if (data_from or data_to) and forecasting.operational_df is not None and not forecasting.operational_df.empty:
            win = forecasting.operational_df.copy()
            if data_from:
                win = win[win["week_start"] >= _pd.to_datetime(data_from)]
            if data_to:
                win = win[win["week_end"] <= _pd.to_datetime(data_to)]
            if not win.empty:
                forecasting._forecast_op_df = win

        usage_type_weekly = usage_type_weekly_json(_get_store_df())

        if not forecasting.has_data():
            return render_template(
                template_name,
                no_data=True,
                price_model=PriceModel(cost_per_credit, available_credits, total_credit_cost),
                contract_status=None,
                forecast=None,
                weekly_chart_data="[]",
                daily_actual_data="[]",
                today=str(_pd.Timestamp.today().date()),
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
            )

        contract_status = forecasting.get_contract_status()
        forecast_data = forecasting.get_forecast()

        auto_save_mode = config.get("forecast", {}).get("snapshot_auto_save", "daily")
        if not is_preview and auto_save_mode in ("daily", "both"):
            forecasting.save_to_dir(pipeline.processed_dir)

        chart_builder = ChartDataBuilder(forecasting, forecasting.historical_df, forecasting.operational_df)
        weekly_chart_data = chart_builder.weekly_burn_json()
        daily_actual_data = _daily_actual_burndown_json(daily_fallback_df, contract_status, data_as_of)
        credit_events = json.dumps(_credit_events(contract_status.get("contract_start_date")))
        cumulative_chart_data = chart_builder.cumulative_burn_json()
        contract_start_str = str(contract_status.get("contract_start_date", ""))
        active_users_data = chart_builder.active_users_json(contract_start_str)

        return render_template(
            template_name,
            no_data=False,
            price_model=PriceModel(cost_per_credit, available_credits, total_credit_cost),
            contract_status=contract_status,
            forecast=forecast_data,
            weekly_chart_data=weekly_chart_data,
            daily_actual_data=daily_actual_data,
            today=str(_pd.Timestamp.today().date()),
            credit_events=credit_events,
            cumulative_chart_data=cumulative_chart_data,
            active_users_data=active_users_data,
            usage_type_weekly=usage_type_weekly,
            pipeline_status=pipeline.status(),
            forecast_history=pipeline.get_forecast_history(),
            is_preview=is_preview,
            saved_contract=config_svc.load_contract(),
            exclude_partial=exclude_partial,
            granularity=granularity,
            data_from=data_from,
            data_to=data_to,
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
            week_end_str = str(_pd.Timestamp(row["week_end"]).date())
            label = f"Week of {week_end_str}"
            snap_ts = f"{week_end_str}T00:00:00"

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

            # Build a service that only sees data through this week
            truncated_op = op_sorted.iloc[: i + 1].copy()
            try:
                svc = ForecastingService(config, hist_df, truncated_op)
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
                week_end_str = str(_pd.Timestamp(row["week_end"]).date())
                label = f"Week of {week_end_str}"
                snap_ts = f"{week_end_str}T00:00:00"
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
                    svc = ForecastingService(config, hist_df, truncated_op)
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
        hist_df = pipeline.get_historical_weekly_summary()
        op_df = pipeline.get_operational_weekly_summary()
        daily_fallback_df = _get_store_df()
        daily_fallback = daily_fallback_df if op_df is None else None

        svc = ForecastingService(config, hist_df, op_df, daily_fallback)
        data_as_of = _latest_data_date(
            (daily_fallback_df, "date_partition"),
            (op_df, "week_end"),
            (hist_df, "period_end"),
        )
        svc._as_of = data_as_of
        # Same lag-aware anchor as the page, so MC/ML overlays line up with
        # the bridged actual line and the deterministic projection.
        svc._today = _pd.Timestamp.today().normalize()
        if exclude_partial:
            if svc.operational_df is not None and not svc.operational_df.empty:
                svc.operational_df = svc.operational_df[
                    svc.operational_df["week_end"] <= data_as_of
                ].copy()

        data_from_mc = request.args.get("data_from", "").strip() or None
        data_to_mc   = request.args.get("data_to",   "").strip() or None
        if (data_from_mc or data_to_mc) and svc.operational_df is not None and not svc.operational_df.empty:
            win = svc.operational_df.copy()
            if data_from_mc:
                win = win[win["week_start"] >= _pd.to_datetime(data_from_mc)]
            if data_to_mc:
                win = win[win["week_end"] <= _pd.to_datetime(data_to_mc)]
            if not win.empty:
                svc._forecast_op_df = win

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
