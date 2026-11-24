from __future__ import annotations

import sys
from typing import Any

from loguru import logger


def configure_logging(verbose: bool = False) -> None:
    logger.remove()
    level = "DEBUG" if verbose else "INFO"
    logger.add(sys.stderr, level=level)


def log_aws_runtime_config(aws_config: Any) -> None:
    logger.info(
        "aws config: endpoint_url={}, region={}, profile={}, access_key_id={}, connect_timeout={}, read_timeout={}, total_max_attempts={}, signature_version={}, addressing_style={}, payload_signing_enabled={}, http_upload_timeout={}",
        aws_config.endpoint_url,
        aws_config.region,
        aws_config.profile or "-",
        mask_secret(aws_config.access_key_id),
        aws_config.connect_timeout,
        aws_config.read_timeout,
        aws_config.total_max_attempts,
        aws_config.signature_version,
        aws_config.addressing_style,
        aws_config.payload_signing_enabled,
        aws_config.http_upload_timeout,
    )


def log_bucket_config(bucket: str, source: str) -> None:
    logger.info("bucket config: bucket={}, source={}", bucket, source)


def mask_secret(value: str | None) -> str:
    if not value:
        return "-"
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}***{value[-3:]}"
