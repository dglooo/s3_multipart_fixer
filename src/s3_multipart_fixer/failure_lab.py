from __future__ import annotations

import argparse
import os
from pathlib import Path
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote
from dataclasses import dataclass
from datetime import UTC, datetime
from secrets import token_hex

from botocore.client import BaseClient
from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError
from dotenv import load_dotenv
from loguru import logger
import requests
from requests_aws4auth import AWS4Auth

from .aws import AwsClientFactory, resolve_aws_runtime_config
from .logging_utils import configure_logging, log_aws_runtime_config, log_bucket_config


class SimulatedMultipartFailure(RuntimeError):
    """Raised when the lab intentionally stops an upload mid-flight."""

    def __init__(self, message: str, created_uploads: list["CreatedMultipartUpload"] | None = None) -> None:
        super().__init__(message)
        self.created_uploads = created_uploads or []


@dataclass(frozen=True, slots=True)
class CreatedMultipartUpload:
    bucket: str
    key: str
    upload_id: str


def _strip_namespace(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _find_xml_text(root: ET.Element, local_name: str) -> str | None:
    for element in root.iter():
        if _strip_namespace(element.tag) == local_name:
            return element.text
    return None


class MultipartBackend:
    def ensure_bucket(self, bucket: str) -> None:
        raise NotImplementedError

    def create_multipart_upload(self, bucket: str, key: str) -> str:
        raise NotImplementedError

    def upload_part(self, bucket: str, key: str, upload_id: str, part_number: int, chunk: bytes) -> str:
        raise NotImplementedError

    def complete_multipart_upload(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        parts: list[tuple[int, str]],
    ) -> None:
        raise NotImplementedError


class Boto3MultipartBackend(MultipartBackend):
    def __init__(self, client: BaseClient) -> None:
        self._client = client

    def ensure_bucket(self, bucket: str) -> None:
        logger.debug("calling HeadBucket via boto3: bucket={}", bucket)
        try:
            self._client.head_bucket(Bucket=bucket)
            logger.info("bucket exists: bucket={}", bucket)
            return
        except ClientError as error:
            error_code = error.response.get("Error", {}).get("Code", "")
            status_code = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if error_code not in {"404", "NoSuchBucket", "NotFound"} and status_code != 404:
                raise
            logger.warning(
                "bucket missing, creating bucket via boto3: bucket={}, error_code={}, status_code={}",
                bucket,
                error_code or "-",
                status_code,
            )
        self._create_bucket(bucket)

    def create_multipart_upload(self, bucket: str, key: str) -> str:
        logger.debug("calling CreateMultipartUpload via boto3: bucket={}, key={}", bucket, key)
        response = self._client.create_multipart_upload(Bucket=bucket, Key=key)
        return response["UploadId"]

    def upload_part(self, bucket: str, key: str, upload_id: str, part_number: int, chunk: bytes) -> str:
        response = self._client.upload_part(
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            PartNumber=part_number,
            Body=chunk,
        )
        etag = response.get("ETag")
        if not etag:
            raise RuntimeError("Boto3 multipart upload did not return ETag for uploaded part")
        return etag.strip('"')

    def complete_multipart_upload(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        parts: list[tuple[int, str]],
    ) -> None:
        self._client.complete_multipart_upload(
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": [{"PartNumber": part_number, "ETag": etag} for part_number, etag in parts]
            },
        )

    def _create_bucket(self, bucket: str) -> None:
        region_name = self._client.meta.region_name
        create_kwargs: dict[str, object] = {"Bucket": bucket}
        if region_name and region_name != "us-east-1":
            create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region_name}

        logger.debug(
            "calling CreateBucket via boto3: bucket={}, region_name={}, with_location_constraint={}",
            bucket,
            region_name,
            "CreateBucketConfiguration" in create_kwargs,
        )
        try:
            self._client.create_bucket(**create_kwargs)
        except ClientError as error:
            error_code = error.response.get("Error", {}).get("Code", "")
            if error_code in {"InvalidLocationConstraint", "MalformedXML", "IllegalLocationConstraintException"}:
                logger.warning(
                    "CreateBucket with location constraint failed, retrying without it: bucket={}, error_code={}",
                    bucket,
                    error_code,
                )
                self._client.create_bucket(Bucket=bucket)
            else:
                raise
        logger.info("bucket created: bucket={}", bucket)


