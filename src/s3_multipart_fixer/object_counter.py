from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from botocore.client import BaseClient
from botocore.exceptions import ClientError


@dataclass(slots=True)
class ObjectCountSummary:
    bucket: str
    prefix: str | None = None
    objects_count: int = 0
    total_size_bytes: int = 0
    directory_depth: int = 1
    directories: dict[str, "DirectoryUsageSummary"] = field(default_factory=dict)
    directory_tree_root: "DirectoryTreeNode" | None = None
    directory_tree_index: dict[str, "DirectoryTreeNode"] = field(default_factory=dict)
    root_objects_count: int = 0
    root_total_size_bytes: int = 0
    earliest_object_last_modified: datetime | None = None
    latest_object_last_modified: datetime | None = None
    prefix_marker_last_modified: datetime | None = None

    @property
    def total_size_gb(self) -> float:
        return round(self.total_size_bytes / (1024**3), 3)

    def to_dict(self) -> dict[str, int | float | str | None]:
        directory_items = sorted(
            (item.to_dict() for item in self.directories.values()),
            key=lambda item: (-item["total_size_bytes"], item["directory"]),
        )
        return {
            "bucket": self.bucket,
            "prefix": self.prefix,
            "objects_count": self.objects_count,
            "total_size_bytes": self.total_size_bytes,
            "total_size_gb": self.total_size_gb,
            "directory_depth": self.directory_depth,
            "root_objects_count": self.root_objects_count,
            "root_total_size_bytes": self.root_total_size_bytes,
            "root_total_size_gb": round(self.root_total_size_bytes / (1024**3), 3),
            "earliest_object_last_modified": self.earliest_object_last_modified.isoformat() if self.earliest_object_last_modified else None,
            "latest_object_last_modified": self.latest_object_last_modified.isoformat() if self.latest_object_last_modified else None,
            "prefix_marker_last_modified": self.prefix_marker_last_modified.isoformat() if self.prefix_marker_last_modified else None,
            "directories": directory_items,
            "directory_tree": self.directory_tree_root.to_children_list() if self.directory_tree_root else [],
        }


@dataclass(slots=True)
class DirectoryUsageSummary:
    directory: str
    objects_count: int = 0
    total_size_bytes: int = 0
    earliest_object_last_modified: datetime | None = None
    latest_object_last_modified: datetime | None = None
    marker_last_modified: datetime | None = None

    @property
    def total_size_gb(self) -> float:
        return round(self.total_size_bytes / (1024**3), 3)

    def to_dict(self) -> dict[str, int | float | str]:
        return {
            "directory": self.directory,
            "objects_count": self.objects_count,
            "total_size_bytes": self.total_size_bytes,
            "total_size_gb": self.total_size_gb,
            "earliest_object_last_modified": self.earliest_object_last_modified.isoformat() if self.earliest_object_last_modified else None,
            "latest_object_last_modified": self.latest_object_last_modified.isoformat() if self.latest_object_last_modified else None,
            "marker_last_modified": self.marker_last_modified.isoformat() if self.marker_last_modified else None,
        }


@dataclass(slots=True)
class DirectoryTreeNode:
    name: str
    path: str
    objects_count: int = 0
    total_size_bytes: int = 0
    earliest_object_last_modified: datetime | None = None
    latest_object_last_modified: datetime | None = None
    marker_last_modified: datetime | None = None
    children: dict[str, "DirectoryTreeNode"] = field(default_factory=dict)

    @property
    def total_size_gb(self) -> float:
        return round(self.total_size_bytes / (1024**3), 3)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": self.path,
            "objects_count": self.objects_count,
            "total_size_bytes": self.total_size_bytes,
            "total_size_gb": self.total_size_gb,
            "earliest_object_last_modified": self.earliest_object_last_modified.isoformat() if self.earliest_object_last_modified else None,
            "latest_object_last_modified": self.latest_object_last_modified.isoformat() if self.latest_object_last_modified else None,
            "marker_last_modified": self.marker_last_modified.isoformat() if self.marker_last_modified else None,
            "children": self.to_children_list(),
        }

    def to_children_list(self) -> list[dict[str, object]]:
        return [
            child.to_dict()
            for child in sorted(
                self.children.values(),
                key=lambda item: (-item.total_size_bytes, item.path),
            )
        ]


