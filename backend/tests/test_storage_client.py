"""Unit tests for MinIO Object Storage Client & Presigned URL Provider (TASK-M3-01)."""

import io
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure backend root is on sys.path for direct test runs
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
from minio.datatypes import Object
from minio.error import S3Error
from urllib3.exceptions import HTTPError, MaxRetryError

from app.clients.storage_client import (
    BucketNotFoundError,
    FileNotFoundStorageError,
    MinIOStorageClient,
    StorageClient,
    StorageConnectionError,
    StorageError,
    get_storage_client,
    set_storage_client,
)
from app.core.config import settings

# ---------------------------------------------------------------------------
# Test Helpers
# ---------------------------------------------------------------------------


def make_s3_error(code: str, message: str = "S3 operation failed") -> S3Error:
    """Helper to construct S3Error instances with appropriate codes."""
    return S3Error(
        code=code,
        message=message,
        resource="/test/resource",
        request_id="req-123",
        host_id="host-123",
        response=MagicMock(),
    )


@pytest.fixture
def mock_minio() -> MagicMock:
    """Create a mock minio.Minio SDK client."""
    return MagicMock()


@pytest.fixture
def storage_client(mock_minio: MagicMock) -> MinIOStorageClient:
    """Provide a MinIOStorageClient with an injected mock Minio SDK."""
    return MinIOStorageClient(
        endpoint="localhost:9000",
        access_key="test-access",
        secret_key="test-secret",
        bucket_name="test-bucket",
        secure=False,
        client=mock_minio,
    )


# ---------------------------------------------------------------------------
# Initialization & Configuration Tests
# ---------------------------------------------------------------------------


class TestStorageClientInit:
    """Verify initialization, URL cleaning, and configuration binding."""

    def test_init_defaults_from_settings(self) -> None:
        """Client should default to values from application settings."""
        with patch.object(settings, "minio_public_endpoint", None), patch("app.clients.storage_client.Minio") as mock_cls:
            client = MinIOStorageClient()
            assert client.default_bucket == settings.minio_bucket_name
            assert client.access_key == settings.minio_access_key
            assert client.secret_key == settings.minio_secret_key
            assert client.endpoint == settings.minio_endpoint.replace("http://", "").replace("https://", "")
            mock_cls.assert_called_once()

    def test_init_strips_http_scheme(self) -> None:
        """Client strips http:// scheme and maintains secure=False."""
        with patch.object(settings, "minio_public_endpoint", None), patch("app.clients.storage_client.Minio") as mock_cls:
            client = MinIOStorageClient(endpoint="http://storage.internal:9000")
            assert client.endpoint == "storage.internal:9000"
            assert client.secure is False
            mock_cls.assert_called_once_with(
                endpoint="storage.internal:9000",
                access_key=settings.minio_access_key,
                secret_key=settings.minio_secret_key,
                secure=False,
                region="us-east-1",
            )

    def test_init_strips_https_scheme(self) -> None:
        """Client strips https:// scheme and sets secure=True when not specified."""
        with patch.object(settings, "minio_public_endpoint", None), patch("app.clients.storage_client.Minio") as mock_cls:
            client = MinIOStorageClient(endpoint="https://s3.example.com")
            assert client.endpoint == "s3.example.com"
            assert client.secure is True
            mock_cls.assert_called_once_with(
                endpoint="s3.example.com",
                access_key=settings.minio_access_key,
                secret_key=settings.minio_secret_key,
                secure=True,
                region="us-east-1",
            )

    def test_client_property_and_alias(self, storage_client: MinIOStorageClient, mock_minio: MagicMock) -> None:
        """Property exposes raw client and StorageClient is an alias."""
        assert storage_client.client is mock_minio
        assert StorageClient is MinIOStorageClient


# ---------------------------------------------------------------------------
# Bucket Management Tests
# ---------------------------------------------------------------------------


