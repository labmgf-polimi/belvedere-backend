import boto3
from botocore.client import BaseClient, Config
from django.conf import settings
from storages.backends.s3 import S3Storage


def _client_config() -> Config:
    return Config(
        signature_version="s3v4",
        s3={
            "addressing_style": "virtual",
            "payload_signing_enabled": False,
        },
    )


def _make_s3_client(access_key: str, secret_key: str) -> BaseClient:
    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=settings.S3_REGION_NAME,
        config=_client_config(),
    )


def build_s3_storage(bucket: str) -> S3Storage:
    """Django file storage on `bucket`, with the same client setup as above.

    Private objects, presigned `.url`, and no silent overwrite of an existing key.
    """
    return S3Storage(
        bucket_name=bucket,
        endpoint_url=settings.S3_ENDPOINT_URL,
        access_key=settings.S3_ACCESS_KEY,
        secret_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION_NAME,
        client_config=_client_config(),
        default_acl=None,
        querystring_auth=True,
        file_overwrite=False,
    )


def build_s3_client() -> BaseClient:
    """Build an S3 client using the read-write credentials."""
    return _make_s3_client(settings.S3_ACCESS_KEY, settings.S3_SECRET_KEY)


def build_readonly_s3_client() -> BaseClient:
    """Build an S3 client using read-only credentials.

    Falls back to the read-write credentials if read-only keys are not configured.
    """
    access_key = (
        getattr(settings, "S3_READONLY_ACCESS_KEY", "") or settings.S3_ACCESS_KEY
    )
    secret_key = (
        getattr(settings, "S3_READONLY_SECRET_KEY", "") or settings.S3_SECRET_KEY
    )
    return _make_s3_client(access_key, secret_key)


def get_object_bytes(s3: BaseClient, bucket: str, key: str) -> tuple[bytes, str | None]:
    """Fetch an object from S3 and return its bytes and content type."""
    response = s3.get_object(Bucket=bucket, Key=key)
    return response["Body"].read(), response.get("ContentType")


def put_object_bytes(
    s3: BaseClient, bucket: str, key: str, data: bytes, content_type: str
) -> None:
    """Store bytes in S3 with the given content type."""
    s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)


def generate_presigned_url(
    s3: BaseClient, bucket: str, key: str, expires_in: int = 900
) -> str:
    """Return a presigned GET URL for an S3 object, valid for expires_in seconds."""
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_in,
    )
