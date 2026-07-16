from __future__ import annotations

import os
import stat
from pathlib import Path

from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from app.shared.credit_ledger import (
    build_credit_entry,
    credit_entries_total,
    credit_kind_label,
    normalize_credit_entries,
    sync_credit_ledger,
)
from app.optimization.service import DEFAULT_WEEKS_PER_MONTH, raw_tier_cap
from app.shared.ingestion import _infer_week_from_filename
from app.shared.tier_import import read_tier_assignments_csv
from .service import force_rmtree, try_snapshot

ALLOWED_HISTORICAL = {".xlsx", ".xls", ".csv"}
ALLOWED_WEEKLY = {".csv"}
ALLOWED_TIERLIST = {".csv"}


def create_settings_blueprint(services) -> Blueprint:
    pipeline = services.pipeline
    config_svc = services.config_svc
    store = services.store
    bp = Blueprint("settings", __name__, template_folder="templates", url_prefix="/settings")

    @bp.route("", methods=["GET"])
    def settings_page() -> str:
        saved_contract = config_svc.load_contract()
        credit_entries = normalize_credit_entries(saved_contract.get("contract", {}))
        credit_total = credit_entries_total(credit_entries)
        saved_contract.setdefault("contract", {})
        saved_contract["contract"]["credit_entries"] = credit_entries
        saved_contract["contract"]["purchased_credits"] = credit_total

        credit_status = None
        try:
            from app.forecast.service import ForecastingService

            svc = ForecastingService(saved_contract, pipeline.get_historical_weekly_summary(), pipeline.get_operational_weekly_summary(), store.data.df)
            if svc.has_data():
                credit_status = svc.get_contract_status()
        except Exception:
            credit_status = None

        tiers = config_svc.load_tiers()
        user_tiers = config_svc.load_user_tiers()
        user_tier_counts: dict[str, int] = {}
        for tier in user_tiers.values():
            user_tier_counts[tier] = user_tier_counts.get(tier, 0) + 1

        # Manual overrides = users whose current tier differs from the value a
        # "reset to tierlist" would restore (their last imported tier, if any).
        tier_histories = config_svc.load_user_tier_history()
        tier_overrides = []
        for email, current in sorted(user_tiers.items()):
            history = tier_histories.get(email, [])
            tierlist_tier = history[-1] if history else ""
            if current != tierlist_tier:
                tier_overrides.append({
                    "email": email,
                    "current_tier": current,
                    "tierlist_tier": tierlist_tier,
                })
        pipeline_status = pipeline.status()
        ingested_weeks = pipeline.get_ingested_weeks()
        forecast_history = sorted(
            pipeline.get_forecast_history(limit=1000),
            key=lambda r: str(r.get("snapshot_date") or ""),
        )
        forecast_history_count = len(forecast_history)
        upload_history = pipeline.get_upload_history()
        return render_template(
            "settings.html",
            saved_contract=saved_contract,
            credit_entries=credit_entries,
            credit_total=credit_total,
            credit_status=credit_status,
            credit_kind_label=credit_kind_label,
            tiers=tiers,
            tier_editing_locked=config_svc.is_tier_editing_locked(),
            tier_overrides=tier_overrides,
            user_tier_count=len(user_tiers),
            user_tier_counts=dict(sorted(user_tier_counts.items())),
            pipeline_status=pipeline_status,
            ingested_weeks=ingested_weeks,
            forecast_history=forecast_history,
            forecast_history_count=forecast_history_count,
            upload_history=upload_history,
        )

    @bp.route("/contract", methods=["POST"])
    def update_contract() -> object:
        try:
            ws_min = request.form.getlist("ws_min[]")
            ws_max = request.form.getlist("ws_max[]")
            ws_hist = request.form.getlist("ws_hist[]")
            ws_recent = request.form.getlist("ws_recent[]")
            ws_latest = request.form.getlist("ws_latest[]")

            auto_weight_schedule = []
            for i in range(len(ws_min)):
                row: dict = {"min_operational_weeks": int(ws_min[i]) if ws_min[i] else 0}
                if i < len(ws_max) and ws_max[i].strip():
                    row["max_operational_weeks"] = int(ws_max[i])
                else:
                    row["max_operational_weeks"] = None
                row["historical_weight"] = float(ws_hist[i]) if i < len(ws_hist) and ws_hist[i].strip() else None
                row["recent_average_weight"] = float(ws_recent[i]) if i < len(ws_recent) and ws_recent[i].strip() else None
                row["latest_week_weight"] = float(ws_latest[i]) if i < len(ws_latest) and ws_latest[i].strip() else None
                auto_weight_schedule.append(row)

            data = config_svc.load_contract()
            data.setdefault("contract", {})
            data["contract"].update({
                "contract_start_date": request.form.get("contract_start_date", ""),
                "contract_end_date": request.form.get("contract_end_date", ""),
                "purchased_credits_date": request.form.get("purchased_credits_date", "").strip()
                    or request.form.get("contract_start_date", "").strip(),
                "rollover_allowed": "rollover_allowed" in request.form,
            })
            data["pricing"] = {
                "current_price_per_credit": float(request.form.get("pricing_current", 0)),
                "next_contract_price_per_credit": float(request.form.get("pricing_next", 0)),
            }
            data["forecast"] = {
                **data.get("forecast", {}),
                "mode": request.form.get("forecast_mode", "auto"),
                "normalize_weights": "forecast_normalize_weights" in request.form,
                "recent_average_window_weeks": int(request.form.get("forecast_recent_window", 4)),
                "minimum_weeks_for_recent_average": int(request.form.get("forecast_min_weeks", 4)),
                "monte_carlo_runs": int(request.form.get("monte_carlo_runs", 10000)),
                "auto_weight_schedule": auto_weight_schedule,
            }
            sync_credit_ledger(data["contract"])
            config_svc.save_contract(data)
            flash("Contract configuration saved.", "success")
        except Exception as exc:
            flash(f"Error saving contract config: {exc}", "danger")
        return redirect(url_for("settings.settings_page"))

    @bp.route("/credits/add", methods=["POST"])
    def add_credit_entry() -> object:
        try:
            amount = float(request.form.get("credits", 0) or 0)
            if amount <= 0:
                flash("Enter a credit amount greater than zero.", "warning")
                return redirect(url_for("settings.settings_page"))

            data = config_svc.load_contract()
            contract = data.setdefault("contract", {})
            entries = normalize_credit_entries(contract)
            entries.append(
                build_credit_entry(
                    date=request.form.get("credits_date", "").strip()
                    or request.form.get("purchased_credits_date", "").strip()
                    or request.form.get("contract_start_date", "").strip()
                    or contract.get("contract_start_date", ""),
                    credits=amount,
                    kind=request.form.get("credit_kind", "purchased"),
                    notes=request.form.get("credits_notes", "").strip(),
                )
            )
            contract["credit_entries"] = entries
            sync_credit_ledger(contract)
            config_svc.save_contract(data)
            flash("Credit entry added.", "success")
        except Exception as exc:
            flash(f"Error adding credit entry: {exc}", "danger")
        return redirect(url_for("settings.settings_page"))

    @bp.route("/credits/remove", methods=["POST"])
    def remove_credit_entry() -> object:
        try:
            entry_id = request.form.get("entry_id", "").strip()
            if not entry_id:
                flash("No credit entry selected for removal.", "warning")
                return redirect(url_for("settings.settings_page"))

            data = config_svc.load_contract()
            contract = data.setdefault("contract", {})
            entries = normalize_credit_entries(contract)
            kept = [e for e in entries if str(e.get("id", "")) != entry_id]
            if len(kept) == len(entries):
                flash("Could not find that credit entry.", "warning")
                return redirect(url_for("settings.settings_page"))

            contract["credit_entries"] = kept
            if not kept:
                contract["purchased_credits"] = 0
            sync_credit_ledger(contract)
            config_svc.save_contract(data)
            flash("Credit entry removed.", "success")
        except Exception as exc:
            flash(f"Error removing credit entry: {exc}", "danger")
        return redirect(url_for("settings.settings_page"))

    @bp.route("/tiers", methods=["POST"])
    def update_tiers() -> object:
        try:
            names = request.form.getlist("tier_name[]")
            caps = request.form.getlist("tier_cap[]")
            tiers_dict: dict = {}
            for name, cap in zip(names, caps):
                name = name.strip()
                if name:
                    tiers_dict[name] = {"credit_cap": int(float(cap))}
            # Preserve non-tier settings (e.g. the editing lock) already on file.
            cfg = config_svc.load_tiers()
            # Merge instead of replace so per-tier YAML comments survive the save.
            existing_tiers = cfg.get("tiers")
            if isinstance(existing_tiers, dict):
                config_svc._merge_into_commented(existing_tiers, tiers_dict)
            else:
                cfg["tiers"] = tiers_dict
            # How the caps above are interpreted: weekly or monthly (mutable).
            period = str(request.form.get("cap_period", "weekly")).strip().lower()
            cfg["cap_period"] = "monthly" if period == "monthly" else "weekly"
            # Monthly->weekly divisor: the real weeks in each month, or a fixed number.
            if str(request.form.get("weeks_per_month_mode", "")).strip().lower() == "actual":
                cfg["weeks_per_month"] = "actual"
            else:
                try:
                    wpm = float(request.form.get("weeks_per_month", DEFAULT_WEEKS_PER_MONTH) or 0)
                except (TypeError, ValueError):
                    wpm = 0.0
                cfg["weeks_per_month"] = wpm or DEFAULT_WEEKS_PER_MONTH
            # Date the workspace switched weekly -> monthly caps (display marker only).
            change_date = str(request.form.get("cap_period_change_date", "")).strip()
            if change_date:
                cfg["cap_period_change_date"] = change_date
            else:
                cfg.pop("cap_period_change_date", None)
            config_svc.save_tiers(cfg)
            flash("Tier policy saved.", "success")
        except Exception as exc:
            flash(f"Error saving tier policy: {exc}", "danger")
        return redirect(url_for("settings.settings_page"))

    @bp.route("/tiers/lock", methods=["POST"])
    def set_tier_lock() -> object:
        try:
            locked = "editing_locked" in request.form
            config_svc.set_tier_editing_locked(locked)
            if locked:
                flash("Tier editing locked. Per-user tier changes are now disabled.", "success")
            else:
                flash("Tier editing unlocked. Per-user tier changes are allowed.", "success")
        except Exception as exc:
            flash(f"Error updating tier lock: {exc}", "danger")
        return redirect(url_for("settings.settings_page"))

    @bp.route("/tiers/reset-import", methods=["POST"])
    def reset_tierlist_data() -> object:
        """Wipe all tierlist-derived data -- assignments, undated history,
        Codex-access flags, AND the dated tier_change_log (so the Tier Changes/
        Stories history is cleared too) -- so a CSV can be re-uploaded from a
        completely clean slate. This is destructive and cannot be undone."""
        try:
            cleared = len(config_svc.load_user_tiers())
            config_svc.save_user_tiers({})
            config_svc.save_user_tier_history({})
            config_svc.save_user_codex_access({})
            config_svc.save_tier_change_log({})
            flash(
                f"Tierlist data cleared ({cleared:,} user assignment(s) removed), "
                "including dated Tier Changes/Stories history. Re-upload a CSV to repopulate.",
                "success",
            )
        except Exception as exc:
            flash(f"Error clearing tierlist data: {exc}", "danger")
        return redirect(url_for("settings.settings_page"))

    @bp.route("/tiers/import", methods=["POST"])
    def import_tier_assignments() -> object:
        if "file" not in request.files:
            flash("No tierlist file provided.", "danger")
            return redirect(url_for("settings.settings_page"))

        file = request.files["file"]
        if not file.filename:
            flash("No tierlist file selected.", "danger")
            return redirect(url_for("settings.settings_page"))

        suffix = Path(file.filename).suffix.lower()
        if suffix not in ALLOWED_TIERLIST:
            flash(f"Invalid tierlist file type '{suffix}'. Must be .csv.", "danger")
            return redirect(url_for("settings.settings_page"))

        try:
            # Caps let the import pick each user's HIGHEST-allotment group as
            # their tier when they belong to several.
            from app.optimization.service import tier_caps as _tier_caps

            result = read_tier_assignments_csv(
                file.stream, tier_caps=_tier_caps(config_svc.load_tiers())
            )
            if not result.assignments:
                flash("No tier assignments were found in that CSV.", "warning")
                return redirect(url_for("settings.settings_page"))

            replace_existing = request.form.get("import_mode") == "replace"
            assignments = {} if replace_existing else config_svc.load_user_tiers()
            assignments.update(result.assignments)
            config_svc.save_user_tiers(assignments)
            histories = {} if replace_existing else config_svc.load_user_tier_history()
            histories.update(result.histories)
            config_svc.save_user_tier_history(histories)
            codex_access = {} if replace_existing else config_svc.load_user_codex_access()
            codex_access.update(result.codex_access)
            config_svc.save_user_codex_access(codex_access)
            # Dated log of "as of this submission, the user's tier history was
            # X" — distinct from the undated histories above (which get
            # overwritten each import). Only tiers not already recorded get a
            # new dated entry, stamped with today (the import date), since a
            # tierlist row never carries real per-transition dates.
            for imp_email, imp_history in result.histories.items():
                config_svc.sync_tier_history_to_log(imp_email, imp_history, "import")

            tier_cfg = config_svc.load_tiers()
            tiers = tier_cfg.setdefault("tiers", {})
            # Seed new tiers using stored cap numbers as-is (same period as the
            # rest of the file); raw_tier_cap reads whichever field is present.
            baseline_cap = raw_tier_cap(tiers.get("Baseline")) or 100
            cap_overrides = {
                "Advanced Credit Users": raw_tier_cap(tiers.get("Advanced")) or 400,
                "High Credit Consumption Users": raw_tier_cap(tiers.get("Super")) or 750,
                "One K Credit Users": raw_tier_cap(tiers.get("Highest")) or 1000,
                "Emergency Credit Users": raw_tier_cap(tiers.get("Highest")) or 1000,
            }
            new_tiers = []
            for tier in sorted(set(result.assignments.values())):
                if tier not in tiers:
                    tiers[tier] = {"credit_cap": int(cap_overrides.get(tier, baseline_cap))}
                    new_tiers.append(tier)
            if new_tiers:
                config_svc.save_tiers(tier_cfg)

            mode = "replaced" if replace_existing else "merged"
            message = (
                f"Tierlist imported: {result.imported_rows:,} assignments {mode} "
                f"from {result.rows:,} rows using '{result.email_column}' and '{result.tier_column}'."
            )
            if result.skipped_rows:
                message += f" Skipped {result.skipped_rows:,} row(s) missing email or tier."
            if new_tiers:
                message += f" Added {len(new_tiers):,} new tier label(s) with the Baseline cap."
            flash(message, "success")
        except Exception as exc:
            flash(f"Error importing tierlist: {exc}", "danger")
        return redirect(url_for("settings.settings_page"))

    @bp.route("/upload/historical", methods=["POST"])
    def upload_historical() -> object:
        from config import HISTORICAL_DIR

        if "file" not in request.files:
            flash("No file provided.", "danger")
            return redirect(url_for("settings.settings_page"))

        file = request.files["file"]
        if not file.filename:
            flash("No file selected.", "danger")
            return redirect(url_for("settings.settings_page"))

        suffix = Path(file.filename).suffix.lower()
        if suffix not in ALLOWED_HISTORICAL:
            flash(f"Invalid file type '{suffix}'. Must be .xlsx, .xls, or .csv.", "danger")
            return redirect(url_for("settings.settings_page"))

        filename = secure_filename(file.filename)
        saved_path = HISTORICAL_DIR / filename
        file.save(str(saved_path))

        try:
            stats = pipeline.ingest_historical(saved_path)
            flash(
                f"Historical data ingested: {stats['rows']:,} rows, "
                f"{stats['weeks']} weeks, {stats['users']} users, "
                f"{stats['total_credits']:,.2f} total credits.",
                "success",
            )
            try_snapshot(pipeline, config_svc, f"Upload: {file.filename}")
        except Exception as exc:
            flash(f"Error ingesting historical data: {exc}", "danger")

        return redirect(url_for("settings.settings_page"))

    @bp.route("/upload/weekly", methods=["POST"])
    def upload_weekly() -> object:
        from config import UPLOADS_DIR

        if "file" not in request.files:
            flash("No file provided.", "danger")
            return redirect(url_for("settings.settings_page"))

        file = request.files["file"]
        if not file.filename:
            flash("No file selected.", "danger")
            return redirect(url_for("settings.settings_page"))

        suffix = Path(file.filename).suffix.lower()
        if suffix not in ALLOWED_WEEKLY:
            flash(f"Invalid file type '{suffix}'. Must be .csv.", "danger")
            return redirect(url_for("settings.settings_page"))

        inferred_start, inferred_end = _infer_week_from_filename(Path(file.filename))

        filename = secure_filename(file.filename)
        saved_path = UPLOADS_DIR / filename
        file.save(str(saved_path))

        week_start = request.form.get("week_start", "").strip() or inferred_start or None
        week_end = request.form.get("week_end", "").strip() or inferred_end or None

        try:
            stats = pipeline.ingest_weekly(saved_path, week_start, week_end)
            flash(
                f"Weekly data ingested: week {stats['week_start']} to {stats['week_end']}, "
                f"{stats['rows']:,} rows, {stats['unique_users']} users, "
                f"{stats['total_credits']:,.2f} credits.",
                "success",
            )
            try_snapshot(pipeline, config_svc, f"Upload: {file.filename}")
        except Exception as exc:
            flash(f"Error ingesting weekly data: {exc}", "danger")

        return redirect(url_for("settings.settings_page"))

    @bp.route("/delete/historical", methods=["POST"])
    def delete_historical() -> object:
        deleted = pipeline.delete_historical()
        if deleted:
            flash("Historical data deleted.", "success")
        else:
            flash("No historical data found to delete.", "warning")
        return redirect(url_for("settings.settings_page"))

    @bp.route("/delete/week/<week_start_str>", methods=["POST"])
    def delete_week(week_start_str: str) -> object:
        deleted = pipeline.delete_week(week_start_str)
        if deleted:
            flash(f"Week starting {week_start_str} deleted.", "success")
        else:
            flash(f"Week starting {week_start_str} not found.", "danger")
        return redirect(url_for("settings.settings_page"))

    @bp.route("/export-data", methods=["GET"])
    def export_data() -> object:
        if store is None or not store.path.exists():
            flash("No data file available to export.", "warning")
            return redirect(url_for("settings.settings_page"))
        return send_file(
            store.path,
            as_attachment=True,
            download_name=store.path.name,
        )

    @bp.route("/clear-all", methods=["POST"])
    def clear_all_data() -> object:
        from config import (
            CURRENT_DATA_PATH,
            CURRENT_DATA_PATH_CACHE,
            DEFAULT_DATA_PATH,
            HISTORICAL_DIR,
            PROCESSED_DIR,
            UPLOADS_DIR,
        )

        try:
            if PROCESSED_DIR.exists():
                force_rmtree(PROCESSED_DIR)
            PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

            for d in (HISTORICAL_DIR, UPLOADS_DIR):
                if d.exists():
                    force_rmtree(d)
                d.mkdir(parents=True, exist_ok=True)

            for p in CURRENT_DATA_PATH.parent.glob(CURRENT_DATA_PATH.stem + ".*"):
                try:
                    os.chmod(p, stat.S_IWRITE)
                    p.unlink()
                except Exception:
                    pass

            if CURRENT_DATA_PATH_CACHE.exists():
                CURRENT_DATA_PATH_CACHE.unlink()

            if store is not None:
                store.reload(DEFAULT_DATA_PATH)

            flash("All data cleared. Showing default demo data.", "success")
        except Exception as exc:
            flash(f"Error clearing data: {exc}", "danger")

        return redirect(url_for("settings.settings_page"))

    return bp
