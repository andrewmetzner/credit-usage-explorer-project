"""Small shared helpers for typed data objects."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class DictMixin(Mapping):
    """Make a ``@dataclass`` readable like the plain dict it replaced.

    Subclass this on a dataclass and every dict-style access keeps working
    unchanged — ``obj["x"]``, ``obj.get(...)``, ``{**obj}``, ``.items()``,
    ``json``/``pandas`` interop, and Jinja's ``obj.x`` — while new code gets
    typed attribute access and IDE autocomplete from the declared fields.

    The instance stays mutable (set ``obj.field = ...``); only dict-style
    *item* assignment (``obj["field"] = ...``) is unavailable, since Mapping is
    read-only.
    """

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError as exc:
            raise KeyError(key) from exc

    def __iter__(self):
        return iter(self.__dataclass_fields__)

    def __len__(self) -> int:
        return len(self.__dataclass_fields__)
