"""Mock multi-contract sandbox app.

Launches the REAL Credit Usage Explorer web app against a disposable temp
config + synthetic usage data, preloaded with several mock contracts that
exercise the multi-contract / rollover / credit-expiration features — so you
can click around /forecast and /settings without touching your real
config/data.

Run:
    python scripts/mock_forecast_app.py                # default scenario, port 5001
    python scripts/mock_forecast_app.py --scenario rollover
    python scripts/mock_forecast_app.py --port 5005 --weekly-burn 60000

Scenarios (see SCENARIOS below):
    expire    Contract 1 (active) does NOT roll over -> leftover expires at
              its end; Contract 2 starts fresh. (default)
    rollover  Contract 1 rolls over -> leftover carries into Contract 2.
    three     Three contracts: expire -> rollover -> expire, to see multiple
              boundaries chained.

Nothing here writes to the real project config/ or data/ — everything lives
in a fresh temp directory that is created on launch (and left on disk so you
can inspect it; the path is printed at startup).
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _contract(cid, label, start, end, credits, price, rollover, credits_date=None):
    return {
        "id": cid,
        "label": label,
        "contract_start_date": start,
        "contract_end_date": end,
        "price_per_credit": price,
        "rollover_allowed": rollover,
        "purchased_credits": credits,
        "purchased_credits_date": credits_date or start,
        "credit_entries": [{
            "id": f"{cid}-e1", "date": credits_date or start,
            "credits": float(credits), "kind": "purchased", "notes": "Initial allocation",
        }] if credits else [],
    }


SCENARIOS = {
    "expire": [
        _contract("c1", "FY26 (no rollover)", "2026-04-05", "2026-09-30", 1_200_000, 0.07, False),
        _contract("c2", "FY27", "2026-10-01", "2027-03-31", 900_000, 0.05, False),
    ],
    "rollover": [
        _contract("c1", "FY26 (rolls over)", "2026-04-05", "2026-09-30", 1_200_000, 0.07, True),
        _contract("c2", "FY27", "2026-10-01", "2027-03-31", 900_000, 0.05, False),
    ],
    "three": [
        _contract("c1", "FY26 (expires)", "2026-04-05", "2026-09-30", 1_200_000, 0.07, False),
        _contract("c2", "FY27 (rolls over)", "2026-10-01", "2027-03-31", 900_000, 0.05, True),
        _contract("c3", "FY28 (expires)", "2027-04-01", "2027-09-30", 700_000, 0.04, False),
    ],
}


def _synthetic_usage(start: str, weekly_burn: float, weeks: int) -> pd.DataFrame:
    """Daily usage rows (~5 rows/week) totaling `weekly_burn` per week, so the
    forecast engine derives a real operational burn rate to project from."""
    rows = []
    day = pd.Timestamp(start)
    per_day = weekly_burn / 5.0
    for w in range(weeks):
        for d in range(5):  # weekday usage only
            date = day + pd.Timedelta(days=w * 7 + d)
            rows.append({
                "date_partition": str(date.date()),
                "email": f"user{d % 3}@example.gov",
                "usage_credits": round(per_day, 2),
                "usage_type": "chat_completions",
                "usage_quantity": 1000,
            })
    return pd.DataFrame(rows)


def build_app(scenario: str, weekly_burn: float, weeks: int):
    contracts = SCENARIOS[scenario]
    tmp = Path(tempfile.mkdtemp(prefix="mock_forecast_"))
    config_dir = tmp / "config"
    data_dir = tmp / "data"
    for d in (config_dir, data_dir / "processed", data_dir / "historical", data_dir / "uploads"):
        d.mkdir(parents=True, exist_ok=True)

    (config_dir / "contract_config.yaml").write_text(
        yaml.dump({
            "contracts": contracts,
            "forecast": {
                "mode": "auto",
                "monte_carlo_runs": 2000,
                "snapshot_auto_save": "manual",
                "recent_average_window_weeks": 4,
                "minimum_weeks_for_recent_average": 1,
                "normalize_weights": True,
                # Same shape as the app's real default (see
                # config_service.DEFAULT_CONTRACT_CONFIG) — ramps from
                # historical-heavy at 0 operational weeks (needed so a
                # not-yet-started "other contract" — 0 operational weeks by
                # definition — still gets a real burn estimate instead of 0)
                # toward latest/recent-weighted as weeks accumulate.
                "auto_weight_schedule": [
                    {"min_operational_weeks": 0, "max_operational_weeks": 2,
                     "historical_weight": 0.7, "latest_week_weight": 0.3, "recent_average_weight": None},
                    {"min_operational_weeks": 3, "max_operational_weeks": 4,
                     "historical_weight": 0.5, "latest_week_weight": 0.2, "recent_average_weight": 0.3},
                    {"min_operational_weeks": 5, "max_operational_weeks": 8,
                     "historical_weight": 0.3, "latest_week_weight": 0.2, "recent_average_weight": 0.5},
                    {"min_operational_weeks": 9, "max_operational_weeks": None,
                     "historical_weight": 0.2, "latest_week_weight": 0.2, "recent_average_weight": 0.6},
                ],
            },
        }, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )

    data_csv = data_dir / "current_data.csv"
    _synthetic_usage(contracts[0]["contract_start_date"], weekly_burn, weeks).to_csv(data_csv, index=False)

    import config as app_config
    app_config.CONFIG_DIR = config_dir
    app_config.PROCESSED_DIR = data_dir / "processed"
    app_config.HISTORICAL_DIR = data_dir / "historical"
    app_config.UPLOADS_DIR = data_dir / "uploads"
    app_config.DEFAULT_DATA_PATH = data_csv
    app_config.CURRENT_DATA_PATH = data_csv
    app_config.CURRENT_DATA_PATH_CACHE = config_dir / "data_path.txt"

    from app import create_app
    flask_app = create_app()
    return flask_app, tmp


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="expire")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--weekly-burn", type=float, default=28_000.0,
                        help="Synthetic weekly burn rate (default 28000 — slow "
                             "enough that Contract 1 keeps a leftover balance at "
                             "its end, so the expiration behavior is visible).")
    parser.add_argument("--weeks", type=int, default=12,
                        help="Weeks of synthetic usage to seed (default 12).")
    args = parser.parse_args()

    flask_app, tmp = build_app(args.scenario, args.weekly_burn, args.weeks)
    print("=" * 70)
    print(f"  MOCK FORECAST SANDBOX — scenario: {args.scenario}")
    print(f"  Temp data dir (safe to delete): {tmp}")
    print(f"  Open:  http://127.0.0.1:{args.port}/forecast")
    print(f"         http://127.0.0.1:{args.port}/settings   (edit mock contracts)")
    print("  Real project config/ and data/ are NOT touched.")
    print("=" * 70)
    flask_app.run(debug=True, port=args.port, use_reloader=False)


if __name__ == "__main__":
    main()
