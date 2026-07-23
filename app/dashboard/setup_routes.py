"""First-run setup wizard routes (registered onto the `main` blueprint)."""
from __future__ import annotations

from flask import flash, redirect, render_template, request, session, url_for

from app.shared.credit_ledger import normalize_credit_entries


def register_setup_routes(bp, services) -> None:
    store = services.store
    config_svc = services.config_svc

    @bp.route("/setup", methods=["GET"])
    def setup_page() -> str:
        from config import DEFAULT_DATA_PATH
        contract = config_svc.load_contract()
        has_data = store.path != DEFAULT_DATA_PATH and not store.data.df.empty
        return render_template(
            "setup.html",
            contract=contract.get("contract", {}),
            pricing=contract.get("pricing", {}),
            has_config=config_svc.is_contract_configured(),
            has_data=has_data,
            total_records=len(store.data.df),
            data_filename=None if store.path == DEFAULT_DATA_PATH else store.path.name,
        )

    @bp.route("/setup/config", methods=["POST"])
    def setup_save_config() -> object:
        """Creates (or, if the wizard is revisited, updates) the first
        contract. Later contracts are added from Settings' Contracts manager."""
        try:
            contract_start_date = request.form.get("contract_start_date", "").strip()
            contract_end_date = request.form.get("contract_end_date", "").strip()
            purchased_credits = float(request.form.get("purchased_credits", 0) or 0)
            price_per_credit = float(request.form.get("current_price_per_credit", 0) or 0)
            rollover_allowed = "rollover_allowed" in request.form

            contracts = config_svc.load_contracts()
            if contracts:
                first = contracts[0]
                config_svc.update_contract_fields(first["id"], {
                    "contract_start_date": contract_start_date,
                    "contract_end_date": contract_end_date,
                    "price_per_credit": price_per_credit,
                    "rollover_allowed": rollover_allowed,
                })
                if not normalize_credit_entries(first) and purchased_credits > 0:
                    config_svc.add_credit_entry(
                        first["id"],
                        date=contract_start_date,
                        credits=purchased_credits,
                        kind="purchased",
                        notes="Initial allocation",
                    )
            else:
                config_svc.add_contract({
                    "label": "Contract 1",
                    "contract_start_date": contract_start_date,
                    "contract_end_date": contract_end_date,
                    "price_per_credit": price_per_credit,
                    "rollover_allowed": rollover_allowed,
                    "purchased_credits": purchased_credits,
                    "purchased_credits_date": contract_start_date,
                })
            flash("Contract configuration saved.", "success")
        except (ValueError, TypeError) as exc:
            flash(f"Could not save configuration: {exc}", "danger")
        return redirect(url_for("main.setup_page"))

    @bp.route("/setup/skip", methods=["GET", "POST"])
    def setup_skip() -> object:
        session["setup_skipped"] = True
        flash("Setup skipped — you can configure anytime in Settings.", "info")
        return redirect(url_for("main.summary_page"))

    @bp.route("/setup/finish", methods=["GET", "POST"])
    def setup_finish() -> object:
        session["setup_skipped"] = True
        return redirect(url_for("main.summary_page"))
