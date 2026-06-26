from __future__ import annotations

import copy
import json
from pathlib import Path

import yaml

from .alert_rules import DEFAULT_ALERT_RULES, AlertRule

# Used when no contract_config.yaml exists yet (fresh install / setup wizard).
DEFAULT_CONTRACT_CONFIG: dict = {
    "contract": {
        "contract_start_date": "",
        "contract_end_date": "",
        "purchased_credits": 0,
        "rollover_allowed": False,
    },
    "pricing": {
        "current_price_per_credit": 0.0,
        "next_contract_price_per_credit": 0.0,
    },
    "forecast": {
        "mode": "auto",
        "normalize_weights": True,
        "recent_average_window_weeks": 4,
        "minimum_weeks_for_recent_average": 4,
        "monte_carlo_runs": 10000,
        "snapshot_auto_save": "daily",
        "auto_weight_schedule": [
            {"min_operational_weeks": 0, "max_operational_weeks": 2, "historical_weight": 0.7, "latest_week_weight": 0.3, "recent_average_weight": None},
            {"min_operational_weeks": 3, "max_operational_weeks": 4, "historical_weight": 0.5, "latest_week_weight": 0.2, "recent_average_weight": 0.3},
            {"min_operational_weeks": 5, "max_operational_weeks": 8, "historical_weight": 0.3, "latest_week_weight": 0.2, "recent_average_weight": 0.5},
            {"min_operational_weeks": 9, "max_operational_weeks": None, "historical_weight": 0.2, "latest_week_weight": 0.2, "recent_average_weight": 0.6},
        ],
    },
}


class AppConfig:
    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.contract_path = config_dir / "contract_config.yaml"
        self.tier_path = config_dir / "tier_policy_config.yaml"
        self.alert_rules_path = config_dir / "alert_rules.json"
        # Which alert conditions the user has dismissed/read (the navbar bell
        # "inbox"). Persisted server-side so read-state survives across browsers
        # and machines, unlike the old per-browser localStorage approach.
        self.read_alerts_path = config_dir / "alert_read_state.json"

    def contract_exists(self) -> bool:
        return self.contract_path.exists()

    def is_contract_configured(self) -> bool:
        """True only when the contract has real dates + purchased credits set."""
        try:
            c = self.load_contract().get("contract", {})
            return bool(c.get("contract_start_date")) and bool(c.get("contract_end_date")) \
                and float(c.get("purchased_credits") or 0) > 0
        except Exception:
            return False

    def load_contract(self) -> dict:
        if not self.contract_path.exists():
            return copy.deepcopy(DEFAULT_CONTRACT_CONFIG)
        with open(self.contract_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or copy.deepcopy(DEFAULT_CONTRACT_CONFIG)

    def save_contract(self, data: dict) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        with open(self.contract_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

    def load_tiers(self) -> dict:
        if not self.tier_path.exists():
            return {"tiers": {}}
        with open(self.tier_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {"tiers": {}}

    def save_tiers(self, data: dict) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        with open(self.tier_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

    def load_alert_rules(self) -> list[AlertRule]:
        if not self.alert_rules_path.exists():
            return [AlertRule.from_dict(r) for r in DEFAULT_ALERT_RULES]
        try:
            data = json.loads(self.alert_rules_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [AlertRule.from_dict(r) for r in data]
            return [AlertRule.from_dict(r) for r in DEFAULT_ALERT_RULES]
        except Exception:
            return [AlertRule.from_dict(r) for r in DEFAULT_ALERT_RULES]

    def save_alert_rules(self, rules: list) -> None:
        # Normalize whatever we're handed (AlertRule or dict) to clean dicts.
        serializable = [AlertRule.from_dict(r).to_dict() for r in rules]
        self.alert_rules_path.write_text(json.dumps(serializable, indent=2), encoding="utf-8")

    # ── Alert read-state (navbar bell "inbox") ──────────────────────────────
    def load_read_alerts(self) -> set[str]:
        """The set of alert ids the user has marked read."""
        if not self.read_alerts_path.exists():
            return set()
        try:
            data = json.loads(self.read_alerts_path.read_text(encoding="utf-8"))
            return {str(x) for x in data} if isinstance(data, list) else set()
        except Exception:
            return set()

    def save_read_alerts(self, ids) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.read_alerts_path.write_text(
            json.dumps(sorted(str(i) for i in ids), indent=2), encoding="utf-8"
        )

    def prune_read_alerts(self, active_ids) -> set[str]:
        """Drop read ids that no longer match an active alert, so a resolved
        condition that later recurs re-notifies. Returns the surviving set;
        only rewrites the file when something actually changed."""
        read = self.load_read_alerts()
        kept = read & {str(i) for i in active_ids}
        if kept != read:
            self.save_read_alerts(kept)
        return kept

    def mark_read_alerts(self, ids, active_ids) -> set[str]:
        """Add `ids` to the read set (pruned to `active_ids`) and persist."""
        active = {str(i) for i in active_ids}
        read = (self.load_read_alerts() | {str(i) for i in ids}) & active
        self.save_read_alerts(read)
        return read
