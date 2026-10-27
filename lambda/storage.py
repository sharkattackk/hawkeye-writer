"""Imagery bucket helpers (needs storage.access set in config.yaml).

URLs are signed locally with the Lambda's role, so creating them needs no network
access. head() calls S3, which works inside the database VPC via its S3 endpoint.
"""
import os

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# Regional virtual-hosted URLs, so browsers are never redirected (which breaks CORS)
_s3 = boto3.client(
    "s3",
    region_name=os.environ.get("AWS_REGION"),
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)


def bucket():
    return os.environ["IMAGERY_BUCKET"]


def presigned_upload(key, content_type, max_bytes, expires_in=900):
    """Form for a browser POST upload: {"url", "fields"}. S3 enforces the type and size."""
    return _s3.generate_presigned_post(
        bucket(),
        key,
        Fields={"Content-Type": content_type},
        Conditions=[{"Content-Type": content_type}, ["content-length-range", 1, max_bytes]],
        ExpiresIn=expires_in,
    )


def presigned_view_url(key, expires_in=3600):
    """Temporary URL a browser can load the object from."""
    return _s3.generate_presigned_url(
        "get_object", Params={"Bucket": bucket(), "Key": key}, ExpiresIn=expires_in
    )


def head(key):
    """Object metadata ({"size", "content_type"}), or None if it doesn't exist."""
    try:
        response = _s3.head_object(Bucket=bucket(), Key=key)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    return {"size": response["ContentLength"], "content_type": response.get("ContentType")}


def delete(keys):
    """Delete objects (write access). Keys that don't exist are fine."""
    keys = list(keys)
    for start in range(0, len(keys), 1000):  # S3's limit per request
        batch = [{"Key": key} for key in keys[start:start + 1000]]
        _s3.delete_objects(Bucket=bucket(), Delete={"Objects": batch, "Quiet": True})
