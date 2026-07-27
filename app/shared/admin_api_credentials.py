"""Multiple named Admin API keys, each with a role telling the client which
key to use for which kind of call: "read" (GET), "write" (POST/DELETE), or
"read_write" (a single key that covers both).

The standalone `openai_admin_API` package (client.py) has no concept of
multiple keys or roles -- it only ever reads from a small, fixed set of
files in aauth/. This module is the layer on top: the full list of keys
lives in its own JSON file here, and every add/update/remove
"materializes" whichever key currently satisfies each role into those
fixed role-specific files, so client.py's role-aware loaders
(load_read_key/load_write_key, etc.) always see a simple, already-resolved
answer without knowing anything about "profiles."
"""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

KEYS_PATH = _PROJECT_ROOT / "aauth" / "admin_api_keys.json"

# potential deprecated single-credential files
_LEGACY_KEY_FILE = _PROJECT_ROOT / "aauth" / "secrete.txt"
_LEGACY_WORKSPACE_FILE = _PROJECT_ROOT / "aauth" / "workspace_id.txt"
_LEGACY_ORG_FILE = _PROJECT_ROOT / "aauth" / "organization_id.txt"

_LEGACY_META_PATH = _PROJECT_ROOT / "config" / "admin_api_credentials_meta.json"

ROLES = ("read", "write", "read_write")

# Materialized, role-specific files -- these are what
# openai_admin_API/client.py actually reads at request time.
READ_KEY_FILE = _PROJECT_ROOT / "aauth" / "secrete_read.txt"
WRITE_KEY_FILE = _PROJECT_ROOT / "aauth" / "secrete_write.txt"
READ_WORKSPACE_FILE = _PROJECT_ROOT / "aauth" / "workspace_id_read.txt"
WRITE_WORKSPACE_FILE = _PROJECT_ROOT / "aauth" / "workspace_id_write.txt"
READ_ORG_FILE = _PROJECT_ROOT / "aauth" / "organization_id_read.txt"
WRITE_ORG_FILE = _PROJECT_ROOT / "aauth" / "organization_id_write.txt"


def _read_file(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_file(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if value:
        path.write_text(value.strip() + "\n", encoding="utf-8")
    elif path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def _legacy_access_level() -> str:
    if not _LEGACY_META_PATH.exists():
        return ""
    try:
        return json.loads(_LEGACY_META_PATH.read_text(encoding="utf-8")).get("access_level", "")
    except (OSError, ValueError):
        return ""


def _empty() -> dict:
    return {"keys": []}


def load_keys() -> dict:
    if KEYS_PATH.exists():
        try:
            data = json.loads(KEYS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _empty()
        data.setdefault("keys", [])
        return data

    secret_key = _read_file(_LEGACY_KEY_FILE)
    workspace_id = _read_file(_LEGACY_WORKSPACE_FILE)
    organization_id = _read_file(_LEGACY_ORG_FILE)
    if not (secret_key or workspace_id or organization_id):
        return _empty()
    legacy_level = _legacy_access_level()
    role = "read_write" if legacy_level == "read_write" else "write" if legacy_level == "write" else "read"
    key = {
        "id": uuid4().hex[:8],
        "label": "Migrated key",
        "secret_key": secret_key,
        "workspace_id": workspace_id,
        "organization_id": organization_id,
        "role": role,
    }
    data = {"keys": [key]}
    save_keys(data)
    return data


def save_keys(data: dict) -> None:
    KEYS_PATH.parent.mkdir(parents=True, exist_ok=True)
    KEYS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    materialize(data)


def _first_for_role(data: dict, role: str) -> dict | None:
    """The key that satisfies `role` right now: an exact-role match takes
    priority; a read_write key only steps in as a fallback when there's no
    dedicated key for that role. Among ties, whichever was added first
    wins -- simple and predictable, no separate priority field to manage."""
    keys = data.get("keys", [])
    exact = next((k for k in keys if k.get("role") == role), None)
    if exact:
        return exact
    return next((k for k in keys if k.get("role") == "read_write"), None)


def materialize(data: dict | None = None) -> None:
    """Write whichever key currently satisfies each role into the fixed
    files client.py reads. Call after any add/update/remove. A role with
    no satisfying key clears its files (see _write_file), so a removed or
    edited-away key can never linger as a stale credential."""
    data = data if data is not None else load_keys()
    read_key = _first_for_role(data, "read")
    write_key = _first_for_role(data, "write")

    _write_file(READ_KEY_FILE, (read_key or {}).get("secret_key", ""))
    _write_file(READ_WORKSPACE_FILE, (read_key or {}).get("workspace_id", ""))
    _write_file(READ_ORG_FILE, (read_key or {}).get("organization_id", ""))

    _write_file(WRITE_KEY_FILE, (write_key or {}).get("secret_key", ""))
    _write_file(WRITE_WORKSPACE_FILE, (write_key or {}).get("workspace_id", ""))
    _write_file(WRITE_ORG_FILE, (write_key or {}).get("organization_id", ""))


def add_key(fields: dict) -> str:
    data = load_keys()
    new_id = uuid4().hex[:8]
    key = {
        "id": new_id,
        "label": str(fields.get("label") or f"Key {len(data['keys']) + 1}"),
        "secret_key": str(fields.get("secret_key") or ""),
        "workspace_id": str(fields.get("workspace_id") or ""),
        "organization_id": str(fields.get("organization_id") or ""),
        "role": str(fields.get("role") or "read"),
    }
    data["keys"].append(key)
    save_keys(data)
    return new_id


def update_key(key_id: str, fields: dict) -> None:
    data = load_keys()
    for k in data["keys"]:
        if k.get("id") != key_id:
            continue
        for field in ("label", "secret_key", "workspace_id", "organization_id", "role"):
            if field not in fields:
                continue
            value = str(fields[field] or "")
            if field in ("secret_key", "workspace_id", "organization_id") and not value:
                continue
            k[field] = value
        break
    save_keys(data)


def remove_key(key_id: str) -> None:
    data = load_keys()
    data["keys"] = [k for k in data["keys"] if k.get("id") != key_id]
    save_keys(data)
