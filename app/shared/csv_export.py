from __future__ import annotations

from datetime import date
import hashlib
from io import StringIO
from pathlib import Path
import re
from typing import Iterable

import pandas as pd
from flask import Response


def _slug(text: object, max_chars: int = 32) -> str:
    value = str(text or "").strip().lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    value = re.sub(r"-{2,}", "-", value)
    if len(value) <= max_chars:
        return value
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:6]
    return f"{value[:max_chars - 7].rstrip('-')}-{digest}"


def filter_slug(filters: Iterable[tuple[str, object]], max_chars: int = 115) -> str:
    """Compact active filters into filename-safe key-value chunks."""
    parts = []
    raw = []
    for key, value in filters:
        if value in (None, "", False):
            continue
        k = _slug(key, 14)
        v = _slug(value, 36)
        if k and v:
            parts.append(f"{k}-{v}")
            raw.append(f"{key}={value}")
    suffix = "_".join(parts)
    if len(suffix) <= max_chars:
        return suffix
    digest = hashlib.sha1("|".join(raw).encode("utf-8")).hexdigest()[:8]
    return f"{suffix[:max_chars - 9].rstrip('-_')}_{digest}"


def dated_csv_filename(filename: str, filters: Iterable[tuple[str, object]] | None = None) -> str:
    """Append compact filters and today's ISO date to a CSV filename stem."""
    path = Path(filename)
    suffix = path.suffix or ".csv"
    stem = path.stem or "export"
    parts = [stem]
    if filters:
        fs = filter_slug(filters)
        if fs:
            parts.append(fs)
    base = "_".join(parts)
    today = date.today().isoformat()
    name = f"{base}_{today}"
    if len(name) > 150:
        digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
        reserved = len(today) + len(digest) + 2
        name = f"{base[:150 - reserved].rstrip('-_')}_{digest}_{today}"
    return f"{name}{suffix}"


def csv_response(
    df: pd.DataFrame,
    filename: str,
    filters: Iterable[tuple[str, object]] | None = None,
) -> Response:
    """Return a UTF-8 CSV download response for a DataFrame."""
    bio = StringIO()
    df.to_csv(bio, index=False)
    download_name = dated_csv_filename(filename, filters)
    return Response(
        bio.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )
