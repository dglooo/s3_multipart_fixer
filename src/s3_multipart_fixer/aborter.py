from __future__ import annotations

import time

from botocore.client import BaseClient
from botocore.exceptions import ClientError

from .models import AbortResult, MultipartUploadRecord


class MultipartUploadAborter:
    def __init__(self, client: BaseClient) -> None:
        self._client = client

    def abort_upload(
        self,
        record: MultipartUploadRecord,
        *,
        execute: bool,
        verify: bool,
        max_attempts: int = 3,
        retry_delay_seconds: float = 1.0,
    ) -> AbortResult:
        if not execute:
            return AbortResult(
                record=record,
                requested=False,
                success=False,
                verified=False,
                attempts=0,
                message="dry-run",
            )

        last_error_code: str | None = None
        last_message = "abort requested"

        for attempt in range(1, max_attempts + 1):
            try:
                self._client.abort_multipart_upload(
                    Bucket=record.bucket,
                    Key=record.key,
                    UploadId=record.upload_id,
                )
            except ClientError as error:
                code = error.response.get("Error", {}).get("Code")
                message = error.response.get("Error", {}).get("Message", str(error))
                return AbortResult(
                    record=record,
                    requested=True,
                    success=False,
                    verified=False,
                    attempts=attempt,
                    message=message,
                    error_code=code,
                )

            if not verify:
                return AbortResult(
                    record=record,
                    requested=True,
                    success=True,
                    verified=False,
                    attempts=attempt,
                    message="abort accepted; verification skipped",
                )

            if self._is_upload_gone(record):
                return AbortResult(
                    record=record,
                    requested=True,
                    success=True,
                    verified=True,
                    attempts=attempt,
                    message="upload no longer exists",
                )

            last_error_code = "UploadStillPresent"
            last_message = "upload still present after abort, retry needed"
            time.sleep(retry_delay_seconds)

        return AbortResult(
            record=record,
            requested=True,
            success=False,
            verified=False,
            attempts=max_attempts,
            message=last_message,
            error_code=last_error_code,
        )

    def _is_upload_gone(self, record: MultipartUploadRecord) -> bool:
        try:
            self._client.list_parts(
                Bucket=record.bucket,
                Key=record.key,
                UploadId=record.upload_id,
                MaxParts=1,
            )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in {"NoSuchUpload", "404", "NotFound"}:
                return True
            raise
        return False
