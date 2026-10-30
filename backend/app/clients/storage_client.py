"""MinIO Object Storage Client & Presigned URL Provider (TASK-M3-01).

Implements Invariant 1:
- The database keeps metadata and object keys only.
- MinIO bucket 'edusense-documents' is private.
- Web client never accesses MinIO directly; uploads and downloads handled
  via API Backend or presigned URLs.
"""

from __future__ import annotations

import io
import logging
from datetime import timedelta
from typing import Any, BinaryIO

from minio import Minio
from minio.error import S3Error
from urllib3.exceptions import HTTPError, MaxRetryError

from app.core.config import settings

logger = logging.getLogger("edusense.storage")


# ---------------------------------------------------------------------------
# Domain Exceptions
# ---------------------------------------------------------------------------


class StorageError(Exception):
    """Base exception for all storage client errors."""

    def __init__(self, message: str, original_error: Exception | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.original_error = original_error


class StorageConnectionError(StorageError):
    """Raised when communication with the storage backend fails."""


class FileNotFoundStorageError(StorageError):
    """Raised when an object or file is not found in the storage backend."""


class BucketNotFoundError(StorageError):
    """Raised when the specified bucket does not exist in the storage backend."""


# ---------------------------------------------------------------------------
# Storage Client Implementation
# ---------------------------------------------------------------------------


class MinIOStorageClient:
    """Production-grade client for MinIO object storage."""

    def __init__(
        self,
        endpoint: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        bucket_name: str | None = None,
        secure: bool | None = None,
        region: str | None = "us-east-1",
        client: Minio | None = None,
        public_endpoint: str | None = None,
    ) -> None:
        raw_endpoint = endpoint if endpoint is not None else settings.minio_endpoint
        self.access_key = access_key if access_key is not None else settings.minio_access_key
        self.secret_key = secret_key if secret_key is not None else settings.minio_secret_key
        self.default_bucket = (
            bucket_name if bucket_name is not None else settings.minio_bucket_name
        )

        clean_endpoint = raw_endpoint.strip()
        is_secure = secure if secure is not None else settings.minio_secure
        if clean_endpoint.startswith("http://"):
            clean_endpoint = clean_endpoint[len("http://") :]
            if secure is None:
                is_secure = False
        elif clean_endpoint.startswith("https://"):
            clean_endpoint = clean_endpoint[len("https://") :]
            if secure is None:
                is_secure = True

        self.endpoint = clean_endpoint
        self.secure = is_secure
        self.region = region

        if client is not None:
            self._client = client
            self._presign_client = client
        else:
            self._client = Minio(
                endpoint=self.endpoint,
                access_key=self.access_key,
                secret_key=self.secret_key,
                secure=self.secure,
                region=self.region,
            )
            raw_pub = (
                public_endpoint
                if public_endpoint is not None
                else settings.minio_public_endpoint
            )
            if raw_pub and raw_pub.strip():
                clean_pub = raw_pub.strip()
                pub_secure = self.secure
                if clean_pub.startswith("http://"):
                    clean_pub = clean_pub[len("http://") :]
                    pub_secure = False
                elif clean_pub.startswith("https://"):
                    clean_pub = clean_pub[len("https://") :]
                    pub_secure = True
                self._presign_client = Minio(
                    endpoint=clean_pub,
                    access_key=self.access_key,
                    secret_key=self.secret_key,
                    secure=pub_secure,
                    region=self.region or "us-east-1",
                )
            else:
                self._presign_client = self._client

    @property
    def client(self) -> Minio:
        """Return the underlying minio.Minio SDK instance."""
        return self._client

    def _resolve_bucket(self, bucket_name: str | None) -> str:
        """Resolve bucket name defaulting to configured default bucket."""
        return bucket_name if bucket_name is not None else self.default_bucket

    def ensure_bucket_exists(self, bucket_name: str | None = None) -> bool:
        """Verify bucket existence and create it if not present.

        Returns True when bucket exists or was successfully created.
        """
        bucket = self._resolve_bucket(bucket_name)
        try:
            if not self._client.bucket_exists(bucket):
                logger.info("Bucket '%s' does not exist; creating bucket...", bucket)
                self._client.make_bucket(bucket)
                logger.info("Bucket '%s' created successfully.", bucket)
            return True
        except S3Error as exc:
            if exc.code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                return True
            logger.error("S3 error while ensuring bucket '%s': %s", bucket, exc)
            raise StorageError(
                f"Failed to ensure bucket '{bucket}' exists: {exc.message}",
                original_error=exc,
            ) from exc
        except (HTTPError, MaxRetryError) as exc:
            logger.error("Connection failed while checking bucket '%s': %s", bucket, exc)
            raise StorageConnectionError(
                f"Failed to connect to storage service while ensuring bucket '{bucket}': {exc}",
                original_error=exc,
            ) from exc
        except StorageError:
            raise
        except Exception as exc:
            logger.error("Unexpected error ensuring bucket '%s': %s", bucket, exc)
            raise StorageError(
                f"Unexpected error ensuring bucket '{bucket}' exists: {exc}",
                original_error=exc,
            ) from exc

    def generate_presigned_upload_url(
        self,
        object_name: str,
        expires_delta: timedelta = timedelta(minutes=15),
        bucket_name: str | None = None,
    ) -> str:
        """Generate presigned PUT URL for direct client upload."""
        bucket = self._resolve_bucket(bucket_name)
        try:
            url: str = self._presign_client.presigned_put_object(
                bucket_name=bucket,
                object_name=object_name,
                expires=expires_delta,
            )
            return url
        except S3Error as exc:
            if exc.code in ("NoSuchBucket", "BucketNotFound"):
                raise BucketNotFoundError(
                    f"Bucket '{bucket}' not found",
                    original_error=exc,
                ) from exc
            raise StorageError(
                f"Failed to generate presigned upload URL: {exc.message}",
                original_error=exc,
            ) from exc
        except (HTTPError, MaxRetryError) as exc:
            raise StorageConnectionError(
                f"Connection error while generating upload URL: {exc}",
                original_error=exc,
            ) from exc
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(
                f"Unexpected error while generating upload URL: {exc}",
                original_error=exc,
            ) from exc

    def generate_presigned_download_url(
        self,
        object_name: str,
        expires_delta: timedelta = timedelta(minutes=15),
        bucket_name: str | None = None,
        response_headers: dict[str, str] | None = None,
    ) -> str:
        """Generate presigned GET URL for authorized client download."""
        bucket = self._resolve_bucket(bucket_name)

        clean_headers: dict[str, str | list[str] | tuple[str]] | None = None
        if response_headers:
            clean_headers = {}
            for k, v in response_headers.items():
                lower_k = k.strip().lower()
                if lower_k in (
                    "content-disposition",
                    "content-type",
                    "content-language",
                    "content-encoding",
                    "cache-control",
                    "expires",
                ):
                    clean_headers[f"response-{lower_k}"] = str(v)
                else:
                    clean_headers[k] = str(v)

        try:
            url: str = self._presign_client.presigned_get_object(
                bucket_name=bucket,
                object_name=object_name,
                expires=expires_delta,
                response_headers=clean_headers,
            )
            return url
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NotFound", "ResourceNotFound"):
                raise FileNotFoundStorageError(
                    f"Object '{object_name}' not found in bucket '{bucket}'",
                    original_error=exc,
                ) from exc
            if exc.code in ("NoSuchBucket", "BucketNotFound"):
                raise BucketNotFoundError(
                    f"Bucket '{bucket}' not found",
                    original_error=exc,
                ) from exc
            raise StorageError(
                f"Failed to generate presigned download URL: {exc.message}",
                original_error=exc,
            ) from exc
        except (HTTPError, MaxRetryError) as exc:
            raise StorageConnectionError(
                f"Connection error while generating download URL: {exc}",
                original_error=exc,
            ) from exc
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(
                f"Unexpected error while generating download URL: {exc}",
                original_error=exc,
            ) from exc

    def upload_file(
        self,
        object_name: str,
        file_data: bytes | BinaryIO,
        length: int | None = None,
        content_type: str = "application/octet-stream",
        bucket_name: str | None = None,
    ) -> str:
        """Direct upload helper supporting raw bytes or file-like streams."""
        bucket = self._resolve_bucket(bucket_name)

        if isinstance(file_data, bytes):
            stream: BinaryIO = io.BytesIO(file_data)
            upload_length = len(file_data) if (length is None or length < 0) else length
            part_size = 0
        else:
            stream = file_data
            if length is not None and length >= 0:
                upload_length = length
                part_size = 0
            else:
                try:
                    current_pos = stream.tell()
                    stream.seek(0, io.SEEK_END)
                    upload_length = stream.tell() - current_pos
                    stream.seek(current_pos)
                    part_size = 0
                except (OSError, AttributeError, io.UnsupportedOperation):
                    upload_length = -1
                    part_size = 10 * 1024 * 1024

        try:
            self._client.put_object(
                bucket_name=bucket,
                object_name=object_name,
                data=stream,
                length=upload_length,
                content_type=content_type,
                part_size=part_size,
            )
            return object_name
        except S3Error as exc:
            if exc.code in ("NoSuchBucket", "BucketNotFound"):
                raise BucketNotFoundError(
                    f"Bucket '{bucket}' not found",
                    original_error=exc,
                ) from exc
            raise StorageError(
                f"Failed to upload object '{object_name}': {exc.message}",
                original_error=exc,
            ) from exc
        except (HTTPError, MaxRetryError) as exc:
            raise StorageConnectionError(
                f"Connection error while uploading '{object_name}': {exc}",
                original_error=exc,
            ) from exc
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(
                f"Unexpected error while uploading '{object_name}': {exc}",
                original_error=exc,
            ) from exc

    def download_file(self, object_name: str, bucket_name: str | None = None) -> bytes:
        """Download object content directly as bytes."""
        bucket = self._resolve_bucket(bucket_name)
        response = None
        try:
            response = self._client.get_object(bucket_name=bucket, object_name=object_name)
            data: bytes = response.read()
            return data
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NotFound", "ResourceNotFound"):
                raise FileNotFoundStorageError(
                    f"Object '{object_name}' not found in bucket '{bucket}'",
                    original_error=exc,
                ) from exc
            if exc.code in ("NoSuchBucket", "BucketNotFound"):
                raise BucketNotFoundError(
                    f"Bucket '{bucket}' not found",
                    original_error=exc,
                ) from exc
            raise StorageError(
                f"Failed to download object '{object_name}': {exc.message}",
                original_error=exc,
            ) from exc
        except (HTTPError, MaxRetryError) as exc:
            raise StorageConnectionError(
                f"Connection error while downloading '{object_name}': {exc}",
                original_error=exc,
            ) from exc
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(
                f"Unexpected error downloading object '{object_name}': {exc}",
                original_error=exc,
            ) from exc
        finally:
            if response is not None:
                try:
                    response.close()
                    response.release_conn()
                except (OSError, HTTPError) as err:
                    logger.debug("Error releasing storage response stream: %s", err)

    def delete_file(self, object_name: str, bucket_name: str | None = None) -> bool:
        """Remove object from storage bucket.

        Returns True on success.
        """
        bucket = self._resolve_bucket(bucket_name)
        try:
            self._client.remove_object(bucket_name=bucket, object_name=object_name)
            return True
        except S3Error as exc:
            if exc.code in ("NoSuchBucket", "BucketNotFound"):
                raise BucketNotFoundError(
                    f"Bucket '{bucket}' not found",
                    original_error=exc,
                ) from exc
            raise StorageError(
                f"Failed to delete object '{object_name}': {exc.message}",
                original_error=exc,
            ) from exc
        except (HTTPError, MaxRetryError) as exc:
            raise StorageConnectionError(
                f"Connection error while deleting '{object_name}': {exc}",
                original_error=exc,
            ) from exc
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(
                f"Unexpected error deleting object '{object_name}': {exc}",
                original_error=exc,
            ) from exc

    def file_exists(self, object_name: str, bucket_name: str | None = None) -> bool:
        """Check whether an object exists in storage bucket."""
        bucket = self._resolve_bucket(bucket_name)
        try:
            self._client.stat_object(bucket_name=bucket, object_name=object_name)
            return True
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NotFound", "ResourceNotFound"):
                return False
            if exc.code in ("NoSuchBucket", "BucketNotFound"):
                raise BucketNotFoundError(
                    f"Bucket '{bucket}' not found",
                    original_error=exc,
                ) from exc
            raise StorageError(
                f"Failed to check existence for object '{object_name}': {exc.message}",
                original_error=exc,
            ) from exc
        except (HTTPError, MaxRetryError) as exc:
            raise StorageConnectionError(
                f"Connection error while checking '{object_name}': {exc}",
                original_error=exc,
            ) from exc
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(
                f"Unexpected error checking object '{object_name}': {exc}",
                original_error=exc,
            ) from exc

    def get_file_metadata(
        self,
        object_name: str,
        bucket_name: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve metadata for an object in storage bucket."""
        bucket = self._resolve_bucket(bucket_name)
        try:
            stat = self._client.stat_object(bucket_name=bucket, object_name=object_name)
            return {
                "object_name": stat.object_name or object_name,
                "size": stat.size,
                "content_type": stat.content_type,
                "last_modified": stat.last_modified,
                "etag": stat.etag,
                "metadata": stat.metadata,
            }
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NotFound", "ResourceNotFound"):
                raise FileNotFoundStorageError(
                    f"Object '{object_name}' not found in bucket '{bucket}'",
                    original_error=exc,
                ) from exc
            if exc.code in ("NoSuchBucket", "BucketNotFound"):
                raise BucketNotFoundError(
                    f"Bucket '{bucket}' not found",
                    original_error=exc,
                ) from exc
            raise StorageError(
                f"Failed to get metadata for object '{object_name}': {exc.message}",
                original_error=exc,
            ) from exc
        except (HTTPError, MaxRetryError) as exc:
            raise StorageConnectionError(
                f"Connection error while getting metadata for '{object_name}': {exc}",
                original_error=exc,
            ) from exc
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(
                f"Unexpected error getting metadata for object '{object_name}': {exc}",
                original_error=exc,
            ) from exc


# Type alias for general interface
StorageClient = MinIOStorageClient

# Singleton instance holder
_storage_client_instance: MinIOStorageClient | None = None


def get_storage_client() -> MinIOStorageClient:
    """FastAPI dependency provider returning MinIOStorageClient singleton."""
    global _storage_client_instance
    if _storage_client_instance is None:
        _storage_client_instance = MinIOStorageClient()
    return _storage_client_instance


def set_storage_client(client: MinIOStorageClient | None) -> None:
    """Set or reset storage client singleton (useful for testing and dependency overrides)."""
    global _storage_client_instance
    _storage_client_instance = client
