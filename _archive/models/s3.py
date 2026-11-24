from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
import math
import os
import xml.etree.ElementTree as ET

import requests
from loguru import logger
from requests_aws4auth import AWS4Auth
from s3_uploader.common import DEFAULT_STORAGE_CLASS, normalize_storage_class, utc_now_iso


DEFAULT_REQUEST_TIMEOUT = 30
DEFAULT_UPLOAD_TIMEOUT = 3600


def _strip_namespace(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _find_xml_text(root: ET.Element, local_name: str) -> Optional[str]:
    for element in root.iter():
        if _strip_namespace(element.tag) == local_name:
            return element.text
    return None


@dataclass(frozen=True)
class S3ConnectionConfig:
    access_key: str
    secret_key: str
    region: str
    endpoint: str

    def __post_init__(self) -> None:
        if not self.access_key:
            raise ValueError("S3 access key is required")
        if not self.secret_key:
            raise ValueError("S3 secret key is required")
        if not self.region:
            raise ValueError("S3 region is required")
        if not self.endpoint:
            raise ValueError("S3 endpoint is required")

        object.__setattr__(self, "endpoint", self.endpoint.rstrip("/"))

    @classmethod
    def from_env(
        cls,
        region: str,
        endpoint: str,
        access_key_env: str = "AWS_ACCESS_KEY_ID",
        secret_key_env: str = "AWS_SECRET_ACCESS_KEY",
    ) -> "S3ConnectionConfig":
        access_key = os.getenv(access_key_env)
        secret_key = os.getenv(secret_key_env)
        if not access_key:
            raise KeyError(access_key_env)
        if not secret_key:
            raise KeyError(secret_key_env)
        return cls(
            access_key=access_key,
            secret_key=secret_key,
            region=region,
            endpoint=endpoint,
        )


@dataclass(frozen=True)
class StoredObject:
    bucket: str
    key: str
    size_bytes: int
    storage_class: str


@dataclass(frozen=True)
class UploadBatchResult:
    requested_key: str
    uploaded_objects: list[StoredObject] = field(default_factory=list)


class UploadExecutionError(RuntimeError):
    def __init__(
        self,
        message: str,
        requested_key: str,
        uploaded_objects: Optional[list[StoredObject]] = None,
    ) -> None:
        super().__init__(message)
        self.requested_key = requested_key
        self.uploaded_objects = uploaded_objects or []


class S3StorageClient:
    def __init__(self, config: S3ConnectionConfig, session: Optional[requests.Session] = None) -> None:
        self.config = config
        self.auth = AWS4Auth(config.access_key, config.secret_key, config.region, "s3")
        self.session = session or requests.Session()

    def create_bucket(self, bucket: str) -> None:
        url = f"{self.config.endpoint}/{bucket}"
        body = self._build_region_constraint()
        headers: dict[str, str] = {}
        if body:
            headers["Content-Type"] = "application/xml"

        response = self.session.put(
            url,
            auth=self.auth,
            data=body,
            headers=headers,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        if response.status_code in (200, 204):
            logger.success(f"Bucket '{bucket}' created.")
            return
        if response.status_code == 409:
            logger.warning(f"Bucket '{bucket}' already exists.")
            return
        if response.status_code == 400 and "InvalidLocationConstraint" in response.text:
            retry = self.session.put(
                url,
                auth=self.auth,
                data=b"",
                headers={},
                timeout=DEFAULT_REQUEST_TIMEOUT,
            )
            if retry.status_code in (200, 204, 409):
                logger.info(f"Bucket '{bucket}' created without explicit region constraint.")
                return
            retry.raise_for_status()
        response.raise_for_status()

    def put_object(
        self,
        bucket: str,
        key: str,
        data: bytes,
        storage_class: Optional[str] = None,
        timeout: int = DEFAULT_UPLOAD_TIMEOUT,
    ) -> StoredObject:
        url = f"{self.config.endpoint}/{bucket}/{key}"
        headers = {"Content-Length": str(len(data))}
        normalized_storage = normalize_storage_class(storage_class)
        if normalized_storage != DEFAULT_STORAGE_CLASS:
            headers["x-amz-storage-class"] = normalized_storage

        response = self.session.put(
            url,
            auth=self.auth,
            data=data,
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        return StoredObject(
            bucket=bucket,
            key=key,
            size_bytes=len(data),
            storage_class=normalized_storage,
        )

    def initiate_multipart_upload(
        self,
        bucket: str,
        key: str,
        storage_class: Optional[str] = None,
    ) -> str:
        url = f"{self.config.endpoint}/{bucket}/{key}?uploads"
        headers: dict[str, str] = {}
        normalized_storage = normalize_storage_class(storage_class)
        if normalized_storage != DEFAULT_STORAGE_CLASS:
            headers["x-amz-storage-class"] = normalized_storage

        response = self.session.post(
            url,
            auth=self.auth,
            headers=headers,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        tree = ET.fromstring(response.text)
        upload_id = _find_xml_text(tree, "UploadId")
        if not upload_id:
            raise RuntimeError("S3 multipart upload did not return UploadId")
        return upload_id

    def upload_part(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        part_number: int,
        data: bytes,
    ) -> str:
        url = f"{self.config.endpoint}/{bucket}/{key}"
        params = {"partNumber": str(part_number), "uploadId": upload_id}
        headers = {"Content-Length": str(len(data))}

        response = self.session.put(
            url,
            auth=self.auth,
            params=params,
            data=data,
            headers=headers,
            timeout=DEFAULT_UPLOAD_TIMEOUT,
        )
        response.raise_for_status()

        etag = response.headers.get("ETag")
        if not etag:
            raise RuntimeError(f"S3 part upload for '{bucket}/{key}' returned response without ETag")
        return etag.strip('"')

    def complete_multipart_upload(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        parts_etags: list[tuple[int, str]],
    ) -> StoredObject:
        url = f"{self.config.endpoint}/{bucket}/{key}?uploadId={upload_id}"
        root = ET.Element("CompleteMultipartUpload")
        for part_number, etag in parts_etags:
            part = ET.SubElement(root, "Part")
            ET.SubElement(part, "PartNumber").text = str(part_number)
            ET.SubElement(part, "ETag").text = f'"{etag}"'
        body = ET.tostring(root, encoding="utf-8")
        headers = {
            "Content-Type": "application/xml",
            "Content-Length": str(len(body)),
        }

        response = self.session.post(
            url,
            auth=self.auth,
            data=body,
            headers=headers,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        if _find_xml_text(ET.fromstring(response.text), "Error"):
            raise RuntimeError(f"S3 multipart completion returned error for '{bucket}/{key}'")

        return StoredObject(
            bucket=bucket,
            key=key,
            size_bytes=0,
            storage_class=DEFAULT_STORAGE_CLASS,
        )

    def delete_object(self, bucket: str, key: str) -> int:
        url = f"{self.config.endpoint}/{bucket}/{key}"
        response = self.session.delete(
            url,
            auth=self.auth,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        if response.status_code not in (200, 204, 404):
            response.raise_for_status()
        return response.status_code

    def _build_region_constraint(self) -> bytes:
        if self.config.region == "us-east-1":
            return b""
        region_constraint = (
            f"<CreateBucketConfiguration xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">"
            f"<LocationConstraint>{self.config.region}</LocationConstraint>"
            f"</CreateBucketConfiguration>"
        )
        return region_constraint.encode("utf-8")


class S3Uploader(ABC):
    def __init__(
        self,
        access_key: str,
        secret_key: str,
        region: str,
        endpoint: str,
        bucket: str,
        key: str,
        file_path: str,
        chunk_size_mb: int,
        storage_class: str = DEFAULT_STORAGE_CLASS,
        client: Optional[S3StorageClient] = None,
    ) -> None:
        self.connection = S3ConnectionConfig(
            access_key=access_key,
            secret_key=secret_key,
            region=region,
            endpoint=endpoint,
        )
        self.bucket = bucket
        self.key = key
        self.file_path = Path(file_path)
        self.chunk_size_mb = chunk_size_mb
        self.storage_class = normalize_storage_class(storage_class)
        self.client = client or S3StorageClient(self.connection)

        if not self.bucket:
            raise ValueError("Bucket name is required")
        if not self.key:
            raise ValueError("Object key is required")
        if not self.file_path.exists():
            raise FileNotFoundError(f"Source file not found: {self.file_path}")
        if not self.file_path.is_file():
            raise ValueError(f"Source path is not a file: {self.file_path}")

    @property
    def chunk_size_bytes(self) -> int:
        return self.chunk_size_mb * 1024 * 1024

    def create_bucket(self) -> None:
        self.client.create_bucket(self.bucket)

    @abstractmethod
    def upload(self) -> UploadBatchResult:
        raise NotImplementedError


class HttpUploader(S3Uploader):
    def upload(self) -> UploadBatchResult:
        file_size = self.file_path.stat().st_size
        uploaded_objects: list[StoredObject] = []
        logger.debug(f"Uploading '{self.file_path.name}' to bucket '{self.bucket}' via HTTP PUT")

        try:
            if self.chunk_size_bytes <= 0 or file_size <= self.chunk_size_bytes:
                data = self.file_path.read_bytes()
                uploaded_object = self.client.put_object(
                    bucket=self.bucket,
                    key=self.key,
                    data=data,
                    storage_class=self.storage_class,
                )
                uploaded_objects.append(uploaded_object)
                return UploadBatchResult(requested_key=self.key, uploaded_objects=uploaded_objects)

            total_parts = math.ceil(file_size / self.chunk_size_bytes)
            logger.debug(
                f"File size: {file_size} bytes. Upload will be split into {total_parts} HTTP objects."
            )

            with self.file_path.open("rb") as source_file:
                for part_index in range(1, total_parts + 1):
                    chunk = source_file.read(self.chunk_size_bytes)
                    if not chunk:
                        break
                    part_key = f"{self.key}.part{part_index}"
                    uploaded_object = self.client.put_object(
                        bucket=self.bucket,
                        key=part_key,
                        data=chunk,
                        storage_class=self.storage_class,
                    )
                    uploaded_objects.append(uploaded_object)

            return UploadBatchResult(requested_key=self.key, uploaded_objects=uploaded_objects)
        except Exception as error:
            raise UploadExecutionError(
                message=str(error),
                requested_key=self.key,
                uploaded_objects=uploaded_objects,
            ) from error


class MultipartUploader(S3Uploader):
    def upload(self) -> UploadBatchResult:
        if self.chunk_size_bytes <= 0:
            raise ValueError("Multipart upload requires chunk_size_mb > 0")

        file_size = self.file_path.stat().st_size
        total_parts = math.ceil(file_size / self.chunk_size_bytes)
        parts_etags: list[tuple[int, str]] = []
        logger.debug(
            f"Uploading '{self.file_path.name}' to bucket '{self.bucket}' via multipart upload in {total_parts} parts."
        )

        try:
            upload_id = self.client.initiate_multipart_upload(
                bucket=self.bucket,
                key=self.key,
                storage_class=self.storage_class,
            )
            with self.file_path.open("rb") as source_file:
                for part_number in range(1, total_parts + 1):
                    chunk = source_file.read(self.chunk_size_bytes)
                    if not chunk:
                        break
                    etag = self.client.upload_part(
                        bucket=self.bucket,
                        key=self.key,
                        upload_id=upload_id,
                        part_number=part_number,
                        data=chunk,
                    )
                    parts_etags.append((part_number, etag))

            completed_object = self.client.complete_multipart_upload(
                bucket=self.bucket,
                key=self.key,
                upload_id=upload_id,
                parts_etags=parts_etags,
            )
            final_object = StoredObject(
                bucket=completed_object.bucket,
                key=completed_object.key,
                size_bytes=file_size,
                storage_class=self.storage_class,
            )
            return UploadBatchResult(requested_key=self.key, uploaded_objects=[final_object])
        except Exception as error:
            raise UploadExecutionError(
                message=str(error),
                requested_key=self.key,
                uploaded_objects=[],
            ) from error


def build_uploader(
    mode: str,
    connection: S3ConnectionConfig,
    bucket: str,
    key: str,
    file_path: str,
    chunk_size_mb: int,
    storage_class: str = DEFAULT_STORAGE_CLASS,
    client: Optional[S3StorageClient] = None,
) -> S3Uploader:
    normalized_mode = mode.strip().lower()
    uploader_classes: dict[str, type[S3Uploader]] = {
        "http": HttpUploader,
        "multipart": MultipartUploader,
    }
    if normalized_mode not in uploader_classes:
        raise ValueError(f"Unsupported upload mode: {mode}")

    uploader_class = uploader_classes[normalized_mode]
    return uploader_class(
        access_key=connection.access_key,
        secret_key=connection.secret_key,
        region=connection.region,
        endpoint=connection.endpoint,
        bucket=bucket,
        key=key,
        file_path=file_path,
        chunk_size_mb=chunk_size_mb,
        storage_class=storage_class,
        client=client,
    )
