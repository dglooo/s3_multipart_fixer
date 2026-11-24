from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)


def parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def parse_duration(value: str | None) -> timedelta | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None

    match = _DURATION_RE.fullmatch(normalized)
    if not match:
        raise ValueError(
            "Invalid duration format. Use values like 30m, 12h, 7d, or 1w."
        )

    amount = int(match.group(1))
    unit = match.group(2).lower()
    unit_to_seconds = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
        "w": 604800,
    }
    return timedelta(seconds=amount * unit_to_seconds[unit])