@dataclass(slots=True)
class ObjectVersionCountSummary:
    bucket: str
    prefix: str | None = None
    current_versions_count: int = 0
    noncurrent_versions_count: int = 0
    delete_markers_count: int = 0
    current_versions_size_bytes: int = 0
    noncurrent_versions_size_bytes: int = 0

    @property
    def total_versions_count(self) -> int:
        return self.current_versions_count + self.noncurrent_versions_count

    @property
    def total_entries_count(self) -> int:
        return self.total_versions_count + self.delete_markers_count

    @property
    def total_size_bytes(self) -> int:
        return self.current_versions_size_bytes + self.noncurrent_versions_size_bytes

    @property
    def total_size_gb(self) -> float:
        return round(self.total_size_bytes / (1024**3), 3)

    def to_dict(self) -> dict[str, int | float | str | None]:
        return {
            "bucket": self.bucket,
            "prefix": self.prefix,
            "current_versions_count": self.current_versions_count,
            "noncurrent_versions_count": self.noncurrent_versions_count,
            "delete_markers_count": self.delete_markers_count,
            "total_versions_count": self.total_versions_count,
            "total_entries_count": self.total_entries_count,
            "current_versions_size_bytes": self.current_versions_size_bytes,
            "noncurrent_versions_size_bytes": self.noncurrent_versions_size_bytes,
            "total_size_bytes": self.total_size_bytes,
            "total_size_gb": self.total_size_gb,
        }


@dataclass(slots=True)
class DirectoryMarkerHeadSummary:
    bucket: str
    requested_key: str
    marker_key: str
    exists: bool
    resolved_via: str = "marker"
    last_modified: datetime | None = None
    content_length: int | None = None
    etag: str | None = None
    storage_class: str | None = None
    sample_object_key: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    error_code: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "bucket": self.bucket,
            "requested_key": self.requested_key,
            "marker_key": self.marker_key,
            "exists": self.exists,
            "resolved_via": self.resolved_via,
            "last_modified": self.last_modified.isoformat() if self.last_modified else None,
            "content_length": self.content_length,
            "etag": self.etag,
            "storage_class": self.storage_class,
            "sample_object_key": self.sample_object_key,
            "metadata": self.metadata,
            "error_code": self.error_code,
            "message": self.message,
        }


