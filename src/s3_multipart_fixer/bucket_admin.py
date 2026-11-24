from __future__ import annotations

from botocore.client import BaseClient
from botocore.exceptions import ClientError
from loguru import logger


class BucketAdmin:
    def __init__(self, client: BaseClient) -> None:
        self._client = client

    def create_bucket(self, bucket: str) -> dict[str, object]:
        logger.debug("calling HeadBucket via boto3: bucket={}", bucket)
        try:
            self._client.head_bucket(Bucket=bucket)
            logger.info("bucket already exists: bucket={}", bucket)
            return {
                "bucket": bucket,
                "created": False,
                "already_exists": True,
            }
        except ClientError as error:
            error_code = error.response.get("Error", {}).get("Code", "")
            status_code = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if error_code not in {"404", "NoSuchBucket", "NotFound"} and status_code != 404:
                raise

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
        return {
            "bucket": bucket,
            "created": True,
            "already_exists": False,
        }
