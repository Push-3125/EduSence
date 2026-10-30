"""External service clients for EduSense backend."""

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

__all__ = [
    "BucketNotFoundError",
    "FileNotFoundStorageError",
    "MinIOStorageClient",
    "StorageClient",
    "StorageConnectionError",
    "StorageError",
    "get_storage_client",
    "set_storage_client",
]
