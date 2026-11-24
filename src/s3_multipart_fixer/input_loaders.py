from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import MultipartUploadRecord


def load_multipart_upload_records(source: str) -> list[MultipartUploadRecord]:
    path = Path(source)
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []

    if path.suffix.lower() == ".json":
        payload = json.loads(text)
        return _records_from_json_payload(payload)

    records: list[MultipartUploadRecord] = []
    for line in text.splitlines():
        normalized = line.strip()
        if not normalized:
            continue
        records.append(MultipartUploadRecord.from_dict(json.loads(normalized)))
    return records


def _records_from_json_payload(payload: Any) -> list[MultipartUploadRecord]:
    if isinstance(payload, list):
        return [MultipartUploadRecord.from_dict(item) for item in payload]
    if isinstance(payload, dict):
        if "uploads" in payload and isinstance(payload["uploads"], list):
            return [MultipartUploadRecord.from_dict(item) for item in payload["uploads"]]
        return [MultipartUploadRecord.from_dict(payload)]
    raise ValueError("Unsupported JSON payload. Expected object, array, or {'uploads': [...]} format.")
