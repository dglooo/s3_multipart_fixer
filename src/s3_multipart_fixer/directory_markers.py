from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from botocore.client import BaseClient

from .object_counter import BucketObjectCounter, DirectoryMarkerHeadSummary


class DirectoryMarkerInspector:
    def __init__(self, client: BaseClient) -> None:
        self._client = client
        self._counter = BucketObjectCounter(client)

    def inspect_paths(
        self,
        *,
        bucket: str,
        paths: Iterable[str],
        progress_every: int | None = None,
        progress_callback: Callable[[int], None] | None = None,
    ) -> list[DirectoryMarkerHeadSummary]:
        unique_paths = []
        seen: set[str] = set()
        for path in paths:
            normalized = _normalize_marker_path(path)
            if normalized in seen:
                continue
            seen.add(normalized)
            unique_paths.append(normalized)

        results: list[DirectoryMarkerHeadSummary] = []
        for index, path in enumerate(unique_paths, start=1):
            results.append(self._counter.head_directory_marker(bucket=bucket, key=path))
            if progress_every and progress_callback and index % progress_every == 0:
                progress_callback(index)
        return results

    def discover_subdirectories(
        self,
        *,
        bucket: str,
        prefix: str,
        page_size: int = 1000,
        include_root: bool = True,
    ) -> list[str]:
        root_prefix = _normalize_marker_path(prefix)
        discovered: list[str] = [root_prefix] if include_root else []
        seen: set[str] = {root_prefix}
        queue: deque[str] = deque([root_prefix])

        while queue:
            current_prefix = queue.popleft()
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(
                Bucket=bucket,
                Prefix=current_prefix,
                Delimiter="/",
                PaginationConfig={"PageSize": page_size},
            ):
                for item in page.get("CommonPrefixes", []):
                    child_prefix = item.get("Prefix")
                    if not child_prefix:
                        continue
                    child_prefix = _normalize_marker_path(child_prefix)
                    if child_prefix in seen:
                        continue
                    seen.add(child_prefix)
                    discovered.append(child_prefix)
                    queue.append(child_prefix)

        return discovered


def load_marker_paths_from_json(input_path: str) -> list[str]:
    payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
    paths: list[str] = []

    if isinstance(payload, list):
        _collect_paths(payload, paths)
        return paths

    if isinstance(payload, dict):
        _collect_paths(payload, paths)
        return paths

    raise ValueError("Input JSON must be a list or object")


def _collect_paths(payload, paths: list[str]) -> None:
    if isinstance(payload, str):
        paths.append(payload)
        return
    if isinstance(payload, list):
        for item in payload:
            _collect_paths(item, paths)
        return
    if isinstance(payload, dict):
        if isinstance(payload.get("path"), str):
            paths.append(payload["path"])
        for value in payload.values():
            _collect_paths(value, paths)


def _normalize_marker_path(path: str) -> str:
    stripped = path.strip()
    if not stripped:
        raise ValueError("Directory marker path must not be empty")
    return stripped if stripped.endswith("/") else f"{stripped}/"


def build_marker_report(
    *,
    bucket: str,
    mode: str,
    source: str,
    markers: list[DirectoryMarkerHeadSummary],
    discovered_paths_count: int | None = None,
) -> dict[str, object]:
    existing = [item.to_dict() for item in markers if item.exists]
    missing = [item.to_dict() for item in markers if not item.exists]
    return {
        "bucket": bucket,
        "mode": mode,
        "source": source,
        "discovered_paths_count": discovered_paths_count,
        "processed_paths_count": len(markers),
        "existing_markers_count": len(existing),
        "missing_markers_count": len(missing),
        "existing_markers": existing,
        "missing_markers": missing,
    }
