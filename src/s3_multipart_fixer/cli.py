from __future__ import annotations

import asyncio
import argparse
import json
import os
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError
from loguru import logger
from dotenv import load_dotenv

from .aborter import MultipartUploadAborter
from .async_aborter import AsyncMultipartUploadAborter
from .async_scanner import AsyncMultipartUploadScanner
from .aws import resolve_aws_runtime_config
from .bucket_admin import BucketAdmin
from .directory_markers import DirectoryMarkerInspector, build_marker_report, load_marker_paths_from_json
from .failure_lab import Boto3MultipartBackend, MultipartFailureLab, SimulatedMultipartFailure
from .health_check import BucketHealthChecker
from .input_loaders import load_multipart_upload_records
from .logging_utils import configure_logging, log_aws_runtime_config, log_bucket_config
from .models import MultipartUploadRecord, MultipartUploadSelector, ScanSummary
from .object_counter import BucketObjectCounter, ObjectCountSummary, ObjectVersionCountSummary
from .prefix_dates import AsyncPrefixDateCollector, load_leaf_prefix_targets
from .reporting import build_report_writer, report_destination
from .scanner import MultipartUploadScanner
from .timeutils import parse_datetime, parse_duration, utc_now


SCAN_FIELDS = (
    "bucket",
    "key",
    "upload_id",
    "initiated",
    "initiator_id",
    "initiator_display_name",
    "owner_id",
    "owner_display_name",
    "storage_class",
    "completed_parts_count",
    "completed_parts_total_bytes",
    "completed_parts_error",
    "completed_parts",
)