class BucketObjectCounter:
    def __init__(self, client: BaseClient) -> None:
        self._client = client

    def count_objects(
        self,
        *,
        bucket: str,
        prefix: str | None = None,
        page_size: int = 1000,
        directory_depth: int = 1,
        limit: int | None = None,
        progress_every: int | None = None,
        progress_callback=None,
    ) -> ObjectCountSummary:
        paginator = self._client.get_paginator("list_objects_v2")
        paginate_kwargs: dict[str, object] = {
            "Bucket": bucket,
            "PaginationConfig": {"PageSize": page_size},
        }
        if prefix:
            paginate_kwargs["Prefix"] = prefix

        summary = ObjectCountSummary(
            bucket=bucket,
            prefix=prefix,
            directory_depth=directory_depth,
            directory_tree_root=DirectoryTreeNode(name="", path=""),
        )
        for page in paginator.paginate(**paginate_kwargs):
            for item in page.get("Contents", []):
                key = item.get("Key", "")
                size = item.get("Size", 0)
                last_modified = item.get("LastModified")
                summary.objects_count += 1
                summary.total_size_bytes += size
                summary.earliest_object_last_modified = _pick_earliest(summary.earliest_object_last_modified, last_modified)
                summary.latest_object_last_modified = _pick_latest(summary.latest_object_last_modified, last_modified)
                if directory_depth > 0:
                    directory_parts = _extract_directory_parts(key=key, prefix=prefix, depth=directory_depth)
                    if directory_parts:
                        directory = "/".join(directory_parts) + "/"
                        bucket_summary = summary.directories.setdefault(directory, DirectoryUsageSummary(directory=directory))
                        bucket_summary.objects_count += 1
                        bucket_summary.total_size_bytes += size
                        bucket_summary.earliest_object_last_modified = _pick_earliest(bucket_summary.earliest_object_last_modified, last_modified)
                        bucket_summary.latest_object_last_modified = _pick_latest(bucket_summary.latest_object_last_modified, last_modified)
                        _update_directory_tree(summary, directory_parts, size, last_modified)
                    else:
                        summary.root_objects_count += 1
                        summary.root_total_size_bytes += size
                if progress_every and progress_callback and summary.objects_count % progress_every == 0:
                    progress_callback(summary)
                if limit is not None and summary.objects_count >= limit:
                    self._hydrate_marker_last_modified(summary)
                    return summary
        self._hydrate_marker_last_modified(summary)
        return summary

    def count_object_versions(
        self,
        *,
        bucket: str,
        prefix: str | None = None,
        page_size: int = 1000,
        progress_every: int | None = None,
        progress_callback=None,
    ) -> ObjectVersionCountSummary:
        paginator = self._client.get_paginator("list_object_versions")
        paginate_kwargs: dict[str, object] = {
            "Bucket": bucket,
            "PaginationConfig": {"PageSize": page_size},
        }
        if prefix:
            paginate_kwargs["Prefix"] = prefix

        summary = ObjectVersionCountSummary(bucket=bucket, prefix=prefix)
        for page in paginator.paginate(**paginate_kwargs):
            for version in page.get("Versions", []):
                if version.get("IsLatest"):
                    summary.current_versions_count += 1
                    summary.current_versions_size_bytes += version.get("Size", 0)
                else:
                    summary.noncurrent_versions_count += 1
                    summary.noncurrent_versions_size_bytes += version.get("Size", 0)
                if progress_every and progress_callback and summary.total_entries_count % progress_every == 0:
                    progress_callback(summary)

            for _marker in page.get("DeleteMarkers", []):
                summary.delete_markers_count += 1
                if progress_every and progress_callback and summary.total_entries_count % progress_every == 0:
                    progress_callback(summary)

        return summary

    def head_directory_marker(self, *, bucket: str, key: str) -> DirectoryMarkerHeadSummary:
        return self.head_directory_reference(bucket=bucket, key=key, resolve_mode="first-object")

    def head_directory_reference(
        self,
        *,
        bucket: str,
        key: str,
        resolve_mode: str = "first-object",
    ) -> DirectoryMarkerHeadSummary:
        marker_key = _normalize_marker_key(key)
        try:
            response = self._client.head_object(Bucket=bucket, Key=marker_key)
        except ClientError as error:
            payload = error.response.get("Error", {})
            code = payload.get("Code", "Unknown")
            message = payload.get("Message", str(error))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return self._head_object_under_prefix(
                    bucket=bucket,
                    requested_key=key,
                    marker_key=marker_key,
                    resolve_mode=resolve_mode,
                    marker_error_code=code,
                    marker_error_message=message,
                )
            raise

        return DirectoryMarkerHeadSummary(
            bucket=bucket,
            requested_key=key,
            marker_key=marker_key,
            exists=True,
            resolved_via="marker",
            last_modified=response.get("LastModified"),
            content_length=response.get("ContentLength"),
            etag=response.get("ETag"),
            storage_class=response.get("StorageClass"),
            metadata=response.get("Metadata") or {},
        )

    def _head_object_under_prefix(
        self,
        *,
        bucket: str,
        requested_key: str,
        marker_key: str,
        resolve_mode: str,
        marker_error_code: str,
        marker_error_message: str,
    ) -> DirectoryMarkerHeadSummary:
        if resolve_mode not in {"first-object", "last-object"}:
            raise ValueError("resolve_mode must be one of: first-object, last-object")

        contents = self._get_prefix_edge_objects(bucket=bucket, marker_key=marker_key, resolve_mode=resolve_mode)
        if not contents:
            return DirectoryMarkerHeadSummary(
                bucket=bucket,
                requested_key=requested_key,
                marker_key=marker_key,
                exists=False,
                resolved_via="not_found",
                error_code=marker_error_code,
                message=marker_error_message,
            )

        selected_object = contents[0]
        return DirectoryMarkerHeadSummary(
            bucket=bucket,
            requested_key=requested_key,
            marker_key=marker_key,
            exists=True,
            resolved_via=resolve_mode.replace("-", "_"),
            last_modified=selected_object.get("LastModified"),
            content_length=selected_object.get("Size"),
            etag=selected_object.get("ETag"),
            storage_class=selected_object.get("StorageClass"),
            sample_object_key=selected_object.get("Key"),
            metadata={},
            error_code=marker_error_code,
            message=f"Marker object not found; using {resolve_mode} under prefix ({marker_error_message})",
        )

    def _get_prefix_edge_objects(self, *, bucket: str, marker_key: str, resolve_mode: str) -> list[dict]:
        if resolve_mode == "first-object":
            response = self._client.list_objects_v2(Bucket=bucket, Prefix=marker_key, MaxKeys=1)
            return response.get("Contents") or []

        paginator = self._client.get_paginator("list_objects_v2")
        last_page_contents: list[dict] = []
        for page in paginator.paginate(
            Bucket=bucket,
            Prefix=marker_key,
            PaginationConfig={"PageSize": 1000},
        ):
            contents = page.get("Contents") or []
            if contents:
                last_page_contents = contents
        return last_page_contents[-1:] if last_page_contents else []

    def _hydrate_marker_last_modified(self, summary: ObjectCountSummary) -> None:
        if summary.prefix:
            prefix_marker = self.head_directory_marker(bucket=summary.bucket, key=summary.prefix)
            if prefix_marker.exists:
                summary.prefix_marker_last_modified = prefix_marker.last_modified

        for directory, directory_summary in summary.directories.items():
            marker = self.head_directory_marker(bucket=summary.bucket, key=directory)
            if marker.exists:
                directory_summary.marker_last_modified = marker.last_modified
                tree_node = summary.directory_tree_index.get(directory)
                if tree_node is not None:
                    tree_node.marker_last_modified = marker.last_modified


