from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


DEFAULT_STORAGE_CLASS = "STANDARD"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_storage_class(storage_class: Optional[str]) -> str:
    return (storage_class or DEFAULT_STORAGE_CLASS).strip().upper()
