"""Storage providers with upload verification for S3-compatible and local targets."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from .config import BackupConfig
from .errors import BackupError


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


class StorageProvider(ABC):
    storage_type: str

    @abstractmethod
    def check_connection(self) -> None: ...

    @abstractmethod
    def upload(self, local_path: Path, object_key: str, metadata: dict[str, str]) -> str: ...

    @abstractmethod
    def get_object_metadata(self, object_key: str) -> dict[str, Any]: ...

    @abstractmethod
    def download(self, object_key: str, destination: Path) -> Path: ...

    @abstractmethod
    def delete(self, object_key: str) -> None: ...

    def verify(self, object_key: str, size: int, checksum: str) -> None:
        metadata = self.get_object_metadata(object_key)
        actual_size = int(metadata.get("size", -1))
        actual_checksum = str(metadata.get("sha256", ""))
        if actual_size != size:
            raise BackupError(
                "upload-verification",
                "ObjectSizeMismatch",
                f"Stored object size mismatch: expected {size}, received {actual_size}",
            )
        if actual_checksum != checksum:
            raise BackupError(
                "upload-verification", "ObjectChecksumMismatch", "Stored object SHA-256 metadata does not match"
            )


class S3StorageProvider(StorageProvider):
    def __init__(self, config: BackupConfig):
        self.config = config
        self.storage_type = config.storage_type
        self.bucket = config.storage_bucket_name or ""
        self.client = boto3.client(
            "s3",
            aws_access_key_id=config.storage_access_key_id,
            aws_secret_access_key=config.storage_secret_access_key,
            region_name="auto" if config.storage_type == "r2" else config.storage_region,
            endpoint_url=config.storage_endpoint_url,
            config=BotoConfig(
                connect_timeout=30,
                read_timeout=300,
                retries={"max_attempts": 1, "mode": "standard"},
            ),
        )
        self.transfer_config = TransferConfig(
            multipart_threshold=64 * 1024 * 1024,
            multipart_chunksize=16 * 1024 * 1024,
            max_concurrency=4,
        )

    def check_connection(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except (ClientError, BotoCoreError) as exc:
            raise BackupError(
                "connection", "StorageConnectionFailed", "S3-compatible storage connection failed", cause=exc
            ) from exc

    def upload(self, local_path: Path, object_key: str, metadata: dict[str, str]) -> str:
        try:
            self.client.upload_file(
                str(local_path),
                self.bucket,
                object_key,
                ExtraArgs={"Metadata": {key: str(value) for key, value in metadata.items()}},
                Config=self.transfer_config,
            )
        except (ClientError, BotoCoreError, OSError) as exc:
            raise BackupError("upload", "StorageUploadFailed", "S3-compatible upload failed", cause=exc) from exc
        return object_key

    def get_object_metadata(self, object_key: str) -> dict[str, Any]:
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=object_key)
        except (ClientError, BotoCoreError) as exc:
            raise BackupError(
                "upload-verification", "ObjectMetadataUnavailable", "Could not read stored object metadata", cause=exc
            ) from exc
        custom = response.get("Metadata", {})
        return {"size": response.get("ContentLength"), "sha256": custom.get("sha256", "")}

    def download(self, object_key: str, destination: Path) -> Path:
        try:
            self.client.download_file(self.bucket, object_key, str(destination))
        except (ClientError, BotoCoreError, OSError) as exc:
            raise BackupError("storage", "StorageDownloadFailed", "S3-compatible download failed", cause=exc) from exc
        return destination

    def delete(self, object_key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=object_key)
        except (ClientError, BotoCoreError) as exc:
            raise BackupError("storage", "StorageDeleteFailed", "S3-compatible delete failed", cause=exc) from exc


class LocalStorageProvider(StorageProvider):
    storage_type = "local"

    def __init__(self, base_path: Path):
        self.base_path = base_path

    def check_connection(self) -> None:
        try:
            self.base_path.mkdir(parents=True, exist_ok=True, mode=0o750)
            if not os.access(self.base_path, os.R_OK | os.W_OK):
                raise OSError("directory is not readable and writable")
        except OSError as exc:
            raise BackupError(
                "connection", "StorageConnectionFailed", "Local storage is unavailable", cause=exc
            ) from exc

    def _path(self, object_key: str) -> Path:
        target = (self.base_path / object_key).resolve()
        base = self.base_path.resolve()
        if target != base and base not in target.parents:
            raise BackupError("storage", "InvalidObjectKey", "Object key escapes local storage root")
        return target

    def upload(self, local_path: Path, object_key: str, metadata: dict[str, str]) -> str:
        target = self._path(object_key)
        try:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
            fd, temp_name = tempfile.mkstemp(prefix=".upload-", dir=target.parent)
            os.close(fd)
            temp_path = Path(temp_name)
            try:
                shutil.copyfile(local_path, temp_path)
                temp_path.chmod(0o600)
                os.replace(temp_path, target)
            finally:
                temp_path.unlink(missing_ok=True)
        except OSError as exc:
            raise BackupError("upload", "StorageUploadFailed", "Local storage copy failed", cause=exc) from exc
        return object_key

    def get_object_metadata(self, object_key: str) -> dict[str, Any]:
        target = self._path(object_key)
        try:
            return {"size": target.stat().st_size, "sha256": sha256_file(target)}
        except OSError as exc:
            raise BackupError(
                "upload-verification", "ObjectMetadataUnavailable", "Local stored object is unavailable", cause=exc
            ) from exc

    def download(self, object_key: str, destination: Path) -> Path:
        try:
            shutil.copyfile(self._path(object_key), destination)
        except OSError as exc:
            raise BackupError("storage", "StorageDownloadFailed", "Local storage download failed", cause=exc) from exc
        return destination

    def delete(self, object_key: str) -> None:
        try:
            self._path(object_key).unlink(missing_ok=True)
        except OSError as exc:
            raise BackupError("storage", "StorageDeleteFailed", "Local storage delete failed", cause=exc) from exc


def create_storage(config: BackupConfig) -> StorageProvider:
    if config.storage_type == "local":
        return LocalStorageProvider(config.storage_local_path)
    return S3StorageProvider(config)


# Compatibility aliases for existing imports.
S3Storage = S3StorageProvider
R2Storage = S3StorageProvider
LocalStorage = LocalStorageProvider
