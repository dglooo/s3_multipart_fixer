# s3_multipart_fixer

Utilities for two tasks:

1. Scan and filter in-progress S3 multipart uploads.
2. Abort selected multipart uploads with optional verification.
3. Create a deliberately incomplete multipart upload for testing.

## Environment

The tool automatically loads `.env` from the project directory. You can also pass another file with `--env-file` before the command name. Keep real credentials in `.env`; it is ignored by Git.

Supported variables:

```bash
S3_ENDPOINT=https://s3.example.internal
S3_BUCKET=final
AWS_REGION=eu-central-1
AWS_ACCESS_KEY_ID=replace-me
AWS_SECRET_ACCESS_KEY=replace-me
S3_CONNECT_TIMEOUT=5
S3_READ_TIMEOUT=60
S3_HTTP_UPLOAD_TIMEOUT=60
S3_TOTAL_MAX_ATTEMPTS=1
S3_SIGNATURE_VERSION=s3v4
S3_ADDRESSING_STYLE=path
S3_PAYLOAD_SIGNING_ENABLED=true
S3_MULTIPART_BACKEND=boto3
S3_ASYNC_CONCURRENCY=10
AWS_SESSION_TOKEN=replace-me-if-needed
```

Custom aliases also work:

```bash
BUCKET=final
S3_REGION=eu-central-1
S3_ACCESS_KEY_ID=replace-me
S3_SECRET_ACCESS_KEY=replace-me
S3_SESSION_TOKEN=replace-me-if-needed
```

`--bucket` overrides `S3_BUCKET` or `BUCKET` from `.env`.
Timeouts and retries can be overridden via `.env` or CLI flags `--connect-timeout`, `--read-timeout`, `--http-upload-timeout`, and `--total-max-attempts`.
S3 compatibility behavior can also be tuned via `S3_SIGNATURE_VERSION`, `S3_ADDRESSING_STYLE`, and `S3_PAYLOAD_SIGNING_ENABLED`.
The multipart lab supports two backends: `boto3` and `http`. Set `S3_MULTIPART_BACKEND=http` or pass `--multipart-backend http` to use the raw HTTP flow derived from `_archive`.
For `scan` and `abort`, the default execution mode is `sync`. Async mode is enabled only with `--io-mode async`. Parallelism can be tuned via `S3_ASYNC_CONCURRENCY` or `--concurrency`.
Async mode requires `aioboto3` and `aiobotocore`, which are included in `requirements.txt`.

## Install

```bash
cd s3_multipart_fixer
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

`pip install -e .` is optional. You can run the project directly with `python multipart_fixer.py` and `python multipart_failure_lab.py`.

## Count objects

```bash
python multipart_fixer.py count-objects \
  --prefix optional/prefix/
```

The command writes `report/objects.json` by default and logs a summary with `objects_count` and `total_size_bytes`. To feed `collect-prefix-dates` or the helper scripts with a new tree, use `--output report/objects_.json`.

## Scan uploads

```bash
python multipart_fixer.py scan \
  --older-than 30d \
  --progress-every 10000 \
  --output report/scan.jsonl \
  --format jsonl
```

The command logs progress while scanning, including the total number of multipart uploads already inspected.
Each scan record also contains:
- `completed_parts`: list of successfully uploaded parts already present in this multipart upload
- `completed_parts_count`
- `completed_parts_total_bytes`
- `completed_parts_error` if `ListParts` failed for that upload

Async scan example:

```bash
python multipart_fixer.py scan \
  --io-mode async \
  --concurrency 20 \
  --older-than 30d \
  --output report/scan.jsonl
```

## Dry-run abort

```bash
python multipart_fixer.py abort \
  --prefix broken/ \
  --older-than 30d \
  --output report/abort-dry-run.csv \
  --format csv
```

Dry-run from previously saved scan results:

```bash
python multipart_fixer.py abort \
  --input-json report/scan.jsonl \
  --output report/abort-dry-run.jsonl
```

Async dry-run example:

```bash
python multipart_fixer.py abort \
  --io-mode async \
  --concurrency 20 \
  --older-than 30d \
  --output report/abort-dry-run.jsonl
```

## Execute abort

```bash
python multipart_fixer.py abort \
  --key broken/example.bin \
  --upload-id abcdef123456789 \
  --execute \
  --output report/abort-result.jsonl \
  --format jsonl
```

Execute abort from previously saved scan results:

```bash
python multipart_fixer.py abort \
  --input-json report/scan.jsonl \
  --execute \
  --output report/abort-result.jsonl
```

Relative age filter examples:

```bash
python multipart_fixer.py scan --older-than 30d --output report/scan.jsonl
python multipart_fixer.py abort --older-than 12h --output report/abort-dry-run.jsonl
python multipart_fixer.py abort --since 30m --execute --output report/abort-result.jsonl
```

Supported suffixes: `s`, `m`, `h`, `d`, `w`.

## Create an incomplete multipart upload

The command uploads several parts and intentionally stops before `CompleteMultipartUpload`.
If the target bucket does not exist, the lab tries to create it first.

```bash
python multipart_failure_lab.py \
  --multipart-backend http \
  --file ./large.bin \
  --part-size 8388608 \
  --uploaded-parts 2 \
  --count 10
```

If `--key` is omitted, the lab auto-generates a dynamic key prefix like `failure-lab/20260520T101500Z-ab12cd34.bin`.
The command prints all generated `upload_id` values. When `--count` is greater than `1`, the lab auto-generates keys like `failure-lab/20260520T101500Z-ab12cd34.failed-001.bin`, `failure-lab/20260520T101500Z-ab12cd34.failed-002.bin`, and so on. These uploads should then appear in `multipart-fixer scan`.

To run a full successful multipart upload instead of leaving it incomplete:

```bash
python multipart_failure_lab.py \
  --multipart-backend http \
  --file ./large.bin \
  --part-size 8388608 \
  --uploaded-parts 2 \
  --count 1 \
  --complete
```

## Reports and date-based cleanup

Existing JSON/JSONL reports are in `report/`, which Git ignores. New reports default to that directory. A bare `--output` filename is placed in `report/`; an explicit path is honored. `scan` defaults to `report/scan.jsonl`, abort dry-run to `report/abort-dry-run.jsonl`, and executed abort to `report/abort-result.jsonl`. Use `--output -` for stdout.

To leave uploads initiated on or after May 1, 2026 00:00 UTC untouched, scan and preview the older ones, then execute the same selection:

```bash
python multipart_fixer.py scan --bucket my-bucket --initiated-before 2026-05-01T00:00:00Z --output report/scan-before-may.jsonl
python multipart_fixer.py abort --bucket my-bucket --input-json report/scan-before-may.jsonl --initiated-before 2026-05-01T00:00:00Z --output report/abort-before-may-dry-run.jsonl
python multipart_fixer.py abort --bucket my-bucket --input-json report/scan-before-may.jsonl --initiated-before 2026-05-01T00:00:00Z --execute --output report/abort-before-may-result.jsonl
```

The second command is a dry-run. Inspect its report before running the third command. `--bucket` also filters records loaded from JSONL. For a rolling cutoff use `--older-than 30d` instead of `--initiated-before`.