class HttpMultipartBackend(MultipartBackend):
    def __init__(self, aws_config: AwsClientFactory) -> None:
        if not aws_config.access_key_id or not aws_config.secret_access_key:
            raise ValueError("HTTP multipart backend requires access key and secret key")

        self._aws_config = aws_config
        self._endpoint = aws_config.endpoint_url.rstrip("/") if aws_config.endpoint_url else ""
        self._request_timeout = (aws_config.connect_timeout, aws_config.read_timeout)
        self._upload_timeout = aws_config.http_upload_timeout
        self._session = requests.Session()
        self._auth = AWS4Auth(
            aws_config.access_key_id,
            aws_config.secret_access_key,
            aws_config.region or "us-east-1",
            "s3",
            session_token=aws_config.session_token,
        )

    def ensure_bucket(self, bucket: str) -> None:
        url = self._bucket_url(bucket)
        logger.debug("calling HeadBucket via http: url={}", url)
        response = self._session.head(url, auth=self._auth, timeout=self._request_timeout)
        if response.status_code in {200, 204, 301, 302, 403}:
            logger.info("bucket exists: bucket={}", bucket)
            return
        if response.status_code != 404:
            response.raise_for_status()

        logger.warning(
            "bucket missing, creating bucket via http: bucket={}, status_code={}",
            bucket,
            response.status_code,
        )
        self._create_bucket(bucket)

    def create_multipart_upload(self, bucket: str, key: str) -> str:
        url = f"{self._object_url(bucket, key)}?uploads"
        logger.debug("calling CreateMultipartUpload via http: url={}", url)
        response = self._session.post(url, auth=self._auth, headers={}, timeout=self._request_timeout)
        response.raise_for_status()
        upload_id = _find_xml_text(ET.fromstring(response.text), "UploadId")
        if not upload_id:
            raise RuntimeError("HTTP multipart upload did not return UploadId")
        return upload_id

    def upload_part(self, bucket: str, key: str, upload_id: str, part_number: int, chunk: bytes) -> str:
        url = self._object_url(bucket, key)
        params = {"partNumber": str(part_number), "uploadId": upload_id}
        headers = {"Content-Length": str(len(chunk))}
        logger.debug(
            "calling UploadPart via http: url={}, upload_timeout={}, chunk_size={}",
            url,
            self._upload_timeout,
            len(chunk),
        )
        response = self._session.put(
            url,
            auth=self._auth,
            params=params,
            data=chunk,
            headers=headers,
            timeout=self._upload_timeout,
        )
        response.raise_for_status()
        etag = response.headers.get("ETag")
        if not etag:
            raise RuntimeError("HTTP multipart upload did not return ETag for uploaded part")
        return etag.strip('"')

    def complete_multipart_upload(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        parts: list[tuple[int, str]],
    ) -> None:
        url = f"{self._object_url(bucket, key)}?uploadId={upload_id}"
        root = ET.Element("CompleteMultipartUpload")
        for part_number, etag in parts:
            part = ET.SubElement(root, "Part")
            ET.SubElement(part, "PartNumber").text = str(part_number)
            ET.SubElement(part, "ETag").text = f'"{etag}"'
        body = ET.tostring(root, encoding="utf-8")
        headers = {
            "Content-Type": "application/xml",
            "Content-Length": str(len(body)),
        }
        logger.debug("calling CompleteMultipartUpload via http: url={}, parts_count={}", url, len(parts))
        response = self._session.post(
            url,
            auth=self._auth,
            data=body,
            headers=headers,
            timeout=self._request_timeout,
        )
        response.raise_for_status()

    def _create_bucket(self, bucket: str) -> None:
        url = self._bucket_url(bucket)
        body = self._build_region_constraint()
        headers: dict[str, str] = {}
        if body:
            headers["Content-Type"] = "application/xml"

        logger.debug(
            "calling CreateBucket via http: url={}, region={}, with_location_constraint={}",
            url,
            self._aws_config.region,
            bool(body),
        )
        response = self._session.put(
            url,
            auth=self._auth,
            data=body,
            headers=headers,
            timeout=self._request_timeout,
        )
        if response.status_code in {200, 204, 409}:
            logger.info("bucket created: bucket={}", bucket)
            return
        if response.status_code == 400 and "InvalidLocationConstraint" in response.text:
            logger.warning(
                "CreateBucket with location constraint failed, retrying without it: bucket={}",
                bucket,
            )
            retry = self._session.put(url, auth=self._auth, data=b"", headers={}, timeout=self._request_timeout)
            if retry.status_code in {200, 204, 409}:
                logger.info("bucket created: bucket={}", bucket)
                return
            retry.raise_for_status()
        response.raise_for_status()

    def _build_region_constraint(self) -> bytes:
        if not self._aws_config.region or self._aws_config.region == "us-east-1":
            return b""
        region_constraint = (
            f"<CreateBucketConfiguration xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">"
            f"<LocationConstraint>{self._aws_config.region}</LocationConstraint>"
            f"</CreateBucketConfiguration>"
        )
        return region_constraint.encode("utf-8")

    def _bucket_url(self, bucket: str) -> str:
        return f"{self._endpoint}/{bucket}"

    def _object_url(self, bucket: str, key: str) -> str:
        return f"{self._endpoint}/{bucket}/{quote(key, safe='/')}"


