from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from botocore.client import BaseClient
from botocore.exceptions import ClientError

from .models import MultipartUploadSelector, ScanSummary
from .object_counter import BucketObjectCounter, ObjectCountSummary, ObjectVersionCountSummary
from .scanner import MultipartUploadScanner
from .timeutils import utc_now


@dataclass(slots=True)
class BucketHealthCheckSummary:
    bucket: str
    prefix: str | None
    objects: ObjectCountSummary
    versions: ObjectVersionCountSummary
    generated_at: datetime
    multipart_uploads_count: int = 0
    multipart_uploaded_parts_count: int = 0
    multipart_uploaded_parts_total_bytes: int = 0
    multipart_parts_errors_count: int = 0
    probes: dict[str, object] | None = None

    @property
    def multipart_uploaded_parts_total_gb(self) -> float:
        return round(self.multipart_uploaded_parts_total_bytes / (1024**3), 3)

    @property
    def namespace_empty(self) -> bool:
        return (
            self.objects.objects_count == 0
            and self.versions.total_entries_count == 0
            and self.multipart_uploads_count == 0
            and self.multipart_uploaded_parts_total_bytes == 0
        )

    def to_dict(self) -> dict[str, int | float | str | bool | None]:
        return {
            "bucket": self.bucket,
            "prefix": self.prefix,
            "generated_at": self.generated_at.isoformat(),
            "objects": self.objects.to_dict(),
            "versions": self.versions.to_dict(),
            "multipart": {
                "uploads_count": self.multipart_uploads_count,
                "uploaded_parts_count": self.multipart_uploaded_parts_count,
                "uploaded_parts_total_bytes": self.multipart_uploaded_parts_total_bytes,
                "uploaded_parts_total_gb": self.multipart_uploaded_parts_total_gb,
                "parts_errors_count": self.multipart_parts_errors_count,
            },
            "namespace_empty": self.namespace_empty,
            "probes": self.probes or {},
        }