class TestEnsureBucketExists:
    """Verify ensure_bucket_exists behavior across state conditions."""

    def test_bucket_already_exists(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """When bucket exists, make_bucket should not be invoked."""
        mock_minio.bucket_exists.return_value = True

        result = storage_client.ensure_bucket_exists()

        assert result is True
        mock_minio.bucket_exists.assert_called_once_with("test-bucket")
        mock_minio.make_bucket.assert_not_called()

    def test_bucket_created_when_missing(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """When bucket does not exist, make_bucket should be invoked."""
        mock_minio.bucket_exists.return_value = False

        result = storage_client.ensure_bucket_exists(bucket_name="custom-bucket")

        assert result is True
        mock_minio.bucket_exists.assert_called_once_with("custom-bucket")
        mock_minio.make_bucket.assert_called_once_with("custom-bucket")

    def test_bucket_concurrent_creation_race_handled(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """BucketAlreadyOwnedByYou from race condition should still return True."""
        mock_minio.bucket_exists.return_value = False
        mock_minio.make_bucket.side_effect = make_s3_error("BucketAlreadyOwnedByYou")

        result = storage_client.ensure_bucket_exists()

        assert result is True

    def test_bucket_connection_error_raises_storage_connection_error(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """Network/transport errors raise StorageConnectionError."""
        mock_minio.bucket_exists.side_effect = MaxRetryError(
            pool=MagicMock(), url="http://localhost:9000", reason=Exception("Connection refused")
        )

        with pytest.raises(StorageConnectionError) as exc_info:
            storage_client.ensure_bucket_exists()
        assert "Failed to connect to storage service" in str(exc_info.value)

    def test_bucket_s3_error_raises_storage_error(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """Other S3 errors raise StorageError."""
        mock_minio.bucket_exists.side_effect = make_s3_error("AccessDenied", "Access denied to bucket")

        with pytest.raises(StorageError) as exc_info:
            storage_client.ensure_bucket_exists()
        assert "Failed to ensure bucket 'test-bucket' exists" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Presigned Upload URL Tests
# ---------------------------------------------------------------------------


class TestPresignedUploadUrl:
    """Verify presigned upload PUT URL generation."""

    def test_generate_upload_url_success(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """Generate presigned PUT URL with custom delta and bucket."""
        mock_minio.presigned_put_object.return_value = "https://s3.local/test-bucket/doc.pdf?signature=abc"
        delta = timedelta(minutes=30)

        url = storage_client.generate_presigned_upload_url(
            object_name="doc.pdf",
            expires_delta=delta,
            bucket_name="custom-bucket",
        )

        assert url == "https://s3.local/test-bucket/doc.pdf?signature=abc"
        mock_minio.presigned_put_object.assert_called_once_with(
            bucket_name="custom-bucket",
            object_name="doc.pdf",
            expires=delta,
        )

    def test_generate_upload_url_bucket_not_found(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """NoSuchBucket error translates to BucketNotFoundError."""
        mock_minio.presigned_put_object.side_effect = make_s3_error("NoSuchBucket")

        with pytest.raises(BucketNotFoundError):
            storage_client.generate_presigned_upload_url(object_name="doc.pdf")

    def test_generate_upload_url_connection_error(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """Connection errors translate to StorageConnectionError."""
        mock_minio.presigned_put_object.side_effect = HTTPError("Network unreachable")

        with pytest.raises(StorageConnectionError):
            storage_client.generate_presigned_upload_url(object_name="doc.pdf")


# ---------------------------------------------------------------------------
# Presigned Download URL Tests
# ---------------------------------------------------------------------------


class TestPresignedDownloadUrl:
    """Verify presigned download GET URL generation."""

    def test_generate_download_url_with_response_headers(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """Standard headers like content-disposition are normalized to response- prefix."""
        mock_minio.presigned_get_object.return_value = "https://s3.local/download?sig=xyz"
        headers = {
            "content-disposition": "attachment; filename=assignment.pdf",
            "content-type": "application/pdf",
            "response-cache-control": "no-cache",
        }

        url = storage_client.generate_presigned_download_url(
            object_name="assignments/assignment.pdf",
            expires_delta=timedelta(minutes=10),
            response_headers=headers,
        )

        assert url == "https://s3.local/download?sig=xyz"
        mock_minio.presigned_get_object.assert_called_once_with(
            bucket_name="test-bucket",
            object_name="assignments/assignment.pdf",
            expires=timedelta(minutes=10),
            response_headers={
                "response-content-disposition": "attachment; filename=assignment.pdf",
                "response-content-type": "application/pdf",
                "response-cache-control": "no-cache",
            },
        )

    def test_generate_download_url_file_not_found(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """NoSuchKey translates to FileNotFoundStorageError."""
        mock_minio.presigned_get_object.side_effect = make_s3_error("NoSuchKey")

        with pytest.raises(FileNotFoundStorageError):
            storage_client.generate_presigned_download_url(object_name="missing.pdf")

    def test_generate_download_url_bucket_not_found(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """NoSuchBucket translates to BucketNotFoundError."""
        mock_minio.presigned_get_object.side_effect = make_s3_error("NoSuchBucket")

        with pytest.raises(BucketNotFoundError):
            storage_client.generate_presigned_download_url(object_name="doc.pdf")


# ---------------------------------------------------------------------------
# Direct File Upload Tests
# ---------------------------------------------------------------------------


class TestUploadFile:
    """Verify direct file uploads via bytes and streams."""

    def test_upload_raw_bytes_without_explicit_length(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """Uploading bytes infers length automatically."""
        payload = b"Sample document bytes content"

        result = storage_client.upload_file(
            object_name="docs/test.txt",
            file_data=payload,
            content_type="text/plain",
        )

        assert result == "docs/test.txt"
        assert mock_minio.put_object.call_count == 1
        call_kwargs = mock_minio.put_object.call_args[1]
        assert call_kwargs["bucket_name"] == "test-bucket"
        assert call_kwargs["object_name"] == "docs/test.txt"
        assert call_kwargs["length"] == len(payload)
        assert call_kwargs["content_type"] == "text/plain"
        assert isinstance(call_kwargs["data"], io.BytesIO)
        assert call_kwargs["data"].getvalue() == payload

    def test_upload_binary_stream_with_seekable_length(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """Uploading seekable stream infers length when not provided."""
        payload = b"Binary stream content"
        stream = io.BytesIO(payload)

        result = storage_client.upload_file(
            object_name="docs/stream.bin",
            file_data=stream,
            content_type="application/octet-stream",
            bucket_name="custom-bucket",
        )

        assert result == "docs/stream.bin"
        call_kwargs = mock_minio.put_object.call_args[1]
        assert call_kwargs["bucket_name"] == "custom-bucket"
        assert call_kwargs["length"] == len(payload)

    def test_upload_with_explicit_length(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """Explicit length argument is respected."""
        payload = b"Exact length content"
        stream = io.BytesIO(payload)

        storage_client.upload_file(
            object_name="docs/exact.bin",
            file_data=stream,
            length=len(payload),
        )

        call_kwargs = mock_minio.put_object.call_args[1]
        assert call_kwargs["length"] == len(payload)

    def test_upload_bucket_not_found(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """NoSuchBucket raises BucketNotFoundError."""
        mock_minio.put_object.side_effect = make_s3_error("NoSuchBucket")

        with pytest.raises(BucketNotFoundError):
            storage_client.upload_file("test.pdf", b"abc")

    def test_upload_connection_error(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """Connection error during upload raises StorageConnectionError."""
        mock_minio.put_object.side_effect = HTTPError("Connection reset")

        with pytest.raises(StorageConnectionError):
            storage_client.upload_file("test.pdf", b"abc")


# ---------------------------------------------------------------------------
# Direct File Download Tests
# ---------------------------------------------------------------------------


class TestDownloadFile:
    """Verify downloading file content and stream cleanup."""

    def test_download_file_success(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """Successful download reads stream and releases connection."""
        mock_response = MagicMock()
        mock_response.read.return_value = b"Downloaded file content"
        mock_minio.get_object.return_value = mock_response

        content = storage_client.download_file("docs/report.pdf")

        assert content == b"Downloaded file content"
        mock_minio.get_object.assert_called_once_with(
            bucket_name="test-bucket",
            object_name="docs/report.pdf",
        )
        mock_response.read.assert_called_once()
        mock_response.close.assert_called_once()
        mock_response.release_conn.assert_called_once()

    def test_download_file_not_found(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """NoSuchKey error raises FileNotFoundStorageError."""
        mock_minio.get_object.side_effect = make_s3_error("NoSuchKey", "Object does not exist")

        with pytest.raises(FileNotFoundStorageError) as exc_info:
            storage_client.download_file("nonexistent.pdf")
        assert "nonexistent.pdf" in str(exc_info.value)

    def test_download_bucket_not_found(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """NoSuchBucket error raises BucketNotFoundError."""
        mock_minio.get_object.side_effect = make_s3_error("NoSuchBucket")

        with pytest.raises(BucketNotFoundError):
            storage_client.download_file("file.pdf")

    def test_download_connection_error(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """Connection error raises StorageConnectionError."""
        mock_minio.get_object.side_effect = HTTPError("Read timeout")

        with pytest.raises(StorageConnectionError):
            storage_client.download_file("file.pdf")


# ---------------------------------------------------------------------------
# File Exists & Metadata Tests
# ---------------------------------------------------------------------------


class TestFileExistsAndMetadata:
    """Verify object existence checking and metadata queries."""

    def test_file_exists_true(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """stat_object success confirms file existence."""
        mock_minio.stat_object.return_value = MagicMock()

        assert storage_client.file_exists("existing.pdf") is True
        mock_minio.stat_object.assert_called_once_with(
            bucket_name="test-bucket",
            object_name="existing.pdf",
        )

    def test_file_exists_false_on_missing(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """NoSuchKey from stat_object returns False."""
        mock_minio.stat_object.side_effect = make_s3_error("NoSuchKey")

        assert storage_client.file_exists("missing.pdf") is False

    def test_file_exists_bucket_not_found(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """NoSuchBucket raises BucketNotFoundError."""
        mock_minio.stat_object.side_effect = make_s3_error("NoSuchBucket")

        with pytest.raises(BucketNotFoundError):
            storage_client.file_exists("doc.pdf")

    def test_get_file_metadata_success(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """Retrieves size, content_type, last_modified, etag, and metadata."""
        now = datetime.now(UTC)
        mock_stat = Object(
            bucket_name="test-bucket",
            object_name="notes/lecture1.pdf",
            last_modified=now,
            etag="etag-456",
            size=2048,
            content_type="application/pdf",
            metadata={"x-amz-meta-custom": "study-material"},
        )
        mock_minio.stat_object.return_value = mock_stat

        meta = storage_client.get_file_metadata("notes/lecture1.pdf")

        assert meta["object_name"] == "notes/lecture1.pdf"
        assert meta["size"] == 2048
        assert meta["content_type"] == "application/pdf"
        assert meta["last_modified"] == now
        assert meta["etag"] == "etag-456"
        assert meta["metadata"] == {"x-amz-meta-custom": "study-material"}

    def test_get_file_metadata_missing_raises_file_not_found(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """NoSuchKey raises FileNotFoundStorageError."""
        mock_minio.stat_object.side_effect = make_s3_error("NoSuchKey")

        with pytest.raises(FileNotFoundStorageError):
            storage_client.get_file_metadata("missing.pdf")

    def test_get_file_metadata_bucket_not_found(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """NoSuchBucket raises BucketNotFoundError."""
        mock_minio.stat_object.side_effect = make_s3_error("NoSuchBucket")

        with pytest.raises(BucketNotFoundError):
            storage_client.get_file_metadata("doc.pdf")


# ---------------------------------------------------------------------------
# File Deletion Tests
# ---------------------------------------------------------------------------


class TestDeleteFile:
    """Verify file deletion and error handling."""

    def test_delete_file_success(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """Successful remove_object returns True."""
        result = storage_client.delete_file("obsolete.pdf")

        assert result is True
        mock_minio.remove_object.assert_called_once_with(
            bucket_name="test-bucket",
            object_name="obsolete.pdf",
        )

    def test_delete_file_bucket_not_found(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """NoSuchBucket raises BucketNotFoundError."""
        mock_minio.remove_object.side_effect = make_s3_error("NoSuchBucket")

        with pytest.raises(BucketNotFoundError):
            storage_client.delete_file("obsolete.pdf")

    def test_delete_file_connection_error(
        self,
        storage_client: MinIOStorageClient,
        mock_minio: MagicMock,
    ) -> None:
        """Connection error raises StorageConnectionError."""
        mock_minio.remove_object.side_effect = HTTPError("Connection broken")

        with pytest.raises(StorageConnectionError):
            storage_client.delete_file("obsolete.pdf")


# ---------------------------------------------------------------------------
# Dependency Provider Tests
# ---------------------------------------------------------------------------


class TestDependencyProvider:
    """Verify FastAPI dependency injection provider."""

    def test_get_and_set_storage_client(self) -> None:
        """get_storage_client returns singleton; set_storage_client overrides it."""
        original = get_storage_client()
        assert isinstance(original, MinIOStorageClient)

        mock_override = MagicMock(spec=MinIOStorageClient)
        set_storage_client(mock_override)

        assert get_storage_client() is mock_override

        # Reset back
        set_storage_client(None)
        assert get_storage_client() is not mock_override
