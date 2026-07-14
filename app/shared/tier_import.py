from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from io import BytesIO
from typing import Any

import pandas as pd


@dataclass
class TierImportResult:
    assignments: dict[str, str]
    histories: dict[str, list[str]]
    tiers: list[str]
    rows: int
    imported_rows: int
    skipped_rows: int
    email_column: str
    tier_column: str
    tier_counts: dict[str, int]
    codex_access: dict[str, bool]


def _normalized_col(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _find_column(columns: list[str], candidates: set[str], contains: tuple[str, ...] = ()) -> str | None:
    normalized = {_normalized_col(col): col for col in columns}
    for candidate in candidates:
        if candidate in normalized:
            return normalized[candidate]
    for col in columns:
        norm = _normalized_col(col)
        if any(part in norm for part in contains):
            return col
    return None


def _flatten_group_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        values: list[str] = []
        for item in value.values():
            values.extend(_flatten_group_values(item))
        return values
    if isinstance(value, (list, tuple, set)):
        values = []
        for item in value:
            values.extend(_flatten_group_values(item))
        return values
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def _normalize_group_label(value: object) -> str:
    text = str(value or "").strip().strip(" \t\r\n{}[]'\"")
    if text.startswith("_") and " " in text:
        text = text.lstrip("_").strip()
    return text


def is_codex_group_label(value: object) -> bool:
    """True for groups that grant Codex product access (e.g. "_Codex Users").

    These aren't a credit-governance tier -- they only surface as a profile
    badge -- so they must never enter a user's tier history/assignment.
    """
    text = _normalize_group_label(value).lstrip("_").strip()
    return text.lower().startswith("codex")


def _group_label_score(value: object) -> tuple[int, int, str]:
    label = _normalize_group_label(value)
    if not label:
        return (0, 0, "")

    exact_priority = {
        "One K Credit Users": 500,
        "Emergency Credit Users": 475,
        "High Credit Consumption Users": 450,
        "Advanced Credit Users": 425,
        "Highest": 400,
        "Super": 375,
        "Advanced": 350,
        "Baseline": 300,
    }
    if label in exact_priority:
        return (exact_priority[label], len(label), label)

    if label.startswith("_"):
        return (0, len(label), label)

    alpha_words = re.findall(r"[A-Za-z]{2,}", label)
    compact = re.sub(r"[^A-Za-z0-9]+", "", label)
    is_short_code = bool(re.fullmatch(r"[A-Z0-9&]{1,6}", compact)) and " " not in label
    if is_short_code:
        return (0, len(label), label)

    if len(alpha_words) >= 2:
        score = 200 + min(len(alpha_words), 8)
        if "Credit" in label:
            score += 50
        if "Users" in label:
            score += 10
        return (score, len(label), label)

    return (0, len(label), label)


def _readable_group_labels(values: list[str]) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for value in values:
        score, _, label = _group_label_score(value)
        if score <= 0 or not label or label in seen:
            continue
        labels.append(label)
        seen.add(label)
    return labels


def _readable_group_token(text: str) -> str:
    text = str(text or "").strip()
    if not text:
        return ""

    try:
        parsed = ast.literal_eval(text)
        values = _flatten_group_values(parsed)
        if values:
            labels = _readable_group_labels(values)
            return labels[-1] if labels else ""
    except Exception:
        pass

    quoted_values = re.findall(r":\s*['\"]([^'\"]+)['\"]", text)
    if quoted_values:
        labels = _readable_group_labels(quoted_values)
        return labels[-1] if labels else ""

    if any(sep in text for sep in ("|", ";", ">")):
        parts = re.split(r"\s*(?:\||;|>)\s*", text)
        parts = [part.strip() for part in parts if part.strip()]
        if parts:
            labels = _readable_group_labels(parts)
            return labels[-1] if labels else ""

    labels = _readable_group_labels([text])
    return labels[-1] if labels else ""


def extract_tier_names(value: object) -> list[str]:
    if pd.isna(value):
        return []

    text = str(value).strip()
    values: list[str] = []
    try:
        values = _flatten_group_values(ast.literal_eval(text))
    except Exception:
        values = re.findall(r":\s*['\"]([^'\"]+)['\"]", text)
        if not values and any(sep in text for sep in ("|", ";", ">")):
            values = re.split(r"\s*(?:\||;|>)\s*", text)
        if not values:
            values = [text]

    return _readable_group_labels(values)


def clean_tier_name(value: object) -> str:
    """Normalize the tier/group cell from a users export.

    OpenAI user exports can store the groups column as a dictionary-like string
    of group ids to names. For tier assignment we want the most meaningful
    English group name, not shorthand org codes or the whole serialized dict.
    """
    if pd.isna(value):
        return ""
    return _readable_group_token(str(value)).strip()


def read_tier_assignments_csv(file_obj: Any, tier_caps: dict[str, float] | None = None) -> TierImportResult:
    """Parse a tierlist users export into assignments + histories.

    When ``tier_caps`` (tier name -> cap) is provided, a user belonging to
    several groups is assigned the group with the HIGHEST credit allotment;
    without caps it falls back to the most recent (last) group."""
    raw = file_obj.read()
    if isinstance(raw, str):
        raw = raw.encode("utf-8")

    try:
        df = pd.read_csv(BytesIO(raw), encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(BytesIO(raw), encoding="cp1252")

    columns = [str(col) for col in df.columns]
    email_col = _find_column(
        columns,
        {"email", "emailaddress", "useremail", "mail"},
        contains=("email",),
    )
    tier_col = _find_column(
        columns,
        {
            "tier",
            "tiername",
            "assignedtier",
            "governancetier",
            "groups",
            "group",
            "groupname",
        },
        contains=("tier", "group"),
    )

    if not email_col:
        raise ValueError("Could not find an email column in the tierlist CSV.")
    if not tier_col:
        raise ValueError("Could not find a tier or groups column in the tierlist CSV.")

    assignments: dict[str, str] = {}
    histories: dict[str, list[str]] = {}
    codex_access: dict[str, bool] = {}
    tiers: set[str] = set()
    skipped_rows = 0
    from app.optimization.service import highest_cap_tier

    for _, row in df.iterrows():
        email = str(row.get(email_col, "") or "").strip().lower()
        # The groups cell lists a user's groups oldest-first, most-recent-last.
        # Codex groups are governance tiers too when the policy gives them a
        # cap (e.g. "Codex Users" at 3,000/month); membership additionally
        # surfaces as the Codex-access badge on the user profile.
        raw_labels = extract_tier_names(row.get(tier_col, ""))
        if email and any(is_codex_group_label(label) for label in raw_labels):
            codex_access[email] = True
        history = list(raw_labels)
        tiers.update(history)
        tier = (
            highest_cap_tier(history, tier_caps) if tier_caps
            else (history[-1] if history else "")
        )
        if not email or not tier:
            skipped_rows += 1
            continue
        assignments[email] = tier
        histories[email] = history

    tier_counts: dict[str, int] = {}
    for tier in assignments.values():
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

    return TierImportResult(
        assignments=assignments,
        histories=histories,
        tiers=sorted(tiers),
        rows=int(len(df)),
        imported_rows=len(assignments),
        skipped_rows=skipped_rows,
        email_column=email_col,
        tier_column=tier_col,
        tier_counts=dict(sorted(tier_counts.items())),
        codex_access=codex_access,
    )