ABORT_FIELDS = (
    "bucket",
    "key",
    "upload_id",
    "initiated",
    "requested",
    "success",
    "verified",
    "attempts",
    "message",
    "error_code",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect and clean incomplete S3 multipart uploads.")
    parser.add_argument("--env-file", default=".env", help="Path to dotenv file. Default: .env")
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
    parser.add_argument("--verbose", action="store_true")

    subparsers = parser.add_subparsers(dest="command", required=True)

    count_parser = subparsers.add_parser("count-objects", help="Count objects in a bucket.")
    count_parser.add_argument("--verbose", action="store_true")
    count_parser.add_argument("--bucket")
    count_parser.add_argument("--prefix")
    count_parser.add_argument("--page-size", type=int, default=1000)
    count_parser.add_argument("--progress-every", type=int, default=10000)
    count_parser.add_argument("--directory-depth", type=int, default=1, help="Aggregate usage by logical directory depth. Use 0 to disable directory breakdown.")
    count_parser.add_argument("--limit", type=int, help="Stop after counting the specified number of objects.")
    count_parser.add_argument("--output", default="report/objects.json", help="Write JSON report to file. Default: report/objects.json.")

    count_versions_parser = subparsers.add_parser("count-object-versions", help="Count object versions and delete markers in a bucket.")
    count_versions_parser.add_argument("--verbose", action="store_true")
    count_versions_parser.add_argument("--bucket")
    count_versions_parser.add_argument("--prefix")
    count_versions_parser.add_argument("--page-size", type=int, default=1000)
    count_versions_parser.add_argument("--progress-every", type=int, default=10000)
    count_versions_parser.add_argument("--output", default="report/object_versions.json")

    head_marker_parser = subparsers.add_parser("head-directory-marker", help="Resolve a path via directory marker object, or fallback to the first/last object under the prefix, and return LastModified.")
    head_marker_parser.add_argument("--verbose", action="store_true")
    head_marker_parser.add_argument("--bucket")
    head_marker_parser.add_argument("--key", required=True, help="Directory prefix to probe, for example: some/path or some/path/")
    head_marker_parser.add_argument("--resolve-mode", choices=("first-object", "last-object"), default="first-object")
    head_marker_parser.add_argument("--output", default="report/marker_report.json", help="Write JSON report to file. Default: report/marker_report.json.")

    head_markers_parser = subparsers.add_parser("head-directory-markers", help="Inspect directory marker objects either from a JSON list of paths or by discovering subdirectories under a prefix.")
    head_markers_parser.add_argument("--verbose", action="store_true")
    head_markers_parser.add_argument("--bucket")
    head_markers_parser.add_argument("--input-json", help="JSON file with an array of paths or objects containing a path field.")
    head_markers_parser.add_argument("--prefix", help="Root prefix to discover subdirectories under.")
    head_markers_parser.add_argument("--page-size", type=int, default=1000)
    head_markers_parser.add_argument("--progress-every", type=int, default=1000)
    head_markers_parser.add_argument("--output", default="report/markers_report.json", help="Write JSON report to file. Default: report/markers_report.json.")

    create_bucket_parser = subparsers.add_parser("create-bucket", help="Create a bucket if it does not exist.")
    create_bucket_parser.add_argument("--verbose", action="store_true")
    create_bucket_parser.add_argument("--bucket")

    health_parser = subparsers.add_parser("bucket-health-check", help="Inspect objects, versions, delete markers and multipart state in a bucket.")
    health_parser.add_argument("--verbose", action="store_true")
    health_parser.add_argument("--bucket")
    health_parser.add_argument("--prefix")
    health_parser.add_argument("--page-size", type=int, default=1000)
    health_parser.add_argument("--progress-every", type=int, default=1000)
    health_parser.add_argument("--output", default="report/bucket_health_check.json")

    prefix_dates_parser = subparsers.add_parser("collect-prefix-dates", help="Read leaf prefixes from report/objects_.json and collect earliest/latest LastModified for each prefix asynchronously.")
    prefix_dates_parser.add_argument("--verbose", action="store_true")
    prefix_dates_parser.add_argument("--bucket")
    prefix_dates_parser.add_argument("--input-json", default="report/objects_.json")
    prefix_dates_parser.add_argument("--output", default="report/prefix_dates.json")
    prefix_dates_parser.add_argument("--output-jsonl", help="Write each processed prefix result immediately to this JSONL file. Default: derived from --output.")
    prefix_dates_parser.add_argument("--anchor-name", default="Data")
    prefix_dates_parser.add_argument("--concurrency", type=int, default=20)
    prefix_dates_parser.add_argument("--page-size", type=int, default=1000)
    prefix_dates_parser.add_argument("--progress-every", type=int, default=100)
    prefix_dates_parser.add_argument("--limit", type=int)

    scan_parser = subparsers.add_parser("scan", help="List and filter multipart uploads.")
    _add_common_scan_arguments(scan_parser)
    scan_parser.add_argument("--output", help="Output destination. Default: report/scan.jsonl or report/scan.csv; '-' for stdout.")
    scan_parser.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")

    abort_parser = subparsers.add_parser("abort", help="Abort matched multipart uploads.")
    _add_common_scan_arguments(abort_parser)
    abort_parser.add_argument(
        "--input-json",
        help="Use previously saved scan results from JSONL or JSON instead of listing multipart uploads from S3.",
    )
    abort_parser.add_argument("--output", help="Output destination. Default: report/abort-dry-run.jsonl or report/abort-result.jsonl; '-' for stdout.")
    abort_parser.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")
    abort_parser.add_argument("--execute", action="store_true", help="Actually send abort requests.")
    abort_parser.add_argument("--skip-verify", action="store_true")
    abort_parser.add_argument("--max-attempts", type=int, default=3)
    abort_parser.add_argument("--retry-delay-seconds", type=float, default=1.0)

    simulate_parser = subparsers.add_parser(
        "simulate-failure",
        help="Upload several multipart parts and intentionally stop before completion.",
    )
    simulate_parser.add_argument("--verbose", action="store_true")
    simulate_parser.add_argument("--bucket", required=True)
    simulate_parser.add_argument("--key", required=True)
    simulate_parser.add_argument("--file", required=True, dest="source_file")
    simulate_parser.add_argument("--part-size", type=int, default=8 * 1024 * 1024)
    simulate_parser.add_argument("--uploaded-parts", type=int, default=2)

    return parser


def _add_common_scan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--bucket")
    parser.add_argument("--prefix")
    parser.add_argument("--io-mode", choices=("sync", "async"), default="sync")
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--initiated-after", help="ISO-8601 timestamp, example: 2026-05-20T00:00:00Z")
    parser.add_argument("--initiated-before", help="ISO-8601 timestamp, example: 2026-05-21T00:00:00Z")
    parser.add_argument(
        "--older-than",
        "--since",
        dest="older_than",
        help="Relative age filter, for example: 30m, 12h, 7d, 1w. Matches uploads older than the given age.",
    )
    parser.add_argument("--key")
    parser.add_argument("--upload-id")
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--progress-every", type=int, default=1000, help="Log progress every N scanned uploads.")
    parser.add_argument("--limit", type=int)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command in {"scan", "abort"} and args.output is None:
        report_name = "scan" if args.command == "scan" else ("abort-result" if args.execute else "abort-dry-run")
        args.output = f"report/{report_name}.{args.format}"
    if hasattr(args, "output"):
        args.output = report_destination(args.output)
    if getattr(args, "output_jsonl", None):
        args.output_jsonl = report_destination(args.output_jsonl)
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
        default_total_max_attempts=10,
        signature_version=args.signature_version,
        addressing_style=args.addressing_style,
        payload_signing_enabled=args.payload_signing_enabled,
        http_upload_timeout=args.http_upload_timeout,
    )
    log_aws_runtime_config(aws_config)
    default_io_mode = "async" if args.command == "collect-prefix-dates" else "sync"
    args.io_mode = getattr(args, "io_mode", None) or default_io_mode
    args.concurrency = getattr(args, "concurrency", None) or _first_env_int("S3_ASYNC_CONCURRENCY", default=10)
    if args.concurrency < 1:
        raise ValueError("--concurrency must be greater than 0")
    _log_execution_mode(args.command, args.io_mode, args.concurrency)
    bucket, bucket_source = _resolve_bucket(args.bucket)
    log_bucket_config(bucket, bucket_source)
    args.bucket = bucket

    try:
        if args.command in {"scan", "abort"} and args.io_mode == "async":
            return asyncio.run(_run_async_command(args, aws_config))
        if args.command == "collect-prefix-dates":
            return asyncio.run(_run_collect_prefix_dates(args, aws_config))

        client = aws_config.build_s3_client()
        if args.command == "count-objects":
            return run_count_objects(args, client)
        if args.command == "count-object-versions":
            return run_count_object_versions(args, client)
        if args.command == "head-directory-marker":
            return run_head_directory_marker(args, client)
        if args.command == "head-directory-markers":
            return run_head_directory_markers(args, client)
        if args.command == "create-bucket":
            return run_create_bucket(args, client)
        if args.command == "bucket-health-check":
            return run_bucket_health_check(args, client)
        if args.command == "scan":
            return run_scan(args, client)
        if args.command == "abort":
            return run_abort(args, client)
        if args.command == "simulate-failure":
            return run_simulate_failure(args, client)
    except ClientError as error:
        logger.exception("AWS request failed")
        print(_extract_error_message(error))
        return 2
    except ValueError as error:
        print(str(error))
        return 2

    parser.error(f"Unknown command: {args.command}")
    return 2