class BucketHealthChecker:
    def __init__(self, client: BaseClient) -> None:
        self._client = client
        self._object_counter = BucketObjectCounter(client)
        self._scanner = MultipartUploadScanner(client)

    def check(
        self,
        *,
        bucket: str,
        prefix: str | None = None,
        page_size: int = 1000,
        progress_every: int | None = None,
        object_progress_callback=None,
        versions_progress_callback=None,
        multipart_progress_callback=None,
    ) -> BucketHealthCheckSummary:
        objects = self._object_counter.count_objects(
            bucket=bucket,
            prefix=prefix,
            page_size=page_size,
            progress_every=progress_every,
            progress_callback=object_progress_callback,
        )
        versions = self._object_counter.count_object_versions(
            bucket=bucket,
            prefix=prefix,
            page_size=page_size,
            progress_every=progress_every,
            progress_callback=versions_progress_callback,
        )

        multipart_summary = ScanSummary()
        health = BucketHealthCheckSummary(
            bucket=bucket,
            prefix=prefix,
            objects=objects,
            versions=versions,
            generated_at=utc_now(),
            probes=self._collect_bucket_probes(bucket),
        )
        selector = MultipartUploadSelector()
        for record in self._scanner.filter_uploads(
            bucket=bucket,
            selector=selector,
            prefix=prefix,
            page_size=page_size,
            summary=multipart_summary,
            progress_every=progress_every,
            progress_callback=multipart_progress_callback,
        ):
            health.multipart_uploads_count += 1
            health.multipart_uploaded_parts_count += record.completed_parts_count
            health.multipart_uploaded_parts_total_bytes += record.completed_parts_total_bytes
            if record.completed_parts_error:
                health.multipart_parts_errors_count += 1

        return health

    def _collect_bucket_probes(self, bucket: str) -> dict[str, object]:
        return {
            "head_bucket": self._probe_head_bucket(bucket),
            "get_bucket_location": self._probe_bucket_location(bucket),
            "get_bucket_versioning": self._probe_bucket_versioning(bucket),
            "get_bucket_lifecycle_configuration": self._probe_call(
                "get_bucket_lifecycle_configuration",
                bucket,
                absent_codes={"NoSuchLifecycleConfiguration", "NoSuchBucketLifecycle", "404"},
            ),
            "get_bucket_encryption": self._probe_bucket_encryption(
                bucket,
            ),
            "get_public_access_block": self._probe_public_access_block(
                bucket,
            ),
            "get_bucket_tagging": self._probe_bucket_tagging(bucket),
            "get_bucket_logging": self._probe_bucket_logging(bucket),
            "get_bucket_request_payment": self._probe_call(
                "get_bucket_request_payment",
                bucket,
                absent_codes={"NoSuchRequestPayment", "404"},
            ),
            "get_bucket_ownership_controls": self._probe_bucket_ownership_controls(bucket),
            "get_object_lock_configuration": self._probe_call(
                "get_object_lock_configuration",
                bucket,
                absent_codes={"ObjectLockConfigurationNotFoundError", "NoSuchObjectLockConfiguration", "404"},
            ),
            "get_bucket_policy_status": self._probe_bucket_policy_status(bucket),
        }

    def _probe_head_bucket(self, bucket: str) -> dict[str, object]:
        try:
            response = self._client.head_bucket(Bucket=bucket)
        except ClientError as error:
            return self._error_probe(error, absent_codes=set())

        payload = response.get("ResponseMetadata", {})
        headers = payload.get("HTTPHeaders", {})
        return {
            "status": "ok",
            "payload": {
                "http_status_code": payload.get("HTTPStatusCode"),
                "request_id": payload.get("RequestId"),
                "host_id": headers.get("x-amz-id-2"),
                "bucket_region": headers.get("x-amz-bucket-region"),
            },
        }

    def _probe_call(self, method_name: str, bucket: str, absent_codes: set[str]) -> dict[str, object]:
        method = getattr(self._client, method_name)
        try:
            response = method(Bucket=bucket)
        except ClientError as error:
            return self._error_probe(error, absent_codes=absent_codes)
        except Exception as error:  # pragma: no cover - defensive for S3-compatible backends
            return {
                "status": "error",
                "error_type": type(error).__name__,
                "message": str(error),
            }

        return {
            "status": "ok",
            "payload": _strip_response_metadata(_serialize_value(response)),
        }

    def _error_probe(self, error: ClientError, absent_codes: set[str]) -> dict[str, object]:
        payload = error.response.get("Error", {})
        code = payload.get("Code", "Unknown")
        message = payload.get("Message", str(error))
        status = "absent" if code in absent_codes else "error"
        return {
            "status": status,
            "error_code": code,
            "message": message,
        }

    def _probe_bucket_location(self, bucket: str) -> dict[str, object]:
        probe = self._probe_call("get_bucket_location", bucket, absent_codes=set())
        if probe["status"] != "ok":
            return probe
        payload = probe["payload"]
        location_constraint = payload.get("LocationConstraint")
        payload["region_name"] = location_constraint or "us-east-1/default"
        return probe

    def _probe_bucket_versioning(self, bucket: str) -> dict[str, object]:
        probe = self._probe_call("get_bucket_versioning", bucket, absent_codes=set())
        if probe["status"] != "ok":
            return probe
        payload = probe["payload"]
        status = payload.get("Status")
        mfa_delete = payload.get("MFADelete")
        payload["versioning_enabled"] = status == "Enabled"
        payload["versioning_suspended"] = status == "Suspended"
        payload["mfa_delete_enabled"] = mfa_delete == "Enabled"
        return probe

    def _probe_bucket_encryption(self, bucket: str) -> dict[str, object]:
        probe = self._probe_call(
            "get_bucket_encryption",
            bucket,
            absent_codes={"ServerSideEncryptionConfigurationNotFoundError", "NoSuchServerSideEncryptionConfiguration", "404"},
        )
        if probe["status"] != "ok":
            return probe
        payload = probe["payload"]
        config = payload.get("ServerSideEncryptionConfiguration") or {}
        rules = config.get("Rules") or []
        payload["encryption_config_present"] = bool(rules)
        payload["rules_count"] = len(rules)
        if not rules:
            payload.pop("ServerSideEncryptionConfiguration", None)
        return probe

    def _probe_public_access_block(self, bucket: str) -> dict[str, object]:
        probe = self._probe_call(
            "get_public_access_block",
            bucket,
            absent_codes={"NoSuchPublicAccessBlockConfiguration", "NoSuchPublicAccessBlock", "404"},
        )
        if probe["status"] != "ok":
            return probe
        payload = probe["payload"]
        config = payload.get("PublicAccessBlockConfiguration") or {}
        payload["public_access_block_present"] = bool(config)
        payload["all_public_access_blocked"] = bool(config) and all(bool(value) for value in config.values())
        if not config:
            payload.pop("PublicAccessBlockConfiguration", None)
        return probe

    def _probe_bucket_tagging(self, bucket: str) -> dict[str, object]:
        probe = self._probe_call(
            "get_bucket_tagging",
            bucket,
            absent_codes={"NoSuchTagSet", "NoSuchTagging", "404"},
        )
        if probe["status"] != "ok":
            return probe
        payload = probe["payload"]
        tag_set = payload.get("TagSet") or []
        payload["tags_present"] = bool(tag_set)
        payload["tags_count"] = len(tag_set)
        if not tag_set:
            payload.pop("TagSet", None)
        return probe

    def _probe_bucket_logging(self, bucket: str) -> dict[str, object]:
        probe = self._probe_call("get_bucket_logging", bucket, absent_codes=set())
        if probe["status"] != "ok":
            return probe
        payload = probe["payload"]
        enabled = payload.get("LoggingEnabled") or {}
        payload["logging_enabled"] = bool(enabled)
        if not enabled:
            payload.pop("LoggingEnabled", None)
        return probe

    def _probe_bucket_ownership_controls(self, bucket: str) -> dict[str, object]:
        probe = self._probe_call(
            "get_bucket_ownership_controls",
            bucket,
            absent_codes={"OwnershipControlsNotFoundError", "NoSuchOwnershipControls", "404"},
        )
        if probe["status"] != "ok":
            return probe
        payload = probe["payload"]
        ownership_controls = payload.get("OwnershipControls") or {}
        rules = ownership_controls.get("Rules") or []
        payload["ownership_controls_present"] = bool(rules)
        payload["rules_count"] = len(rules)
        if not rules:
            payload.pop("OwnershipControls", None)
        return probe

    def _probe_bucket_policy_status(self, bucket: str) -> dict[str, object]:
        probe = self._probe_call(
            "get_bucket_policy_status",
            bucket,
            absent_codes={"NoSuchBucketPolicy", "404"},
        )
        if probe["status"] != "ok":
            return probe
        payload = probe["payload"]
        policy_status = payload.get("PolicyStatus") or {}
        payload["policy_status_present"] = bool(policy_status)
        if not policy_status:
            payload.pop("PolicyStatus", None)
        return probe


def _serialize_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _serialize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    return value


def _strip_response_metadata(payload: dict[str, object]) -> dict[str, object]:
    if "ResponseMetadata" not in payload:
        return payload
    stripped = dict(payload)
    stripped.pop("ResponseMetadata", None)
    return stripped
