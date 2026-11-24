from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
import json
import os

from s3_uploader.common import DEFAULT_STORAGE_CLASS, normalize_storage_class
from s3_uploader.models.s3 import S3ConnectionConfig


@dataclass(frozen=True)
class ConnectionSettings:
    region: str
    endpoint: str
    access_key: Optional[str] = None
    secret_key: Optional[str] = None
    access_key_env: Optional[str] = "AWS_ACCESS_KEY_ID"
    secret_key_env: Optional[str] = "AWS_SECRET_ACCESS_KEY"

    def build_connection_config(self) -> S3ConnectionConfig:
        access_key = self._resolve_credential(
            direct_value=self.access_key,
            env_name=self.access_key_env,
            default_env_name="AWS_ACCESS_KEY_ID",
            credential_label="access key",
        )
        secret_key = self._resolve_credential(
            direct_value=self.secret_key,
            env_name=self.secret_key_env,
            default_env_name="AWS_SECRET_ACCESS_KEY",
            credential_label="secret key",
        )
        return S3ConnectionConfig(
            access_key=access_key,
            secret_key=secret_key,
            region=self.region,
            endpoint=self.endpoint,
        )

    def _resolve_credential(
        self,
        direct_value: Optional[str],
        env_name: Optional[str],
        default_env_name: str,
        credential_label: str,
    ) -> str:
        if direct_value:
            return direct_value

        candidate_env_name = env_name or default_env_name
        env_value = None
        if candidate_env_name:
            env_value = os.getenv(candidate_env_name)
        if env_value:
            return env_value

        if candidate_env_name and candidate_env_name != default_env_name:
            return candidate_env_name

        raise KeyError(
            f"S3 {credential_label} is not configured. "
            f"Use '{default_env_name}' in environment or set direct '{credential_label.replace(' ', '_')}' in config."
        )


@dataclass(frozen=True)
class BucketUploadPlan:
    bucket: str
    upload_count: int
    storage_class: str = DEFAULT_STORAGE_CLASS
    key_prefix: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.bucket:
            raise ValueError("Bucket name is required in upload plan")
        if self.upload_count <= 0:
            raise ValueError(f"upload_count must be > 0 for bucket '{self.bucket}'")
        object.__setattr__(self, "storage_class", normalize_storage_class(self.storage_class))


@dataclass(frozen=True)
class UploadPlan:
    connection: ConnectionSettings
    file_path: Path
    mode: str
    chunk_size_mb: int
    bucket_plans: list[BucketUploadPlan] = field(default_factory=list)
    create_bucket: bool = True
    report_dir: Optional[Path] = None
    key_prefix: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.mode:
            raise ValueError("Upload mode is required")
        if not self.bucket_plans:
            raise ValueError("At least one bucket plan is required")
        if self.mode.strip().lower() not in {"http", "multipart"}:
            raise ValueError(f"Unsupported upload mode: {self.mode}")


@dataclass(frozen=True)
class BucketDeletePlan:
    bucket: str
    delete_count: int
    storage_class: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.bucket:
            raise ValueError("Bucket name is required in delete plan")
        if self.delete_count <= 0:
            raise ValueError(f"delete_count must be > 0 for bucket '{self.bucket}'")
        if self.storage_class:
            object.__setattr__(self, "storage_class", normalize_storage_class(self.storage_class))


@dataclass(frozen=True)
class DeletePlan:
    connection: ConnectionSettings
    bucket_plans: list[BucketDeletePlan] = field(default_factory=list)
    report_dir: Optional[Path] = None
    delete_oldest_first: bool = True

    def __post_init__(self) -> None:
        if not self.bucket_plans:
            raise ValueError("At least one bucket delete plan is required")


def load_upload_plan(config_path: str) -> UploadPlan:
    config_file = Path(config_path).resolve()
    raw_config = _load_json(config_file)

    connection = _parse_connection_settings(raw_config.get("connection", {}))
    upload_settings = raw_config.get("upload", {})
    file_path = _resolve_path(config_file.parent, upload_settings.get("file_path", "100M.bin"))
    report_dir = _optional_path(config_file.parent, raw_config.get("report_dir"))

    bucket_plans = [
        BucketUploadPlan(
            bucket=item["bucket"],
            upload_count=int(item["upload_count"]),
            storage_class=item.get("storage_class", DEFAULT_STORAGE_CLASS),
            key_prefix=item.get("key_prefix"),
        )
        for item in upload_settings.get("bucket_plans", [])
    ]

    return UploadPlan(
        connection=connection,
        file_path=file_path,
        mode=upload_settings.get("mode", "http"),
        chunk_size_mb=int(upload_settings.get("chunk_size_mb", 100)),
        bucket_plans=bucket_plans,
        create_bucket=bool(upload_settings.get("create_bucket", True)),
        report_dir=report_dir,
        key_prefix=upload_settings.get("key_prefix"),
    )


def load_delete_plan(config_path: str) -> DeletePlan:
    config_file = Path(config_path).resolve()
    raw_config = _load_json(config_file)

    connection = _parse_connection_settings(raw_config.get("connection", {}))
    delete_settings = raw_config.get("delete", {})
    report_dir = _optional_path(config_file.parent, raw_config.get("report_dir"))

    bucket_plans = [
        BucketDeletePlan(
            bucket=item["bucket"],
            delete_count=int(item["delete_count"]),
            storage_class=item.get("storage_class"),
        )
        for item in delete_settings.get("bucket_plans", [])
    ]

    return DeletePlan(
        connection=connection,
        bucket_plans=bucket_plans,
        report_dir=report_dir,
        delete_oldest_first=bool(delete_settings.get("delete_oldest_first", True)),
    )


def _parse_connection_settings(raw_connection: dict[str, Any]) -> ConnectionSettings:
    return ConnectionSettings(
        region=raw_connection["region"],
        endpoint=raw_connection["endpoint"],
        access_key_env=raw_connection.get("access_key_env", "AWS_ACCESS_KEY_ID"),
        secret_key_env=raw_connection.get("secret_key_env", "AWS_SECRET_ACCESS_KEY"),
    )


def _optional_path(base_dir: Path, path_value: Optional[str]) -> Optional[Path]:
    if not path_value:
        return None
    return _resolve_path(base_dir, path_value)


def _resolve_path(base_dir: Path, path_value: str) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file_handle:
        return json.load(file_handle)