def run_scan(args: argparse.Namespace, client: Any) -> int:
    selector = _build_selector(args)
    summary = ScanSummary()
    scanner = MultipartUploadScanner(client)
    writer = build_report_writer(args.format, args.output, SCAN_FIELDS)
    try:
        for record in scanner.filter_uploads(
            bucket=args.bucket,
            selector=selector,
            prefix=args.prefix,
            page_size=args.page_size,
            limit=args.limit,
            summary=summary,
            progress_every=args.progress_every,
            progress_callback=lambda current: _log_progress("scan", current),
        ):
            writer.write_row(record.to_dict())
    finally:
        writer.close()

    _log_summary("scan", summary)
    return 0


def run_count_objects(args: argparse.Namespace, client: Any) -> int:
    if args.directory_depth < 0:
        raise ValueError("--directory-depth must be greater than or equal to 0")
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit must be greater than 0")
    counter = BucketObjectCounter(client)
    summary = counter.count_objects(
        bucket=args.bucket,
        prefix=args.prefix,
        page_size=args.page_size,
        directory_depth=args.directory_depth,
        limit=args.limit,
        progress_every=args.progress_every,
        progress_callback=_log_object_count_progress,
    )
    logger.info(
        "count-objects summary: bucket={}, prefix={}, objects_count={}, total_size_bytes={}, total_size_gb={}, directories_count={}, limit={}",
        summary.bucket,
        summary.prefix,
        summary.objects_count,
        summary.total_size_bytes,
        summary.total_size_gb,
        len(summary.directories),
        args.limit,
    )
    _emit_pretty_json(summary.to_dict(), args.output)
    return 0


