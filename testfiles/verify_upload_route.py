"""Integration check for the multi-file /upload-data route and upload logging.

Uses the Flask test client to POST two mock sheets at once (replace mode) and
verifies the merged result and the upload log. Backs up and restores any
existing current_data.csv and upload_history.json so live data is untouched.

Run from the webapp_v3 directory:
    .venv\\Scripts\\python.exe testfiles\\verify_upload_route.py
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from app import create_app  # noqa: E402

MOCK_DIR = ROOT / "data" / "test_mock"
MOCKS = ["mock_2_mar_to_jun7.csv", "mock_3_jun1_to_jun22.csv"]


def _backup(path: Path):
    return path.read_bytes() if path.exists() else None


def _restore(path: Path, data):
    if data is None:
        if path.exists():
            path.unlink()
    else:
        path.write_bytes(data)


def main() -> int:
    if not all((MOCK_DIR / m).exists() for m in MOCKS):
        print("Mock files missing — run testfiles/test_data_merge.py first to generate them.")
        return 1

    current_csv = config.CURRENT_DATA_PATH.with_suffix(".csv")
    hist_json = config.PROCESSED_DIR / "upload_history.json"
    cache = config.CURRENT_DATA_PATH_CACHE

    backups = {p: _backup(p) for p in (current_csv, hist_json, cache)}

    ok = True
    try:
        app = create_app()
        client = app.test_client()

        data = {"replace_existing": "on"}
        files = []
        for m in MOCKS:
            files.append((io.BytesIO((MOCK_DIR / m).read_bytes()), m))
        data["file"] = files

        resp = client.post("/upload-data", data=data,
                           content_type="multipart/form-data", follow_redirects=False)
        print(f"  POST /upload-data -> {resp.status_code} (expect 302)")
        ok &= resp.status_code == 302

        merged = pd.read_csv(current_csv)
        dates = pd.to_datetime(merged["date_partition"], errors="coerce").dt.normalize().nunique()
        print(f"  merged rows  : {len(merged):,} (expect 1,710)")
        print(f"  unique dates : {dates} (expect 114)")
        ok &= len(merged) == 1710 and dates == 114

        log = json.loads(hist_json.read_text(encoding="utf-8"))
        sheet_entries = [e for e in log if e.get("type") == "data_sheet"]
        print(f"  log entries  : {len(sheet_entries)} data_sheet rows (expect 2)")
        ok &= len(sheet_entries) >= 2
        added = [e["stats"].get("rows_added") for e in sheet_entries[:2]]
        print(f"  rows_added logged: {added}")
        ok &= all(a is not None for a in added)
        ok &= all(e["stats"].get("mode") == "replace" for e in sheet_entries[:2])
    finally:
        for p, data in backups.items():
            _restore(p, data)
        print("  restored original current_data / upload_history / cache")

    print("\n" + ("PASS — multi-upload route + logging verified"
                  if ok else "FAIL — see mismatches above"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
