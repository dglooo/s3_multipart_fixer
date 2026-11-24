from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

import boto3
from botocore.config import Config
from botocore.client import BaseClient

try:
    import aioboto3
    from aiobotocore.config import AioConfig
except ImportError:  # pragma: no cover - optional async dependency
    aioboto3 = None
    AioConfig = Any


@dataclass(slots=True, frozen=True)
class AwsClientFactory:
    profile: str | None = None
    region: str | None = None
    endpoint_url: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None
    connect_timeout: int = 5
    read_timeout: int = 60
    total_max_attempts: int = 10
    signature_version: str = "s3v4"
    addressing_style: str = "path"
    payload_signing_enabled: bool = True
    http_upload_timeout: int = 60

    def build_s3_client(self) -> BaseClient:
        session = boto3.Session(**self._build_session_kwargs())
        return session.client(
            "s3",
            endpoint_url=self.endpoint_url,
            config=self._build_sync_config(),
        )

    def build_async_s3_client(self):
        if aioboto3 is None:
            raise RuntimeError(
                "Async S3 mode requires aioboto3/aiobotocore. "
                "Install dependencies with: ./.venv/bin/pip install -r requirements.txt"
            )
        session = aioboto3.Session(**self._build_session_kwargs())
        return session.client(
            "s3",
            endpoint_url=self.endpoint_url,
            config=self._build_async_config(),
        )

    def _build_session_kwargs(self) -> dict[str, str]:
        session_kwargs: dict[str, str] = {}
        if self.profile:
            session_kwargs["profile_name"] = self.profile
        if self.region:
            session_kwargs["region_name"] = self.region
        if self.access_key_id and self.secret_access_key:
            session_kwargs["aws_access_key_id"] = self.access_key_id
            session_kwargs["aws_secret_access_key"] = self.secret_access_key
        if self.session_token:
            session_kwargs["aws_session_token"] = self.session_token
        return session_kwargs

    def _build_sync_config(self) -> Config:
        return Config(
            retries={"total_max_attempts": self.total_max_attempts, "mode": "standard"},
            connect_timeout=self.connect_timeout,
            read_timeout=self.read_timeout,
            signature_version=self.signature_version,
            s3={
                "addressing_style": self.addressing_style,
                "payload_signing_enabled": self.payload_signing_enabled,
            },
        )

    def _build_async_config(self) -> AioConfig:
        return AioConfig(
            retries={"total_max_attempts": self.total_max_attempts, "mode": "standard"},
            connect_timeout=self.connect_timeout,
            read_timeout=self.read_timeout,
            signature_version=self.signature_version,
            s3={
                "addressing_style": self.addressing_style,
                "payload_signing_enabled": self.payload_signing_enabled,
            },
        )


def resolve_aws_runtime_config(
    *,
    profile: str | None,
    region: str | None,
    endpoint_url: str | None,
    access_key_id: str | None,
    secret_access_key: str | None,
    session_token: str | None,
    connect_timeout: int | None,
    read_timeout: int | None,
    total_max_attempts: int | None,
    default_total_max_attempts: int,
    signature_version: str | None,
    addressing_style: str | None,
    payload_signing_enabled: bool | None,
    http_upload_timeout: int | None,
) -> AwsClientFactory:
    resolved_access_key_id = _normalize_value(
        access_key_id or _first_env("S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID")
    )
    resolved_secret_access_key = _normalize_value(
        secret_access_key or _first_env("S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY")
    )
    resolved_session_token = _normalize_value(
        session_token or _first_env("S3_SESSION_TOKEN", "AWS_SESSION_TOKEN")
    )
    resolved_profile = _normalize_value(profile or _first_env("AWS_PROFILE"))

    # Direct credentials should win over profile-based resolution.
    if resolved_access_key_id and resolved_secret_access_key:
        resolved_profile = _normalize_value(profile)

    return AwsClientFactory(
        profile=resolved_profile,
        region=_normalize_value(region or _first_env("AWS_REGION", "AWS_DEFAULT_REGION", "S3_REGION")),
        endpoint_url=_normalize_value(endpoint_url or _first_env("S3_ENDPOINT", "AWS_ENDPOINT_URL_S3")),
        access_key_id=resolved_access_key_id,
        secret_access_key=resolved_secret_access_key,
        session_token=resolved_session_token,
        connect_timeout=_normalize_int(connect_timeout, "S3_CONNECT_TIMEOUT", default=5),
        read_timeout=_normalize_int(read_timeout, "S3_READ_TIMEOUT", default=60),
        total_max_attempts=_normalize_int(
            total_max_attempts,
            "S3_TOTAL_MAX_ATTEMPTS",
            default=default_total_max_attempts,
        ),
        signature_version=_normalize_value(signature_version or _first_env("S3_SIGNATURE_VERSION")) or "s3v4",
        addressing_style=_normalize_addressing_style(addressing_style or _first_env("S3_ADDRESSING_STYLE")),
        payload_signing_enabled=_normalize_bool(
            payload_signing_enabled,
            "S3_PAYLOAD_SIGNING_ENABLED",
            default=True,
        ),
        http_upload_timeout=_normalize_int(
            http_upload_timeout,
            "S3_HTTP_UPLOAD_TIMEOUT",
            default=60,
        ),
    )


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def _normalize_value(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {"'", '"'}:
        normalized = normalized[1:-1].strip()
    return normalized or None


def _normalize_int(value: int | None, env_name: str, default: int) -> int:
    if value is not None:
        return value
    env_value = _first_env(env_name)
    if env_value is None:
        return default
    return int(env_value)


def _normalize_bool(value: bool | None, env_name: str, default: bool) -> bool:
    if value is not None:
        return value
    env_value = _first_env(env_name)
    if env_value is None:
        return default
    normalized = env_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{env_name} must be a boolean value")


def _normalize_addressing_style(value: str | None) -> str:
    normalized = _normalize_value(value) or "path"
    if normalized not in {"auto", "virtual", "path"}:
        raise ValueError("S3_ADDRESSING_STYLE must be one of: auto, virtual, path")
    return normalized
