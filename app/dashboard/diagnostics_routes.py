"""Built-in diagnostics routes (/debug, /debug.json) on the `main` blueprint."""
from __future__ import annotations

from flask import jsonify, render_template


def register_diagnostics_routes(bp, services) -> None:
    @bp.route("/debug", methods=["GET"])
    def diagnostics_page() -> str:
        from app.shared.diagnostics import Diagnostics
        report = Diagnostics(services).run_all()
        return render_template("diagnostics.html", report=report)

    @bp.route("/debug.json", methods=["GET"])
    def diagnostics_json() -> object:
        from app.shared.diagnostics import Diagnostics
        report = Diagnostics(services).run_all()
        return jsonify({
            "overall": report["overall"],
            "total_ms": report["total_ms"],
            "results": [r.to_dict() for r in report["results"]],
        })
