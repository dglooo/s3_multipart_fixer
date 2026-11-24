from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace

from botocore.client import BaseClient
from botocore.exceptions import ClientError
from loguru import logger

from .models import MultipartUploadPart, MultipartUploadRecord, MultipartUploadSelector, ScanSummary


class MultipartUploadScanner:
    def __init__(self, client: BaseClient) -> None:
        self._client = client

    def iterate_uploads(
        self,
        bucket: str,
        prefix: str | None = None,
        page_size: int | None = None,
    ) -> Iterator[MultipartUploadRecord]:
        max_uploads = page_size or 1000
        key_marker: str | None = None
        upload_id_marker: str | None = None

        while True:
            request: dict[str, object] = {
                "Bucket": bucket,
                "MaxUploads": max_uploads,
            }
            if prefix:
                request["Prefix"] = prefix
            if key_marker:
                request["KeyMarker"] = key_marker
            if upload_id_marker:
                request["UploadIdMarker"] = upload_id_marker

            page = self._client.list_multipart_uploads(**request)
            uploads = page.get("Uploads", [])
            logger.debug(
                "list_multipart_uploads page: bucket={}, uploads_in_page={}, is_truncated={}, next_key_marker={}, next_upload_id_marker={}, request_key_marker={}, request_upload_id_marker={}",
                bucket,
                len(uploads),
                page.get("IsTruncated"),
                page.get("NextKeyMarker"),
                page.get("NextUploadIdMarker"),
                key_marker,
                upload_id_marker,
            )
            for upload in uploads:
                yield self._enrich_with_parts(MultipartUploadRecord.from_api(bucket=bucket, payload=upload))

            if not page.get("IsTruncated"):
                return
            key_marker = page.get("NextKeyMarker")
            upload_id_marker = page.get("NextUploadIdMarker")
            if (not key_marker or not upload_id_marker) and uploads:
                last_upload = uploads[-1]
                key_marker = key_marker or last_upload.get("Key")
                upload_id_marker = upload_id_marker or last_upload.get("UploadId")
            if not key_marker:
                return

    def filter_uploads(
        self,
        bucket: str,
        selector: MultipartUploadSelector,
        prefix: str | None = None,
        page_size: int | None = None,
        limit: int | None = None,
        summary: ScanSummary | None = None,
        progress_every: int | None = None,
        progress_callback: Callable[[ScanSummary], None] | None = None,
    ) -> Iterator[MultipartUploadRecord]:
        emitted = 0
        for record in self.iterate_uploads(bucket=bucket, prefix=prefix, page_size=page_size):
            if summary:
                summary.scanned += 1
                if progress_every and progress_callback and summary.scanned % progress_every == 0:
                    progress_callback(summary)
            if not selector.matches(record):
                continue
            if summary:
                summary.matched += 1
            yield record
            emitted += 1
            if summary:
                summary.emitted += 1
            if limit is not None and emitted >= limit:
                return

    def _enrich_with_parts(self, record: MultipartUploadRecord) -> MultipartUploadRecord:
        try:
            completed_parts = tuple(self._list_parts(record))
        except ClientError as error:
            payload = error.response.get("Error", {})
            code = payload.get("Code", "Unknown")
            message = payload.get("Message", str(error))
            return replace(
                record,
                completed_parts=(),
                completed_parts_count=0,
                completed_parts_total_bytes=0,
                completed_parts_error=f"{code}: {message}",
            )

        return replace(
            record,
            completed_parts=completed_parts,
            completed_parts_count=len(completed_parts),
            completed_parts_total_bytes=sum(part.size_bytes for part in completed_parts),
            completed_parts_error=None,
        )

    def _list_parts(self, record: MultipartUploadRecord) -> Iterator[MultipartUploadPart]:
        part_number_marker = 0
        while True:
            page = self._client.list_parts(
                Bucket=record.bucket,
                Key=record.key,
                UploadId=record.upload_id,
                MaxParts=1000,
                PartNumberMarker=part_number_marker,
            )
            for part in page.get("Parts", []):
                yield MultipartUploadPart.from_api(part)
            if not page.get("IsTruncated"):
                return
            part_number_marker = page.get("NextPartNumberMarker", 0)