def run_count_object_versions(args: argparse.Namespace, client: Any) -> int:
    counter = BucketObjectCounter(client)
    summary = counter.count_object_versions(
        bucket=args.bucket,
        prefix=args.prefix,
        page_size=args.page_size,
        progress_every=args.progress_every,
        progress_callback=_log_object_versions_progress,
    )
    logger.info(
        "count-object-versions summary: bucket={}, prefix={}, current_versions_count={}, noncurrent_versions_count={}, delete_markers_count={}, total_size_bytes={}, total_size_gb={}",
        summary.bucket,
        summary.prefix,
        summary.current_versions_count,
        summary.noncurrent_versions_count,
        summary.delete_markers_count,
        summary.total_size_bytes,
        summary.total_size_gb,
    )
    _emit_pretty_json(summary.to_dict(), args.output)
    return 0


def run_head_directory_marker(args: argparse.Namespace, client: Any) -> int:
    counter = BucketObjectCounter(client)
    summary = counter.head_directory_reference(bucket=args.bucket, key=args.key, resolve_mode=args.resolve_mode)
    logger.info(
        "head-directory-marker summary: bucket={}, requested_key={}, marker_key={}, resolve_mode={}, exists={}, resolved_via={}, last_modified={}, sample_object_key={}",
        summary.bucket,
        summary.requested_key,
        summary.marker_key,
        args.resolve_mode,
        summary.exists,
        summary.resolved_via,
        summary.last_modified.isoformat() if summary.last_modified else None,
        summary.sample_object_key,
    )
    _emit_pretty_json(summary.to_dict(), args.output)
    return 0


def run_head_directory_markers(args: argparse.Namespace, client: Any) -> int:
    if bool(args.input_json) == bool(args.prefix):
        raise ValueError("Use exactly one of --input-json or --prefix")

    inspector = DirectoryMarkerInspector(client)
    if args.input_json:
        paths = load_marker_paths_from_json(args.input_json)
        markers = inspector.inspect_paths(
            bucket=args.bucket,
            paths=paths,
            progress_every=args.progress_every,
            progress_callback=lambda processed: logger.info(
                "head-directory-markers progress: processed_paths={}",
                processed,
            ),
        )
        payload = build_marker_report(
            bucket=args.bucket,
            mode="input-json",
            source=args.input_json,
            markers=markers,
        )
    else:
        discovered_paths = inspector.discover_subdirectories(
            bucket=args.bucket,
            prefix=args.prefix,
            page_size=args.page_size,
            include_root=True,
        )
        markers = inspector.inspect_paths(
            bucket=args.bucket,
            paths=discovered_paths,
            progress_every=args.progress_every,
            progress_callback=lambda processed: logger.info(
                "head-directory-markers progress: processed_paths={}, discovered_paths={}",
                processed,
                len(discovered_paths),
            ),
        )
        payload = build_marker_report(
            bucket=args.bucket,
            mode="discover-prefix",
            source=args.prefix,
            markers=markers,
            discovered_paths_count=len(discovered_paths),
        )

    logger.info(
        "head-directory-markers summary: bucket={}, mode={}, processed_paths_count={}, existing_markers_count={}, missing_markers_count={}",
        payload["bucket"],
        payload["mode"],
        payload["processed_paths_count"],
        payload["existing_markers_count"],
        payload["missing_markers_count"],
    )
    _emit_pretty_json(payload, args.output)
    return 0


def run_create_bucket(args: argparse.Namespace, client: Any) -> int:
    admin = BucketAdmin(client)
    result = admin.create_bucket(args.bucket)
    print(json.dumps(result, indent=4, ensure_ascii=False))
    return 0


