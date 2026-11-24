from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone
from itertools import count
import argparse
import sys

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from loguru import logger

from s3_uploader.models.s3 import S3StorageClient, UploadExecutionError, build_uploader
from s3_uploader.plans import BucketUploadPlan, UploadPlan, load_upload_plan
from s3_uploader.report import OperationRecord, ReportRepository


class ObjectKeyFactory:
    def __init__(self) -> None:
        self._sequence = count(1)

    def build_key(self, bucket: str, source_file: Path, key_prefix: Optional[str] = None) -> str:
        prefix = key_prefix or source_file.stem
        suffix = source_file.suffix
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        sequence = next(self._sequence)
        sanitized_bucket = bucket.replace("/", "-")
        return f"{prefix}-{sanitized_bucket}-{timestamp}-{sequence:04d}{suffix}"


@dataclass
class UploadRunResult:
    session_path: Path
    operations_count: int


class UploadService:
    def __init__(self, report_repository: ReportRepository) -> None:
        self.report_repository = report_repository
        self.key_factory = ObjectKeyFactory()

    def run(self, plan: UploadPlan) -> UploadRunResult:
        connection = plan.connection.build_connection_config()
        client = S3StorageClient(connection)
        operations: list[OperationRecord] = []

        for bucket_plan in plan.bucket_plans:
            if plan.create_bucket:
                logger.info(f"Ensuring bucket exists: {bucket_plan.bucket}")
                client.create_bucket(bucket_plan.bucket)

            for upload_index in range(1, bucket_plan.upload_count + 1):
                key = self.key_factory.build_key(
                    bucket=bucket_plan.bucket,
                    source_file=plan.file_path,
                    key_prefix=bucket_plan.key_prefix or plan.key_prefix,
                )
                logger.info(
                    f"Uploading '{plan.file_path.name}' to bucket '{bucket_plan.bucket}' "
                    f"({upload_index}/{bucket_plan.upload_count}) as '{key}'"
                )
                operations.extend(
                    self._run_single_upload(
                        plan=plan,
                        bucket_plan=bucket_plan,
                        key=key,
                        client=client,
                    )
                )

        session_path = self.report_repository.append_session(
            session_type="upload",
            operations=operations,
            metadata={
                "mode": plan.mode,
                "file_path": str(plan.file_path),
                "chunk_size_mb": plan.chunk_size_mb,
            },
        )
        return UploadRunResult(session_path=session_path, operations_count=len(operations))

    def _run_single_upload(
        self,
        plan: UploadPlan,
        bucket_plan: BucketUploadPlan,
        key: str,
        client: S3StorageClient,
    ) -> list[OperationRecord]:
        uploader = build_uploader(
            mode=plan.mode,
            connection=client.config,
            bucket=bucket_plan.bucket,
            key=key,
            file_path=str(plan.file_path),
            chunk_size_mb=plan.chunk_size_mb,
            storage_class=bucket_plan.storage_class,
            client=client,
        )

        try:
            result = uploader.upload()
            return [
                OperationRecord(
                    operation="upload",
                    bucket=item.bucket,
                    key=item.key,
                    status="success",
                    storage_class=item.storage_class,
                    size_bytes=item.size_bytes,
                    upload_mode=plan.mode,
                    source_file=str(plan.file_path),
                    logical_key=result.requested_key,
                )
                for item in result.uploaded_objects
            ]
        except UploadExecutionError as error:
            partial_successes = [
                OperationRecord(
                    operation="upload",
                    bucket=item.bucket,
                    key=item.key,
                    status="success",
                    storage_class=item.storage_class,
                    size_bytes=item.size_bytes,
                    upload_mode=plan.mode,
                    source_file=str(plan.file_path),
                    logical_key=error.requested_key,
                )
                for item in error.uploaded_objects
            ]
            partial_successes.append(
                OperationRecord(
                    operation="upload",
                    bucket=bucket_plan.bucket,
                    key=error.requested_key,
                    status="failed",
                    storage_class=bucket_plan.storage_class,
                    size_bytes=0,
                    upload_mode=plan.mode,
                    source_file=str(plan.file_path),
                    logical_key=error.requested_key,
                    details={"error": str(error)},
                )
            )
            return partial_successes


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Upload test objects to S3 and persist cumulative JSON reports.")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent / "upload_plan.example.json"),
        help="Path to upload JSON config.",
    )
    return parser


def main() -> None:
    load_dotenv()
    parser = build_argument_parser()
    args = parser.parse_args()
    plan = load_upload_plan(args.config)

    if not plan.file_path.exists():
        raise FileNotFoundError(f"Upload source file not found: {plan.file_path}")

    report_repository = ReportRepository(results_dir=plan.report_dir)
    service = UploadService(report_repository=report_repository)
    result = service.run(plan)

    logger.success(f"Upload session stored in {result.session_path}")
    logger.info(f"Recorded operations: {result.operations_count}")


if __name__ == "__main__":
    main()