class MultipartFailureLab:
    def __init__(self, backend: MultipartBackend) -> None:
        self._backend = backend

    def create_incomplete_uploads(
        self,
        *,
        bucket: str,
        key_prefix: str,
        source_path: Path,
        part_size: int,
        uploaded_parts: int,
        count: int,
        complete_uploads: bool,
    ) -> list[CreatedMultipartUpload]:
        if count < 1:
            raise ValueError("count must be >= 1")
        if uploaded_parts < 1:
            raise ValueError("uploaded_parts must be >= 1")
        if part_size < 5 * 1024 * 1024:
            raise ValueError("S3 multipart part size must be at least 5 MiB")

        file_size = source_path.stat().st_size
        logger.info(
            "starting multipart failure simulation: bucket={}, key_prefix={}, file={}, file_size={}, part_size={}, uploaded_parts={}, count={}",
            bucket,
            key_prefix,
            str(source_path),
            file_size,
            part_size,
            uploaded_parts,
            count,
        )
        self._backend.ensure_bucket(bucket)
        created_uploads: list[CreatedMultipartUpload] = []

        for index in range(1, count + 1):
            key = _build_failure_key(key_prefix, index, count)
            upload_id = self._backend.create_multipart_upload(bucket, key)
            logger.info(
                "created multipart upload: bucket={}, key={}, upload_id={}, upload_index={}, upload_count={}",
                bucket,
                key,
                upload_id,
                index,
                count,
            )

            uploaded_etags: list[tuple[int, str]] = []
            with source_path.open("rb") as source:
                for part_number in range(1, uploaded_parts + 1):
                    chunk = source.read(part_size)
                    if not chunk:
                        raise ValueError(
                            f"source file ended before part {part_number}; increase file size or lower uploaded_parts"
                        )
                    logger.info(
                        "starting upload part: bucket={}, key={}, upload_id={}, part_number={}, chunk_size={}, upload_index={}, upload_count={}",
                        bucket,
                        key,
                        upload_id,
                        part_number,
                        len(chunk),
                        index,
                        count,
                    )
                    started_at = time.perf_counter()
                    etag = self._backend.upload_part(bucket, key, upload_id, part_number, chunk)
                    uploaded_etags.append((part_number, etag))
                    logger.info(
                        "uploaded part: bucket={}, key={}, upload_id={}, part_number={}, chunk_size={}, elapsed_seconds={:.3f}, upload_index={}, upload_count={}",
                        bucket,
                        key,
                        upload_id,
                        part_number,
                        len(chunk),
                        time.perf_counter() - started_at,
                        index,
                        count,
                    )

            if complete_uploads:
                logger.info(
                    "completing multipart upload: bucket={}, key={}, upload_id={}, parts_count={}, upload_index={}, upload_count={}",
                    bucket,
                    key,
                    upload_id,
                    len(uploaded_etags),
                    index,
                    count,
                )
                self._backend.complete_multipart_upload(bucket, key, upload_id, uploaded_etags)
                logger.info(
                    "completed multipart upload: bucket={}, key={}, upload_id={}, upload_index={}, upload_count={}",
                    bucket,
                    key,
                    upload_id,
                    index,
                    count,
                )
            else:
                created_uploads.append(CreatedMultipartUpload(bucket=bucket, key=key, upload_id=upload_id))

        return created_uploads


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create an incomplete multipart upload for testing.")
    parser.add_argument("--env-file", default=".env", help="Path to dotenv file. Default: .env")
    parser.add_argument("--bucket")
    parser.add_argument("--key")
    parser.add_argument("--file", required=True, dest="source_file")
    parser.add_argument("--part-size", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--uploaded-parts", type=int, default=2)
    parser.add_argument("--count", type=int, default=1, help="How many incomplete multipart uploads to create.")
    parser.add_argument("--profile")
    parser.add_argument("--region")
    parser.add_argument("--endpoint-url")
    parser.add_argument("--access-key-id")
    parser.add_argument("--secret-access-key")
    parser.add_argument("--session-token")
    parser.add_argument("--connect-timeout", type=int)
    parser.add_argument("--read-timeout", type=int)
    parser.add_argument("--total-max-attempts", type=int)
    parser.add_argument("--http-upload-timeout", type=int)
    parser.add_argument("--signature-version")
    parser.add_argument("--addressing-style", choices=("auto", "virtual", "path"))
    parser.add_argument("--payload-signing-enabled", dest="payload_signing_enabled", action="store_true")
    parser.add_argument("--no-payload-signing-enabled", dest="payload_signing_enabled", action="store_false")
    parser.set_defaults(payload_signing_enabled=None)
    parser.add_argument("--multipart-backend", choices=("boto3", "http"))
    parser.add_argument(
        "--complete",
        action="store_true",
        help="Complete multipart uploads after all parts are uploaded instead of leaving them incomplete.",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    load_dotenv(args.env_file, override=True)
    configure_logging(verbose=args.verbose)

    aws_config = resolve_aws_runtime_config(
        profile=args.profile,
        region=args.region,
        endpoint_url=args.endpoint_url,
        access_key_id=args.access_key_id,
        secret_access_key=args.secret_access_key,
        session_token=args.session_token,
        connect_timeout=args.connect_timeout,
        read_timeout=args.read_timeout,
        total_max_attempts=args.total_max_attempts,
        default_total_max_attempts=1,
        signature_version=args.signature_version,
        addressing_style=args.addressing_style,
        payload_signing_enabled=args.payload_signing_enabled,
        http_upload_timeout=args.http_upload_timeout,
    )
    log_aws_runtime_config(aws_config)
    bucket, bucket_source = _resolve_bucket(args.bucket)
    log_bucket_config(bucket, bucket_source)
    key_prefix = args.key or _generate_dynamic_key_prefix()
    logger.info("key config: key_prefix={}, source={}", key_prefix, "cli" if args.key else "generated")
    backend_name = args.multipart_backend or _first_env("S3_MULTIPART_BACKEND") or "boto3"
    logger.info("multipart backend: {}", backend_name)
    backend = _build_backend(backend_name, aws_config)
    lab = MultipartFailureLab(backend)

    try:
        created_uploads = lab.create_incomplete_uploads(
            bucket=bucket,
            key_prefix=key_prefix,
            source_path=Path(args.source_file),
            part_size=args.part_size,
            uploaded_parts=args.uploaded_parts,
            count=args.count,
            complete_uploads=args.complete,
        )
        if args.complete:
            print(_build_completed_uploads_message(bucket=bucket, key_prefix=key_prefix, count=args.count))
            return 0
        message = _build_created_uploads_message(created_uploads)
        raise SimulatedMultipartFailure(message, created_uploads=created_uploads)
    except SimulatedMultipartFailure as error:
        print(str(error))
        return 0
    except ClientError as error:
        logger.exception("AWS request failed during multipart failure simulation")
        payload = error.response.get("Error", {})
        code = payload.get("Code", "Unknown")
        message = payload.get("Message", str(error))
        print(f"{code}: {message}")
        return 2
    except requests.RequestException as error:
        logger.exception("HTTP request failed during multipart failure simulation")
        print(str(error))
        return 2
    except (ReadTimeoutError, ConnectTimeoutError, EndpointConnectionError) as error:
        logger.exception("Network timeout or endpoint connection failure during multipart failure simulation")
        print(str(error))
        return 2

    return 1


def _resolve_bucket(cli_bucket: str | None) -> tuple[str, str]:
    if cli_bucket:
        return cli_bucket, "cli"
    env_bucket = _first_env("S3_BUCKET", "BUCKET")
    if env_bucket:
        return env_bucket, "env"
    raise ValueError("Bucket is required. Set S3_BUCKET/BUCKET in .env or pass --bucket.")


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def _build_backend(name: str, aws_config: AwsClientFactory) -> MultipartBackend:
    normalized = name.strip().lower()
    if normalized == "boto3":
        return Boto3MultipartBackend(aws_config.build_s3_client())
    if normalized == "http":
        return HttpMultipartBackend(aws_config)
    raise ValueError(f"Unsupported multipart backend: {name}")


def _build_failure_key(key_prefix: str, index: int, count: int) -> str:
    if count == 1:
        return key_prefix
    width = max(3, len(str(count)))
    path = Path(key_prefix)
    suffix = "".join(path.suffixes)
    stem = path.name[: -len(suffix)] if suffix else path.name
    generated_name = f"{stem}.failed-{index:0{width}d}{suffix}"
    parent = path.parent.as_posix()
    if parent in {"", "."}:
        return generated_name
    return f"{parent}/{generated_name}"


def _build_created_uploads_message(created_uploads: list[CreatedMultipartUpload]) -> str:
    if not created_uploads:
        return "no multipart uploads were created"
    lines = [f"multipart uploads intentionally left incomplete: count={len(created_uploads)}"]
    for item in created_uploads:
        lines.append(f"- bucket={item.bucket} key={item.key} upload_id={item.upload_id}")
    return "\n".join(lines)


def _build_completed_uploads_message(*, bucket: str, key_prefix: str, count: int) -> str:
    return (
        "multipart uploads completed successfully: "
        f"bucket={bucket} key_prefix={key_prefix} count={count}"
    )


def _generate_dynamic_key_prefix() -> str:
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"failure-lab/{timestamp}-{token_hex(4)}.bin"