def run_bucket_health_check(args: argparse.Namespace, client: Any) -> int:
    checker = BucketHealthChecker(client)
    summary = checker.check(
        bucket=args.bucket,
        prefix=args.prefix,
        page_size=args.page_size,
        progress_every=args.progress_every,
        object_progress_callback=_log_object_count_progress,
        versions_progress_callback=_log_object_versions_progress,
        multipart_progress_callback=lambda current: _log_progress("bucket-health-multipart", current),
    )
    logger.info(
        "bucket-health-check summary: bucket={}, prefix={}, objects_count={}, versions_total_entries_count={}, multipart_uploads_count={}, multipart_uploaded_parts_total_bytes={}, namespace_empty={}",
        summary.bucket,
        summary.prefix,
        summary.objects.objects_count,
        summary.versions.total_entries_count,
        summary.multipart_uploads_count,
        summary.multipart_uploaded_parts_total_bytes,
        summary.namespace_empty,
    )
    _emit_pretty_json(summary.to_dict(), args.output)
    return 0


def run_abort(args: argparse.Namespace, client: Any) -> int:
    selector = _build_selector(args)
    summary = ScanSummary()
    aborter = MultipartUploadAborter(client)
    writer = build_report_writer(args.format, args.output, ABORT_FIELDS)
    exit_code = 0
    processed_results = 0
    try:
        if args.input_json:
            records = _filter_input_records(
                records=load_multipart_upload_records(args.input_json),
                bucket=args.bucket,
                selector=selector,
                limit=args.limit,
                summary=summary,
                progress_every=args.progress_every,
            )
        else:
            scanner = MultipartUploadScanner(client)
            records = scanner.filter_uploads(
                bucket=args.bucket,
                selector=selector,
                prefix=args.prefix,
                page_size=args.page_size,
                limit=args.limit,
                summary=summary,
                progress_every=args.progress_every,
                progress_callback=lambda current: _log_progress("abort-scan", current),
            )

        for record in records:
            result = aborter.abort_upload(
                record,
                execute=args.execute,
                verify=not args.skip_verify,
                max_attempts=args.max_attempts,
                retry_delay_seconds=args.retry_delay_seconds,
            )
            writer.write_row(result.to_dict())
            processed_results += 1
            if result.requested:
                summary.aborted += 1
            if result.verified:
                summary.verified += 1
            if result.requested and not result.success:
                summary.failed += 1
                exit_code = 1
            _maybe_log_abort_progress(
                command="abort",
                processed_results=processed_results,
                summary=summary,
                progress_every=args.progress_every,
            )
    finally:
        writer.close()

    _log_summary("abort", summary, execute=args.execute)
    return exit_code


def run_simulate_failure(args: argparse.Namespace, client: Any) -> int:
    lab = MultipartFailureLab(Boto3MultipartBackend(client))
    try:
        created_uploads = lab.create_incomplete_uploads(
            bucket=args.bucket,
            key_prefix=args.key,
            source_path=Path(args.source_file),
            part_size=args.part_size,
            uploaded_parts=args.uploaded_parts,
            count=1,
            complete_uploads=False,
        )
        raise SimulatedMultipartFailure(_build_created_uploads_message(created_uploads))
    except SimulatedMultipartFailure as error:
        print(str(error))
        return 0
    return 1


def _build_selector(args: argparse.Namespace) -> MultipartUploadSelector:
    initiated_after = parse_datetime(args.initiated_after)
    initiated_before = parse_datetime(args.initiated_before)
    older_than = parse_duration(args.older_than)

    if older_than and initiated_before:
        raise ValueError("--older-than/--since cannot be used together with --initiated-before")
    if older_than:
        initiated_before = utc_now() - older_than

    if initiated_after and initiated_before and initiated_after > initiated_before:
        raise ValueError("--initiated-after must be earlier than or equal to --initiated-before")

    return MultipartUploadSelector(
        initiated_after=initiated_after,
        initiated_before=initiated_before,
        key_prefix=args.prefix,
        key_equals=args.key,
        upload_id_equals=args.upload_id,
    )


