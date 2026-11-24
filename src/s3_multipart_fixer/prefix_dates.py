from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger


@dataclass(slots=True, frozen=True)
class PrefixTarget:
    path: str
    objects_count: int
    total_size_bytes: int
    total_size_gb: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "objects_count": self.objects_count,
            "total_size_bytes": self.total_size_bytes,
            "total_size_gb": self.total_size_gb,
        }


@dataclass(slots=True, frozen=True)
class PrefixDateResult:
    path: str
    expected_objects_count: int
    expected_total_size_bytes: int
    expected_total_size_gb: float
    observed_objects_count: int
    earliest_object_last_modified: str | None
    latest_object_last_modified: str | None
    status: str
    error_type: str | None
    error_message: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "expected_objects_count": self.expected_objects_count,
            "expected_total_size_bytes": self.expected_total_size_bytes,
            "expected_total_size_gb": self.expected_total_size_gb,
            "observed_objects_count": self.observed_objects_count,
            "earliest_object_last_modified": self.earliest_object_last_modified,
            "latest_object_last_modified": self.latest_object_last_modified,
            "status": self.status,
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


@dataclass(slots=True, frozen=True)
class PrefixDateCollectionSummary:
    processed_prefixes_count: int
    succeeded_prefixes_count: int
    failed_prefixes_count: int


def load_leaf_prefix_targets(input_json_path: str, *, anchor_name: str = "Data") -> list[PrefixTarget]:
    payload = json.loads(Path(input_json_path).read_text(encoding="utf-8"))
    targets: dict[str, PrefixTarget] = {}
    for node in payload.get("directory_tree", []):
        _walk_tree_for_anchor(node=node, anchor_name=anchor_name, targets=targets)
    return sorted(targets.values(), key=lambda item: item.path)


def _walk_tree_for_anchor(*, node: dict[str, Any], anchor_name: str, targets: dict[str, PrefixTarget]) -> None:
    children = node.get("children") or []
    if node.get("name") == anchor_name:
        _collect_leaf_descendants(node=node, targets=targets)
    for child in children:
        _walk_tree_for_anchor(node=child, anchor_name=anchor_name, targets=targets)


def _collect_leaf_descendants(*, node: dict[str, Any], targets: dict[str, PrefixTarget]) -> None:
    children = node.get("children") or []
    if not children:
        path = node.get("path")
        if isinstance(path, str) and path:
            targets[path] = PrefixTarget(
                path=path,
                objects_count=int(node.get("objects_count", 0) or 0),
                total_size_bytes=int(node.get("total_size_bytes", 0) or 0),
                total_size_gb=float(node.get("total_size_gb", 0.0) or 0.0),
            )
        return
    for child in children:
        _collect_leaf_descendants(node=child, targets=targets)


class AsyncPrefixDateCollector:
    def __init__(self, client) -> None:
        self._client = client

    async def collect(
        self,
        *,
        bucket: str,
        targets: list[PrefixTarget],
        concurrency: int = 20,
        page_size: int = 1000,
        progress_every: int | None = None,
        progress_callback=None,
        result_callback=None,
    ) -> PrefixDateCollectionSummary:
        pending: set[asyncio.Task] = set()
        processed = 0
        succeeded = 0
        failed = 0
        total = len(targets)

        for target in targets:
            pending.add(asyncio.create_task(self._collect_prefix_dates(bucket=bucket, target=target, page_size=page_size)))
            if len(pending) < concurrency:
                continue
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            processed, succeeded, failed = self._handle_completed_tasks(
                done=done,
                total=total,
                processed=processed,
                succeeded=succeeded,
                failed=failed,
                progress_every=progress_every,
                progress_callback=progress_callback,
                result_callback=result_callback,
            )

        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            processed, succeeded, failed = self._handle_completed_tasks(
                done=done,
                total=total,
                processed=processed,
                succeeded=succeeded,
                failed=failed,
                progress_every=progress_every,
                progress_callback=progress_callback,
                result_callback=result_callback,
            )

        return PrefixDateCollectionSummary(
            processed_prefixes_count=processed,
            succeeded_prefixes_count=succeeded,
            failed_prefixes_count=failed,
        )

    def _handle_completed_tasks(
        self,
        *,
        done: set[asyncio.Task],
        total: int,
        processed: int,
        succeeded: int,
        failed: int,
        progress_every: int | None,
        progress_callback,
        result_callback,
    ) -> tuple[int, int, int]:
        for task in done:
            result = task.result()
            if result_callback is not None:
                result_callback(result)
            processed += 1
            if result.status == "ok":
                succeeded += 1
            else:
                failed += 1
            if progress_every and progress_callback and (processed % progress_every == 0 or processed == total):
                progress_callback(processed, total, succeeded, failed)
        return processed, succeeded, failed

    async def _collect_prefix_dates(
        self,
        *,
        bucket: str,
        target: PrefixTarget,
        page_size: int,
    ) -> PrefixDateResult:
        continuation_token: str | None = None
        earliest = None
        latest = None
        observed_objects_count = 0
        pages_processed = 0

        logger.debug(
            "collect-prefix-dates prefix started: bucket={}, path={}, expected_objects_count={}, expected_total_size_gb={}",
            bucket,
            target.path,
            target.objects_count,
            target.total_size_gb,
        )
        try:
            while True:
                request: dict[str, Any] = {
                    "Bucket": bucket,
                    "Prefix": target.path,
                    "MaxKeys": page_size,
                }
                if continuation_token:
                    request["ContinuationToken"] = continuation_token
                page = await self._client.list_objects_v2(**request)
                pages_processed += 1
                for item in page.get("Contents", []):
                    last_modified = item.get("LastModified")
                    observed_objects_count += 1
                    if last_modified is None:
                        continue
                    if earliest is None or last_modified < earliest:
                        earliest = last_modified
                    if latest is None or last_modified > latest:
                        latest = last_modified
                if not page.get("IsTruncated"):
                    break
                continuation_token = page.get("NextContinuationToken")
                if not continuation_token:
                    break
        except Exception as error:
            logger.exception(
                "collect-prefix-dates prefix failed: bucket={}, path={}, pages_processed={}, observed_objects_count={}",
                bucket,
                target.path,
                pages_processed,
                observed_objects_count,
            )
            return PrefixDateResult(
                path=target.path,
                expected_objects_count=target.objects_count,
                expected_total_size_bytes=target.total_size_bytes,
                expected_total_size_gb=target.total_size_gb,
                observed_objects_count=observed_objects_count,
                earliest_object_last_modified=earliest.isoformat() if earliest else None,
                latest_object_last_modified=latest.isoformat() if latest else None,
                status="error",
                error_type=error.__class__.__name__,
                error_message=str(error),
            )

        result = PrefixDateResult(
            path=target.path,
            expected_objects_count=target.objects_count,
            expected_total_size_bytes=target.total_size_bytes,
            expected_total_size_gb=target.total_size_gb,
            observed_objects_count=observed_objects_count,
            earliest_object_last_modified=earliest.isoformat() if earliest else None,
            latest_object_last_modified=latest.isoformat() if latest else None,
            status="ok",
            error_type=None,
            error_message=None,
        )
        logger.debug(
            "collect-prefix-dates prefix completed: bucket={}, path={}, pages_processed={}, observed_objects_count={}, earliest_object_last_modified={}, latest_object_last_modified={}",
            bucket,
            target.path,
            pages_processed,
            observed_objects_count,
            result.earliest_object_last_modified,
            result.latest_object_last_modified,
        )
        return result
