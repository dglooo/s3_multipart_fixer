import os
import sys
import json
import asyncio
import random
import hashlib
from typing import Optional
from pathlib import Path

from curl_cffi.requests import AsyncSession
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
try:
    from botocore.auth import S3SigV4Auth as SigV4Auth
except ImportError:
    from botocore.auth import SigV4Auth
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

AWS_REGION = 'eu'
S3_ENDPOINT = 'https://s3-updates-test.slcloud.ru'
#S3_ENDPOINT = 'https://s3test.slcloud.ru'
#S3_ENDPOINT = 'https://s3-01.slcloud.ru'

FILE_SIZES_MB = [10, 20, 30, 50, 200]
MAX_WORKERS = 3
VERIFY_TLS = True

def build_file_map() -> dict:
    file_map = {}
    missing = []
    bad_size = []
    for size_mb in FILE_SIZES_MB:
        path = BASE_DIR / f"{size_mb}M.bin"
        if not path.exists():
            missing.append(str(path))
            continue
        expected = size_mb * 1024 * 1024
        actual = path.stat().st_size
        if actual != expected:
            bad_size.append((str(path), expected, actual))
            continue
        file_map[size_mb] = path

    if missing:
        logger.error(f"Missing files: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
    if bad_size:
        details = ", ".join([f"{p} (expected {e}, got {a})" for p, e, a in bad_size])
        logger.error(f"File size mismatch: {details}", file=sys.stderr)
        sys.exit(1)

    return file_map

def build_credentials() -> Credentials:
    access_key = os.getenv('AWS_ACCESS_KEY_ID')
    secret_key = os.getenv('AWS_SECRET_ACCESS_KEY')
    token = os.getenv('AWS_SESSION_TOKEN')
    if not access_key:
        raise KeyError('AWS_ACCESS_KEY_ID')
    if not secret_key:
        raise KeyError('AWS_SECRET_ACCESS_KEY')
    return Credentials(access_key, secret_key, token)

def bucket_names() -> list:
    return [f"upl-tests-{i}" for i in range(1, 101)]

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def read_file_and_hash(path: Path) -> tuple:
    h = hashlib.sha256()
    data = bytearray()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(chunk)
            data.extend(chunk)
    return bytes(data), h.hexdigest()

def signed_headers(
    creds: Credentials,
    method: str,
    url: str,
    headers: dict,
    payload_hash: str,
    body: Optional[bytes] = None
) -> dict:
    headers = dict(headers)
    headers['x-amz-content-sha256'] = payload_hash
    req = AWSRequest(method=method, url=url, data=body, headers=headers)
    req.context['payload_hash'] = payload_hash
    SigV4Auth(creds, 's3', AWS_REGION).add_auth(req)
    return dict(req.headers)

async def create_bucket(session: AsyncSession, creds: Credentials, bucket: str) -> None:
    url = f"{S3_ENDPOINT}/{bucket}"
    body = b""
    if AWS_REGION and AWS_REGION != "us-east-1":
        body = (
            f"<CreateBucketConfiguration xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">"
            f"<LocationConstraint>{AWS_REGION}</LocationConstraint>"
            f"</CreateBucketConfiguration>"
        ).encode()

    headers = {
        "Content-Length": str(len(body)),
    }
    if body:
        headers["Content-Type"] = "application/xml"

    headers = signed_headers(creds, "PUT", url, headers, sha256_bytes(body), body=body)
    resp = await session.put(url, data=body, headers=headers)
    if resp.status_code in (200, 204, 409):
        return

    if resp.status_code == 400:
        text = resp.text or ""
        if "InvalidLocationConstraint" in text:
            headers = {
                "Content-Length": "0",
            }
            headers = signed_headers(creds, "PUT", url, headers, sha256_bytes(b""), body=b"")
            retry = await session.put(url, data=b"", headers=headers)
            if retry.status_code in (200, 204, 409):
                return
            text = retry.text or ""
            logger.error(f"Create bucket '{bucket}' failed: {retry.status_code} {text.strip()}")
            retry.raise_for_status()

    logger.error(f"Create bucket '{bucket}' failed: {resp.status_code} {resp.text.strip()}")
    resp.raise_for_status()

async def upload_object(session: AsyncSession, creds: Credentials, bucket: str, file_path: Path) -> None:
    key = file_path.name
    url = f"{S3_ENDPOINT}/{bucket}/{key}"
    data, payload_hash = await asyncio.to_thread(read_file_and_hash, file_path)

    headers = {
        "Content-Length": str(len(data)),
    }
    headers = signed_headers(creds, "PUT", url, headers, payload_hash, body=data)

    resp = await session.put(url, data=data, headers=headers)
    if resp.status_code >= 400:
        logger.error(f"Upload '{bucket}/{key}' failed: {resp.status_code} {resp.text.strip()}")
        resp.raise_for_status()

async def handle_bucket(
    sem: asyncio.Semaphore,
    session: AsyncSession,
    creds: Credentials,
    bucket: str,
    file_map: dict
) -> tuple:
    async with sem:
        size_mb = random.choice(FILE_SIZES_MB)
        file_path = file_map[size_mb]
        try:
            await create_bucket(session, creds, bucket)
            await upload_object(session, creds, bucket, file_path)
            return bucket, size_mb
        except Exception as e:
            logger.error(f"Bucket '{bucket}' failed: {e}")
            return bucket, 0

async def run() -> None:
    try:
        creds = build_credentials()
    except KeyError as e:
        logger.error(f"Missing required environment variable: {e.args[0]}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        logger.error(e, file=sys.stderr)
        sys.exit(1)

    file_map = build_file_map()
    buckets = bucket_names()
    random.shuffle(buckets)

    sem = asyncio.Semaphore(MAX_WORKERS)
    stats = {}

    async with AsyncSession(impersonate="chrome136", timeout=600, verify=VERIFY_TLS) as session:
        tasks = [handle_bucket(sem, session, creds, bucket, file_map) for bucket in buckets]
        for task in asyncio.as_completed(tasks):
            bucket, size_mb = await task
            stats[bucket] = size_mb

    print(json.dumps(stats, ensure_ascii=False))

def main() -> None:
    asyncio.run(run())

if __name__ == '__main__':
    main()