def _filter_input_records(
    *,
    records: list[MultipartUploadRecord],
    bucket: str,
    selector: MultipartUploadSelector,
    limit: int | None,
    summary: ScanSummary,
    progress_every: int | None,
):
    emitted = 0
    for record in records:
        summary.scanned += 1
        if progress_every and summary.scanned % progress_every == 0:
            _log_progress("abort-input", summary)
        if record.bucket != bucket or not selector.matches(record):
            continue
        summary.matched += 1
        summary.emitted += 1
        yield record
        emitted += 1
        if limit is not None and emitted >= limit:
            return


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


def _first_env_int(name: str, default: int) -> int:
    value = _first_env(name)
    if value is None:
        return default
    return int(value)


def _log_summary(command: str, summary: ScanSummary, **extra: Any) -> None:
    logger.info(
        "{} summary: scanned={}, matched={}, emitted={}, aborted={}, verified={}, failed={}, extra={}",
        command,
        summary.scanned,
        summary.matched,
        summary.emitted,
        summary.aborted,
        summary.verified,
        summary.failed,
        extra,
    )


def _log_progress(command: str, summary: ScanSummary) -> None:
    logger.info(
        "{} progress: scanned_total={}, matched={}, emitted={}",
        command,
        summary.scanned,
        summary.matched,
        summary.emitted,
    )


def _maybe_log_abort_progress(
    *,
    command: str,
    processed_results: int,
    summary: ScanSummary,
    progress_every: int | None,
) -> None:
    if not progress_every or processed_results % progress_every != 0:
        return
    logger.info(
        "{} progress: processed_results={}, scanned_total={}, matched={}, emitted={}, abort_requested={}, verified={}, failed={}",
        command,
        processed_results,
        summary.scanned,
        summary.matched,
        summary.emitted,
        summary.aborted,
        summary.verified,
        summary.failed,
    )


def _log_object_count_progress(summary: ObjectCountSummary) -> None:
    logger.info(
        "count-objects progress: bucket={}, prefix={}, objects_count={}, total_size_bytes={}",
        summary.bucket,
        summary.prefix,
        summary.objects_count,
        summary.total_size_bytes,
    )


def _log_object_versions_progress(summary: ObjectVersionCountSummary) -> None:
    logger.info(
        "count-object-versions progress: bucket={}, prefix={}, current_versions_count={}, noncurrent_versions_count={}, delete_markers_count={}, total_size_bytes={}",
        summary.bucket,
        summary.prefix,
        summary.current_versions_count,
        summary.noncurrent_versions_count,
        summary.delete_markers_count,
        summary.total_size_bytes,
    )


def _log_execution_mode(command: str, io_mode: str, concurrency: int) -> None:
    if command in {"scan", "abort", "collect-prefix-dates"}:
        logger.info("execution mode: command={}, io_mode={}, concurrency={}", command, io_mode, concurrency)
        return
    logger.info("execution mode: command={}, io_mode=sync", command)


def _extract_error_message(error: ClientError) -> str:
    payload = error.response.get("Error", {})
    code = payload.get("Code", "Unknown")
    message = payload.get("Message", str(error))
    return f"{code}: {message}"


def _emit_pretty_json(payload: dict[str, Any], destination: str) -> None:
    rendered = json.dumps(payload, indent=4, ensure_ascii=False)
    if destination == "-":
        print(rendered)
        return
    output_path = Path(report_destination(destination))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered + "\n", encoding="utf-8")
    logger.info("JSON report saved: {}", output_path)


def _derive_jsonl_output_path(summary_output: str) -> str:
    if summary_output == "-":
        return "report/prefix_dates.jsonl"
    output_path = Path(summary_output)
    if output_path.suffix.lower() == ".json":
        return str(output_path.with_suffix(".jsonl"))
    return f"{summary_output}.jsonl"


def _format_progress_bar(processed: int, total: int, width: int = 20) -> str:
    if total <= 0:
        return "[{}]".format("." * width)
    ratio = min(max(processed / total, 0.0), 1.0)
    filled = int(ratio * width)
    return "[{}{} {:>5.1f}%]".format("#" * filled, "." * (width - filled), ratio * 100.0)


