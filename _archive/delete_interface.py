from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import argparse
import sys

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from loguru import logger

from s3_uploader.models.s3 import S3StorageClient
from s3_uploader.plans import BucketDeletePlan, DeletePlan, load_delete_plan
from s3_uploader.report import OperationRecord, ReportRepository


@dataclass
class DeleteRunResult:
    session_path: Path
    operations_count: int


class DeleteService:
    def __init__(self, report_repository: ReportRepository) -> None:
        self.report_repository = report_repository

    def run(self, plan: DeletePlan) -> DeleteRunResult:
        connection = plan.connection.build_connection_config()
        client = S3StorageClient(connection)
        candidates = self._select_candidates(plan)
        operations: list[OperationRecord] = []

        for request, objects_to_delete in candidates:
            logger.info(
                f"Deleting {len(objects_to_delete)} object(s) from bucket '{request.bucket}'"
                + (f" with storage class '{request.storage_class}'" if request.storage_class else "")
            )
            for object_data in objects_to_delete:
                status_code = client.delete_object(request.bucket, object_data["key"])
                operations.append(
                    OperationRecord(
                        operation="delete",
                        bucket=request.bucket,
                        key=object_data["key"],
                        status="success",
                        storage_class=object_data["storage_class"],
                        size_bytes=int(object_data["size_bytes"]),
                        source_file=object_data.get("source_file"),
                        logical_key=object_data.get("logical_key"),
                        details={"remote_status_code": status_code},
                    )
                )

        session_path = self.report_repository.append_session(
            session_type="delete",
            operations=operations,
            metadata={"delete_oldest_first": plan.delete_oldest_first},
        )
        return DeleteRunResult(session_path=session_path, operations_count=len(operations))

    def _select_candidates(
        self,
        plan: DeletePlan,
    ) -> list[tuple[BucketDeletePlan, list[dict]]]:
        candidates: list[tuple[BucketDeletePlan, list[dict]]] = []
        for request in plan.bucket_plans:
            available_objects = self.report_repository.list_active_objects(
                bucket=request.bucket,
                storage_class=request.storage_class,
            )
            ordered_objects = (
                available_objects
                if plan.delete_oldest_first
                else list(reversed(available_objects))
            )
            if len(ordered_objects) < request.delete_count:
                storage_text = request.storage_class or "ANY"
                raise ValueError(
                    f"Not enough active objects for bucket '{request.bucket}' and storage '{storage_text}'. "
                    f"Requested {request.delete_count}, available {len(ordered_objects)}."
                )
            candidates.append((request, ordered_objects[: request.delete_count]))
        return candidates


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Delete objects recorded in the cumulative S3 test report.")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent.parent / "report" / "examples" / "delete_plan.example.json"),
        help="Path to delete JSON config.",
    )
    return parser


def main() -> None:
    load_dotenv()
    parser = build_argument_parser()
    args = parser.parse_args()
    plan = load_delete_plan(args.config)

    report_repository = ReportRepository(results_dir=plan.report_dir)
    service = DeleteService(report_repository=report_repository)
    result = service.run(plan)

    logger.success(f"Delete session stored in {result.session_path}")
    logger.info(f"Recorded operations: {result.operations_count}")


if __name__ == "__main__":
    main()
