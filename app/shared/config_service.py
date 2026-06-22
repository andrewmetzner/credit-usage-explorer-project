from __future__ import annotations

from pathlib import Path

import yaml


class AppConfig:
    def __init__(self, config_dir: Path) -> None:
        self.contract_path = config_dir / "contract_config.yaml"
        self.tier_path = config_dir / "tier_policy_config.yaml"

    def load_contract(self) -> dict:
        with open(self.contract_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def save_contract(self, data: dict) -> None:
        with open(self.contract_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

    def load_tiers(self) -> dict:
        with open(self.tier_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def save_tiers(self, data: dict) -> None:
        with open(self.tier_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