def _log_collect_prefix_dates_timeouts(client, aws_config) -> None:
    config = getattr(getattr(client, "meta", None), "config", None)
    connect_timeout = getattr(config, "connect_timeout", aws_config.connect_timeout)
    read_timeout = getattr(config, "read_timeout", aws_config.read_timeout)
    retries = getattr(config, "retries", {}) or {}
    total_max_attempts = retries.get("total_max_attempts", aws_config.total_max_attempts)
    logger.opt(colors=True).info(
        "collect-prefix-dates effective timeouts: <green>connect_timeout={}s, read_timeout={}s, total_max_attempts={}</green>",
        connect_timeout,
        read_timeout,
        total_max_attempts,
    )


async def _run_async_command(args: argparse.Namespace, aws_config) -> int:
    if args.command == "scan":
        return await _run_scan_async(args, aws_config)
    if args.command == "abort":
        return await _run_abort_async(args, aws_config)
    raise ValueError(f"Async mode is unsupported for command: {args.command}")


async def _run_collect_prefix_dates(args: argparse.Namespace, aws_config) -> int:
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit must be greater than 0")

    targets = load_leaf_prefix_targets(args.input_json, anchor_name=args.anchor_name)
    if args.limit is not None:
        targets = targets[: args.limit]
    output_jsonl = args.output_jsonl or _derive_jsonl_output_path(args.output)
    writer = build_report_writer("jsonl", output_jsonl, (), flush_every=1)

    logger.info(
        "collect-prefix-dates start: bucket={}, anchor_name={}, input_json={}, targets_count={}, concurrency={}, page_size={}, output_jsonl={}",
        args.bucket,
        args.anchor_name,
        args.input_json,
        len(targets),
        args.concurrency,
        args.page_size,
        output_jsonl,
    )

    try:
        async with aws_config.build_async_s3_client() as client:
            _log_collect_prefix_dates_timeouts(client, aws_config)
            collector = AsyncPrefixDateCollector(client)
            summary = await collector.collect(
                bucket=args.bucket,
                targets=targets,
                concurrency=args.concurrency,
                page_size=args.page_size,
                progress_every=args.progress_every,
                progress_callback=lambda processed, total, succeeded, failed: logger.info(
                    "collect-prefix-dates progress: {} processed={}/{} remaining={} succeeded={} failed={}",
                    _format_progress_bar(processed, total),
                    processed,
                    total,
                    max(total - processed, 0),
                    succeeded,
                    failed,
                ),
                result_callback=lambda result: writer.write_row(result.to_dict()),
            )
    finally:
        writer.close()

    payload = {
        "bucket": args.bucket,
        "input_json": args.input_json,
        "summary_output_file": args.output,
        "output_jsonl": output_jsonl,
        "anchor_name": args.anchor_name,
        "processed_prefixes_count": summary.processed_prefixes_count,
        "succeeded_prefixes_count": summary.succeeded_prefixes_count,
        "failed_prefixes_count": summary.failed_prefixes_count,
    }
    logger.info(
        "collect-prefix-dates summary: bucket={}, anchor_name={}, processed_prefixes_count={}, succeeded_prefixes_count={}, failed_prefixes_count={}, output_jsonl={}",
        args.bucket,
        args.anchor_name,
        summary.processed_prefixes_count,
        summary.succeeded_prefixes_count,
        summary.failed_prefixes_count,
        output_jsonl,
    )
    _emit_pretty_json(payload, args.output)
    return 0


async def _run_scan_async(args: argparse.Namespace, aws_config) -> int:
    selector = _build_selector(args)
    summary = ScanSummary()
    writer = build_report_writer(args.format, args.output, SCAN_FIELDS)
    try:
        async with aws_config.build_async_s3_client() as client:
            scanner = AsyncMultipartUploadScanner(client)
            async for record in scanner.filter_uploads(
                bucket=args.bucket,
                selector=selector,
                prefix=args.prefix,
                page_size=args.page_size,
                limit=args.limit,
                summary=summary,
                progress_every=args.progress_every,
                progress_callback=lambda current: _log_progress("scan-async", current),
                concurrency=args.concurrency,
            ):
                writer.write_row(record.to_dict())
    finally:
        writer.close()

    _log_summary("scan-async", summary, concurrency=args.concurrency)
    return 0


