from pathlib import Path

WEBAPP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = WEBAPP_ROOT.parent

DEFAULT_DATA_PATH = WEBAPP_ROOT / "data" / "data.xlsx"
CURRENT_DATA_PATH = WEBAPP_ROOT / "data" / "current_data.xlsx"

CONFIG_DIR = WEBAPP_ROOT / "config"
PROCESSED_DIR = WEBAPP_ROOT / "data" / "processed"
HISTORICAL_DIR = WEBAPP_ROOT / "data" / "historical"
UPLOADS_DIR = WEBAPP_ROOT / "data" / "uploads"
CONTRACT_CONFIG_PATH = CONFIG_DIR / "contract_config.yaml"
TIER_CONFIG_PATH = CONFIG_DIR / "tier_policy_config.yaml"
CURRENT_DATA_PATH_CACHE = CONFIG_DIR / "data_path.txt"
