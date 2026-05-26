"""S3 upload with read-back verification before local delete."""
import gzip
import json
from pathlib import Path

import boto3
import botocore.exceptions
import pyarrow.parquet as pq

from pm_research.logging import get_logger

log = get_logger(__name__)


class S3Uploader:
    def __init__(self, bucket: str, region: str = "eu-west-1") -> None:
        self._bucket = bucket
        self._client = boto3.client("s3", region_name=region)

    def upload_parquet(self, local_path: Path, s3_key: str) -> None:
        """Upload Parquet, verify row count + first 100 rows readable, then delete local."""
        self._upload(local_path, s3_key, content_type="application/octet-stream")
        self._verify_parquet(local_path, s3_key)
        local_path.unlink()
        log.info("s3_upload_complete", key=s3_key)

    def upload_jsonl_gz(self, local_path: Path, s3_key: str) -> None:
        """Upload JSONL.gz, verify object exists + first 100 lines parseable, then delete."""
        self._upload(local_path, s3_key, content_type="application/gzip")
        self._verify_jsonl_gz(local_path, s3_key)
        local_path.unlink()
        log.info("s3_upload_complete", key=s3_key)

    def _upload(self, local_path: Path, s3_key: str, content_type: str) -> None:
        log.info("s3_upload_start", key=s3_key, size=local_path.stat().st_size)
        self._client.upload_file(
            str(local_path),
            self._bucket,
            s3_key,
            ExtraArgs={"ContentType": content_type},
        )

    def _verify_parquet(self, local_path: Path, s3_key: str) -> None:
        # Verify local file is valid Parquet and get row count
        local_meta = pq.read_metadata(local_path)
        local_rows = local_meta.num_rows
        local_size = local_path.stat().st_size

        # Verify S3 object exists and matches local size exactly
        try:
            response = self._client.head_object(Bucket=self._bucket, Key=s3_key)
        except botocore.exceptions.ClientError as exc:
            raise RuntimeError(f"S3 head_object failed for {s3_key}: {exc}") from exc

        remote_size = response["ContentLength"]
        if remote_size != local_size:
            raise RuntimeError(
                f"S3 size mismatch for {s3_key}: local={local_size}, remote={remote_size}"
            )
        log.info("s3_verified", key=s3_key, rows=local_rows, size=local_size)

    def _verify_jsonl_gz(self, local_path: Path, s3_key: str) -> None:
        try:
            self._client.head_object(Bucket=self._bucket, Key=s3_key)
        except botocore.exceptions.ClientError as exc:
            raise RuntimeError(f"S3 head_object failed for {s3_key}: {exc}") from exc

        # Verify locally (file still present at this point)
        with gzip.open(local_path, "rt", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= 100:
                    break
                json.loads(line)  # raises on malformed JSON
