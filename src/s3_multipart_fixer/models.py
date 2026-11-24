from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(slots=True, frozen=True)
class MultipartUploadPart:
    part_number: int
    size_bytes: int
    etag: str | None = None
    last_modified: datetime | None = None

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "MultipartUploadPart":
        return cls(
            part_number=payload["PartNumber"],
            size_bytes=payload["Size"],
            etag=payload.get("ETag"),
            last_modified=payload.get("LastModified"),
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MultipartUploadPart":
        last_modified = payload.get("last_modified")
        parsed_last_modified: datetime | None = None
        if isinstance(last_modified, str):
            normalized = last_modified[:-1] + "+00:00" if last_modified.endswith("Z") else last_modified
            parsed_last_modified = datetime.fromisoformat(normalized)
            if parsed_last_modified.tzinfo is None:
                parsed_last_modified = parsed_last_modified.replace(tzinfo=UTC)
        elif isinstance(last_modified, datetime):
            parsed_last_modified = last_modified

        return cls(
            part_number=payload["part_number"],
            size_bytes=payload["size_bytes"],
            etag=payload.get("etag"),
            last_modified=parsed_last_modified,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "part_number": self.part_number,
            "size_bytes": self.size_bytes,
            "etag": self.etag,
            "last_modified": self.last_modified.isoformat() if self.last_modified else None,
        }


@dataclass(slots=True, frozen=True)
class MultipartUploadRecord:
    bucket: str
    key: str
    upload_id: str
    initiated: datetime
    initiator_id: str | None = None
    initiator_display_name: str | None = None
    owner_id: str | None = None
    owner_display_name: str | None = None
    storage_class: str | None = None
    completed_parts: tuple[MultipartUploadPart, ...] = ()
    completed_parts_count: int = 0
    completed_parts_total_bytes: int = 0
    completed_parts_error: str | None = None

    @classmethod
    def from_api(cls, bucket: str, payload: dict[str, Any]) -> "MultipartUploadRecord":
        initiator = payload.get("Initiator") or {}
        owner = payload.get("Owner") or {}
        return cls(
            bucket=bucket,
            key=payload["Key"],
            upload_id=payload["UploadId"],
            initiated=payload["Initiated"],
            initiator_id=initiator.get("ID"),
            initiator_display_name=initiator.get("DisplayName"),
            owner_id=owner.get("ID"),
            owner_display_name=owner.get("DisplayName"),
            storage_class=payload.get("StorageClass"),
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MultipartUploadRecord":
        initiated = payload["initiated"]
        if isinstance(initiated, str):
            normalized = initiated[:-1] + "+00:00" if initiated.endswith("Z") else initiated
            parsed_initiated = datetime.fromisoformat(normalized)
            if parsed_initiated.tzinfo is None:
                parsed_initiated = parsed_initiated.replace(tzinfo=UTC)
        elif isinstance(initiated, datetime):
            parsed_initiated = initiated
        else:
            raise ValueError("initiated must be an ISO-8601 string or datetime")

        completed_parts_payload = payload.get("completed_parts") or []
        completed_parts = tuple(
            part if isinstance(part, MultipartUploadPart) else MultipartUploadPart.from_dict(part)
            for part in completed_parts_payload
        )
        completed_parts_count = payload.get("completed_parts_count", len(completed_parts))
        completed_parts_total_bytes = payload.get(
            "completed_parts_total_bytes",
            sum(part.size_bytes for part in completed_parts),
        )

        return cls(
            bucket=payload["bucket"],
            key=payload["key"],
            upload_id=payload["upload_id"],
            initiated=parsed_initiated,
            initiator_id=payload.get("initiator_id"),
            initiator_display_name=payload.get("initiator_display_name"),
            owner_id=payload.get("owner_id"),
            owner_display_name=payload.get("owner_display_name"),
            storage_class=payload.get("storage_class"),
            completed_parts=completed_parts,
            completed_parts_count=completed_parts_count,
            completed_parts_total_bytes=completed_parts_total_bytes,
            completed_parts_error=payload.get("completed_parts_error"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "key": self.key,
            "upload_id": self.upload_id,
            "initiated": self.initiated.isoformat(),
            "initiator_id": self.initiator_id,
            "initiator_display_name": self.initiator_display_name,
            "owner_id": self.owner_id,
            "owner_display_name": self.owner_display_name,
            "storage_class": self.storage_class,
            "completed_parts": [part.to_dict() for part in self.completed_parts],
            "completed_parts_count": self.completed_parts_count,
            "completed_parts_total_bytes": self.completed_parts_total_bytes,
            "completed_parts_error": self.completed_parts_error,
        }


@dataclass(slots=True, frozen=True)
class MultipartUploadSelector:
    initiated_after: datetime | None = None
    initiated_before: datetime | None = None
    key_prefix: str | None = None
    key_equals: str | None = None
    upload_id_equals: str | None = None

    def matches(self, record: MultipartUploadRecord) -> bool:
        if self.key_prefix and not record.key.startswith(self.key_prefix):
            return False
        if self.key_equals and record.key != self.key_equals:
            return False
        if self.upload_id_equals and record.upload_id != self.upload_id_equals:
            return False
        if self.initiated_after and record.initiated < self.initiated_after:
            return False
        if self.initiated_before and record.initiated >= self.initiated_before:
            return False
        return True


@dataclass(slots=True)
class AbortResult:
    record: MultipartUploadRecord
    requested: bool
    success: bool
    verified: bool
    attempts: int
    message: str
    error_code: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "bucket": self.record.bucket,
            "key": self.record.key,
            "upload_id": self.record.upload_id,
            "initiated": self.record.initiated.isoformat(),
            "requested": self.requested,
            "success": self.success,
            "verified": self.verified,
            "attempts": self.attempts,
            "message": self.message,
            "error_code": self.error_code,
        }
        payload.update(self.extra)
        return payload


@dataclass(slots=True)
class ScanSummary:
    scanned: int = 0
    matched: int = 0
    emitted: int = 0
    aborted: int = 0
    verified: int = 0
    failed: int = 0