def _extract_directory_parts(*, key: str, prefix: str | None, depth: int) -> list[str]:
    normalized_prefix = prefix or ""
    relative_key = key[len(normalized_prefix):] if normalized_prefix and key.startswith(normalized_prefix) else key
    stripped_key = relative_key.lstrip("/")
    parts = [part for part in stripped_key.split("/") if part]
    if len(parts) <= 1:
        return []
    directory_parts = parts[:-1]
    if not directory_parts:
        return []
    return directory_parts[:depth]


def _normalize_marker_key(key: str) -> str:
    stripped = key.strip()
    if not stripped:
        raise ValueError("Directory marker key must not be empty")
    return stripped if stripped.endswith("/") else f"{stripped}/"


def _update_directory_tree(
    summary: ObjectCountSummary,
    directory_parts: list[str],
    size: int,
    last_modified: datetime | None,
) -> None:
    root = summary.directory_tree_root
    if root is None:
        return
    current = root
    current_path_parts: list[str] = []
    for part in directory_parts:
        current_path_parts.append(part)
        path = "/".join(current_path_parts) + "/"
        child = current.children.get(part)
        if child is None:
            child = DirectoryTreeNode(name=part, path=path)
            current.children[part] = child
            summary.directory_tree_index[path] = child
        child.objects_count += 1
        child.total_size_bytes += size
        child.earliest_object_last_modified = _pick_earliest(child.earliest_object_last_modified, last_modified)
        child.latest_object_last_modified = _pick_latest(child.latest_object_last_modified, last_modified)
        current = child


def _pick_earliest(current: datetime | None, candidate: datetime | None) -> datetime | None:
    if candidate is None:
        return current
    if current is None or candidate < current:
        return candidate
    return current


def _pick_latest(current: datetime | None, candidate: datetime | None) -> datetime | None:
    if candidate is None:
        return current
    if current is None or candidate > current:
        return candidate
    return current