async def _run_abort_async(args: argparse.Namespace, aws_config) -> int:
    selector = _build_selector(args)
    summary = ScanSummary()
    writer = build_report_writer(args.format, args.output, ABORT_FIELDS)
    exit_code = 0
    processed_results = 0
    try:
        async with aws_config.build_async_s3_client() as client:
            aborter = AsyncMultipartUploadAborter(client)
            if args.input_json:
                records = _filter_input_records(
                    records=load_multipart_upload_records(args.input_json),
                    bucket=args.bucket,
                    selector=selector,
                    limit=args.limit,
                    summary=summary,
                    progress_every=args.progress_every,
                )
                async for result in _abort_records_async(
                    records=records,
                    aborter=aborter,
                    execute=args.execute,
                    verify=not args.skip_verify,
                    max_attempts=args.max_attempts,
                    retry_delay_seconds=args.retry_delay_seconds,
                    concurrency=args.concurrency,
                ):
                    writer.write_row(result.to_dict())
                    processed_results += 1
                    if result.requested:
                        summary.aborted += 1
                    if result.verified:
                        summary.verified += 1
                    if result.requested and not result.success:
                        summary.failed += 1
                        exit_code = 1
                    _maybe_log_abort_progress(
                        command="abort-async",
                        processed_results=processed_results,
                        summary=summary,
                        progress_every=args.progress_every,
                    )
            else:
                scanner = AsyncMultipartUploadScanner(client)
                async for result in _abort_records_async(
                    records=scanner.filter_uploads(
                        bucket=args.bucket,
                        selector=selector,
                        prefix=args.prefix,
                        page_size=args.page_size,
                        limit=args.limit,
                        summary=summary,
                        progress_every=args.progress_every,
                        progress_callback=lambda current: _log_progress("abort-scan-async", current),
                        concurrency=args.concurrency,
                    ),
                    aborter=aborter,
                    execute=args.execute,
                    verify=not args.skip_verify,
                    max_attempts=args.max_attempts,
                    retry_delay_seconds=args.retry_delay_seconds,
                    concurrency=args.concurrency,
                ):
                    writer.write_row(result.to_dict())
                    processed_results += 1
                    if result.requested:
                        summary.aborted += 1
                    if result.verified:
                        summary.verified += 1
                    if result.requested and not result.success:
                        summary.failed += 1
                        exit_code = 1
                    _maybe_log_abort_progress(
                        command="abort-async",
                        processed_results=processed_results,
                        summary=summary,
                        progress_every=args.progress_every,
                    )
    finally:
        writer.close()

    _log_summary("abort-async", summary, execute=args.execute, concurrency=args.concurrency)
    return exit_code


async def _abort_records_async(
    *,
    records,
    aborter: AsyncMultipartUploadAborter,
    execute: bool,
    verify: bool,
    max_attempts: int,
    retry_delay_seconds: float,
    concurrency: int,
):
    pending: set[asyncio.Task] = set()

    if hasattr(records, "__aiter__"):
        async for record in records:
            pending.add(
                asyncio.create_task(
                    aborter.abort_upload(
                        record,
                        execute=execute,
                        verify=verify,
                        max_attempts=max_attempts,
                        retry_delay_seconds=retry_delay_seconds,
                    )
                )
            )
            if len(pending) >= concurrency:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    yield task.result()
    else:
        for record in records:
            pending.add(
                asyncio.create_task(
                    aborter.abort_upload(
                        record,
                        execute=execute,
                        verify=verify,
                        max_attempts=max_attempts,
                        retry_delay_seconds=retry_delay_seconds,
                    )
                )
            )
            if len(pending) >= concurrency:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    yield task.result()

    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            yield task.result()


def _build_created_uploads_message(created_uploads) -> str:
    if not created_uploads:
        return "no multipart uploads were created"
    lines = [f"multipart uploads intentionally left incomplete: count={len(created_uploads)}"]
    for item in created_uploads:
        lines.append(f"- bucket={item.bucket} key={item.key} upload_id={item.upload_id}")
    return "\n".join(lines)
