from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from uuid import uuid4
import json

from s3_uploader.common import DEFAULT_STORAGE_CLASS, normalize_storage_class, utc_now_iso


@dataclass(frozen=True)
class OperationRecord:
    operation: str
    bucket: str
    key: str
    status: str
    storage_class: str = DEFAULT_STORAGE_CLASS
    size_bytes: int = 0
    upload_mode: Optional[str] = None
    source_file: Optional[str] = None
    logical_key: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid4().hex)
    timestamp: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["storage_class"] = normalize_storage_class(payload.get("storage_class"))
        return payload


class ReportRepository:
    REPORT_FILE_NAME = "s3_upload_report.json"
    SESSIONS_DIR_NAME = "sessions"

    def __init__(self, results_dir: Optional[Path] = None) -> None:
        base_dir = results_dir or Path(__file__).resolve().parent / "results"
        self.results_dir = Path(base_dir).resolve()
        self.report_path = self.results_dir / self.REPORT_FILE_NAME
        self.sessions_dir = self.results_dir / self.SESSIONS_DIR_NAME
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.report_path.exists():
            return self._empty_report()

        with self.report_path.open("r", encoding="utf-8") as file_handle:
            return json.load(file_handle)

    def save(self, report_data: dict[str, Any]) -> None:
        with self.report_path.open("w", encoding="utf-8") as file_handle:
            json.dump(report_data, file_handle, indent=2, ensure_ascii=False)

    def append_session(
        self,
        session_type: str,
        operations: Iterable[OperationRecord],
        metadata: Optional[dict[str, Any]] = None,
    ) -> Path:
        operation_dicts = [operation.to_dict() for operation in operations]
        if not operation_dicts:
            raise ValueError("Session contains no operations")

        session_payload = {
            "session_type": session_type,
            "created_at": utc_now_iso(),
            "metadata": metadata or {},
            "operations": operation_dicts,
            "summary": self._build_session_summary(operation_dicts),
        }

        session_path = self._build_session_path(session_type)
        with session_path.open("w", encoding="utf-8") as file_handle:
            json.dump(session_payload, file_handle, indent=2, ensure_ascii=False)

        report_data = self.load()
        report_data["operations"].extend(operation_dicts)
        report_data["sessions"].append(
            {
                "session_type": session_type,
                "file_name": session_path.name,
                "created_at": session_payload["created_at"],
                "summary": session_payload["summary"],
            }
        )

        self._apply_operations(report_data["active_objects"], operation_dicts)
        self._update_operation_summary(report_data["summary"], operation_dicts)
        report_data["summary"]["active"] = self._build_active_summary(report_data["active_objects"])
        report_data["meta"]["updated_at"] = utc_now_iso()
        self.save(report_data)
        return session_path

    def list_active_objects(
        self,
        bucket: Optional[str] = None,
        storage_class: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        report_data = self.load()
        active_objects: list[dict[str, Any]] = []
        storage_filter = normalize_storage_class(storage_class) if storage_class else None

        for bucket_name, bucket_objects in report_data.get("active_objects", {}).items():
            if bucket and bucket_name != bucket:
                continue
            for object_key, object_data in bucket_objects.items():
                object_storage = normalize_storage_class(object_data["storage_class"])
                if storage_filter and object_storage != storage_filter:
                    continue
                active_objects.append(
                    {
                        "bucket": bucket_name,
                        "key": object_key,
                        **object_data,
                    }
                )

        return sorted(active_objects, key=lambda item: (item["uploaded_at"], item["bucket"], item["key"]))

    def render_human_summary(
        self,
        bucket: Optional[str] = None,
        recent_limit: int = 10,
    ) -> str:
        report_data = self.load()
        lines: list[str] = []
        summary = report_data["summary"]

        lines.append("Totals")
        lines.append(
            f"  Uploads: {summary['uploads']['objects']} objects, {self._format_bytes(summary['uploads']['bytes'])}"
        )
        lines.append(
            f"  Deletes: {summary['deletes']['objects']} objects, {self._format_bytes(summary['deletes']['bytes'])}"
        )
        lines.append(
            f"  Active:  {summary['active']['objects']} objects, {self._format_bytes(summary['active']['bytes'])}"
        )

        lines.append("")
        lines.append("Buckets")
        bucket_names = self._collect_bucket_names(summary)
        for bucket_name in bucket_names:
            if bucket and bucket_name != bucket:
                continue
            lines.append(f"  {bucket_name}")
            lines.extend(self._format_bucket_section(summary, bucket_name))

        recent_operations = report_data.get("operations", [])
        if bucket:
            recent_operations = [item for item in recent_operations if item["bucket"] == bucket]
        recent_operations = recent_operations[-recent_limit:]

        lines.append("")
        lines.append(f"Recent operations ({len(recent_operations)})")
        for item in recent_operations:
            size_text = self._format_bytes(item.get("size_bytes", 0))
            storage_class = normalize_storage_class(item.get("storage_class"))
            lines.append(
                f"  {item['timestamp']} | {item['operation']} | {item['status']} | "
                f"{item['bucket']}/{item['key']} | {storage_class} | {size_text}"
            )

        lines.append("")
        lines.append(f"Report file: {self.report_path}")
        return "\n".join(lines)

    def _empty_report(self) -> dict[str, Any]:
        return {
            "meta": {
                "version": 1,
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            },
            "sessions": [],
            "operations": [],
            "active_objects": {},
            "summary": {
                "uploads": self._empty_counter(),
                "deletes": self._empty_counter(),
                "active": self._empty_counter(),
            },
        }

    def _apply_operations(
        self,
        active_objects: dict[str, dict[str, Any]],
        operations: list[dict[str, Any]],
    ) -> None:
        for item in operations:
            if item["status"] != "success":
                continue

            bucket = item["bucket"]
            key = item["key"]
            if item["operation"] == "upload":
                bucket_objects = active_objects.setdefault(bucket, {})
                bucket_objects[key] = {
                    "storage_class": normalize_storage_class(item.get("storage_class")),
                    "size_bytes": int(item.get("size_bytes", 0)),
                    "uploaded_at": item["timestamp"],
                    "source_file": item.get("source_file"),
                    "logical_key": item.get("logical_key"),
                    "upload_mode": item.get("upload_mode"),
                }
            elif item["operation"] == "delete":
                bucket_objects = active_objects.get(bucket, {})
                bucket_objects.pop(key, None)
                if not bucket_objects and bucket in active_objects:
                    del active_objects[bucket]

    def _update_operation_summary(
        self,
        summary: dict[str, Any],
        operations: list[dict[str, Any]],
    ) -> None:
        for item in operations:
            if item["status"] != "success":
                continue
            operation_name = item["operation"]
            if operation_name not in {"upload", "delete"}:
                continue
            counter = summary[f"{operation_name}s"]
            self._increment_counter(
                counter=counter,
                bucket=item["bucket"],
                storage_class=normalize_storage_class(item.get("storage_class")),
                size_bytes=int(item.get("size_bytes", 0)),
            )

    def _build_active_summary(self, active_objects: dict[str, dict[str, Any]]) -> dict[str, Any]:
        summary = self._empty_counter()
        for bucket, bucket_objects in active_objects.items():
            for object_data in bucket_objects.values():
                self._increment_counter(
                    counter=summary,
                    bucket=bucket,
                    storage_class=normalize_storage_class(object_data.get("storage_class")),
                    size_bytes=int(object_data.get("size_bytes", 0)),
                )
        return summary

    def _increment_counter(
        self,
        counter: dict[str, Any],
        bucket: str,
        storage_class: str,
        size_bytes: int,
    ) -> None:
        counter["objects"] += 1
        counter["bytes"] += size_bytes
        by_bucket = counter["by_bucket"].setdefault(bucket, {})
        storage_counter = by_bucket.setdefault(storage_class, {"objects": 0, "bytes": 0})
        storage_counter["objects"] += 1
        storage_counter["bytes"] += size_bytes

    def _build_session_summary(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        summary = {
            "uploads": self._empty_counter(),
            "deletes": self._empty_counter(),
        }
        for item in operations:
            if item["status"] != "success":
                continue
            if item["operation"] not in {"upload", "delete"}:
                continue
            self._increment_counter(
                counter=summary[f"{item['operation']}s"],
                bucket=item["bucket"],
                storage_class=normalize_storage_class(item.get("storage_class")),
                size_bytes=int(item.get("size_bytes", 0)),
            )
        return summary

    def _build_session_path(self, session_type: str) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        file_name = f"{session_type}_{timestamp}_{uuid4().hex[:8]}.json"
        return self.sessions_dir / file_name

    def _empty_counter(self) -> dict[str, Any]:
        return {
            "objects": 0,
            "bytes": 0,
            "by_bucket": {},
        }

    def _collect_bucket_names(self, summary: dict[str, Any]) -> list[str]:
        bucket_names = set(summary["uploads"]["by_bucket"].keys())
        bucket_names.update(summary["deletes"]["by_bucket"].keys())
        bucket_names.update(summary["active"]["by_bucket"].keys())
        return sorted(bucket_names)

    def _format_bucket_section(self, summary: dict[str, Any], bucket_name: str) -> list[str]:
        lines: list[str] = []
        bucket_uploads = summary["uploads"]["by_bucket"].get(bucket_name, {})
        bucket_deletes = summary["deletes"]["by_bucket"].get(bucket_name, {})
        bucket_active = summary["active"]["by_bucket"].get(bucket_name, {})
        storage_classes = sorted(set(bucket_uploads) | set(bucket_deletes) | set(bucket_active))

        bucket_upload_total = self._sum_storage_counters(bucket_uploads)
        bucket_delete_total = self._sum_storage_counters(bucket_deletes)
        bucket_active_total = self._sum_storage_counters(bucket_active)
        lines.append(
            f"    TOTAL: "
            f"uploaded {bucket_upload_total['objects']} / {self._format_bytes(bucket_upload_total['bytes'])}, "
            f"deleted {bucket_delete_total['objects']} / {self._format_bytes(bucket_delete_total['bytes'])}, "
            f"active {bucket_active_total['objects']} / {self._format_bytes(bucket_active_total['bytes'])}"
        )

        for storage_class in storage_classes:
            upload_data = bucket_uploads.get(storage_class, {"objects": 0, "bytes": 0})
            delete_data = bucket_deletes.get(storage_class, {"objects": 0, "bytes": 0})
            active_data = bucket_active.get(storage_class, {"objects": 0, "bytes": 0})
            lines.append(
                f"    {storage_class}: "
                f"uploaded {upload_data['objects']} / {self._format_bytes(upload_data['bytes'])}, "
                f"deleted {delete_data['objects']} / {self._format_bytes(delete_data['bytes'])}, "
                f"active {active_data['objects']} / {self._format_bytes(active_data['bytes'])}"
            )
        if not storage_classes:
            lines.append("    no operations")
        return lines

    def _sum_storage_counters(self, storage_counters: dict[str, dict[str, int]]) -> dict[str, int]:
        total_objects = 0
        total_bytes = 0
        for item in storage_counters.values():
            total_objects += int(item.get("objects", 0))
            total_bytes += int(item.get("bytes", 0))
        return {"objects": total_objects, "bytes": total_bytes}

    def _format_bytes(self, size_bytes: int) -> str:
        value = float(size_bytes)
        units = ["B", "KiB", "MiB", "GiB", "TiB"]
        for unit in units:
            if value < 1024 or unit == units[-1]:
                return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
            value /= 1024
        return f"{size_bytes} B"
