"""Accept and hold uploaded models without ever parsing their geometry."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, Tuple

from .errors import ApiError
from .quotas import QuotaLimits, storage_quota_error


# The worker rejects anything else before it invokes an importer; the API
# rejects it before the bytes are ever stored.
SUPPORTED_FORMATS = {".stl": "stl", ".obj": "obj", ".3mf": "3mf"}
UPLOAD_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
MAX_FILENAME_CHARS = 200
_UNSAFE_FILENAME_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_METADATA_NAME = "upload.json"
_METADATA_VERSION = 2
_OWNER_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclasses.dataclass(frozen=True)
class UploadRecord:
    upload_id: str
    filename: str
    model_format: str
    size_bytes: int
    sha256: str
    path: Path
    created_at: float
    # Never rendered by `describe()`: ownership is an authorization fact, not
    # something a client needs to see about its own upload.
    owner_id: str

    def describe(self) -> Dict[str, Any]:
        return {
            "upload_id": self.upload_id,
            "filename": self.filename,
            "format": self.model_format,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "created_at": self.created_at,
        }

    def _metadata(self) -> Dict[str, Any]:
        """The on-disk record, which does carry `owner_id`, unlike `describe()`."""
        payload = self.describe()
        payload["metadata_version"] = _METADATA_VERSION
        payload["owner_id"] = self.owner_id
        return payload


def _display_filename(filename: Optional[str]) -> str:
    """Reduce a client filename to something safe to echo back.

    It is never used to build a path: the stored file is always `source.<ext>`.
    """
    candidate = os.path.basename((filename or "").replace("\\", "/")).strip()
    candidate = _UNSAFE_FILENAME_CHARS.sub("", candidate)
    return candidate[:MAX_FILENAME_CHARS] or "model"


def _model_format(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    model_format = SUPPORTED_FORMATS.get(suffix)
    if model_format is None:
        supported = ", ".join(sorted(SUPPORTED_FORMATS))
        raise ApiError("unsupported_model_format", f"Only {supported} models are supported.", 415)
    return model_format


class UploadStore:
    """One directory per upload, holding the model and its metadata.

    `legacy_owner_id`, when set, is the owner a pre-auth upload (one whose
    stored metadata has no `owner_id`) is treated as belonging to. It is the
    fixed local-development principal in disabled mode and `None` (never
    matching a real caller) in required mode, so a legacy upload is exposed
    only in the one topology that documents the transition.
    """

    def __init__(self, root: Path, max_bytes: int, legacy_owner_id: Optional[str] = None) -> None:
        self._root = Path(root)
        self._max_bytes = max_bytes
        self._legacy_owner_id = legacy_owner_id

    def create(
        self,
        filename: Optional[str],
        reader: BinaryIO,
        owner_id: str,
        quota: Optional[Tuple[int, QuotaLimits]] = None,
    ) -> UploadRecord:
        """Store one model. `quota`, when given, is the (bytes, limits) this
        upload may still consume of its owner's storage budget; exceeding it
        is a quota failure rather than the service-wide size limit."""
        display = _display_filename(filename)
        model_format = _model_format(display)
        upload_id = uuid.uuid4().hex
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            directory = self._root / upload_id
            directory.mkdir(mode=0o700, exist_ok=False)
        except OSError as error:
            raise ApiError("upload_root_unavailable", "The upload could not be stored.", 500) from error

        target = directory / f"source.{model_format}"
        try:
            size_bytes, digest = self._store(reader, target, quota)
            record = UploadRecord(
                upload_id=upload_id,
                filename=display,
                model_format=model_format,
                size_bytes=size_bytes,
                sha256=digest,
                path=target,
                created_at=time.time(),
                owner_id=owner_id,
            )
            # Metadata lands last, so a directory holding it is always complete.
            self._write_metadata(directory, record)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return record

    def get(self, upload_id: str, owner_id: str) -> UploadRecord:
        if not isinstance(upload_id, str) or not UPLOAD_ID_PATTERN.match(upload_id):
            raise ApiError("unknown_upload", "No upload is stored under that id.", 404)
        directory = self._root / upload_id
        try:
            metadata = json.loads((directory / _METADATA_NAME).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ApiError("unknown_upload", "No upload is stored under that id.", 404) from error
        model_format = metadata.get("format")
        if model_format not in set(SUPPORTED_FORMATS.values()):
            raise ApiError("unknown_upload", "No upload is stored under that id.", 404)
        path = directory / f"source.{model_format}"
        if not path.is_file() or path.is_symlink():
            raise ApiError("unknown_upload", "No upload is stored under that id.", 404)
        if self._stored_owner(metadata) != owner_id:
            raise ApiError("unknown_upload", "No upload is stored under that id.", 404)
        return UploadRecord(
            upload_id=upload_id,
            filename=str(metadata.get("filename", "model")),
            model_format=model_format,
            size_bytes=int(metadata.get("size_bytes", 0)),
            sha256=str(metadata.get("sha256", "")),
            path=path,
            created_at=float(metadata.get("created_at", 0.0)),
            owner_id=owner_id,
        )

    def owner_bytes(self, owner_id: str) -> int:
        """Bytes held by one owner's complete uploads, read from their metadata.

        Measured from disk rather than remembered, so a restart never forgets
        what an owner still holds. A directory without readable metadata is
        either mid-upload (and reserved by the caller) or reclaimed by `sweep`.
        """
        total = 0
        try:
            entries = list(self._root.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            return 0
        except OSError as error:
            raise ApiError("upload_root_unavailable", "The upload root could not be listed.", 500) from error
        for entry in entries:
            try:
                metadata = json.loads((entry / _METADATA_NAME).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(metadata, dict) and self._stored_owner(metadata) == owner_id:
                size = metadata.get("size_bytes", 0)
                total += size if isinstance(size, int) and size > 0 else 0
        return total

    def sweep(self, retention_seconds: int, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Reclaim uploads older than the retention window.

        Age is measured from the `created_at` an upload's metadata records, or
        from the directory's last modification when it has none (an upload
        still streaming, or one a crash abandoned). Returns one entry per
        removal attempt, naming its id, owner, size, and outcome, for the
        deletion audit.
        """
        deadline = (time.time() if now is None else now) - retention_seconds
        swept: List[Dict[str, Any]] = []
        try:
            entries = sorted(self._root.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            return swept
        except OSError as error:
            raise ApiError("upload_root_unavailable", "The upload root could not be listed.", 500) from error
        for entry in entries:
            outcome: Dict[str, Any] = {"upload_id": entry.name, "owner_id": None, "bytes": 0, "created_at": None}
            try:
                if entry.is_symlink() or not entry.is_dir():
                    entry.unlink()
                    swept.append(dict(outcome, removed=True))
                    continue
                try:
                    metadata = json.loads((entry / _METADATA_NAME).read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    metadata = None
                if isinstance(metadata, dict) and isinstance(metadata.get("created_at"), (int, float)):
                    created_at = float(metadata["created_at"])
                    size = metadata.get("size_bytes", 0)
                    outcome.update(
                        owner_id=self._stored_owner(metadata),
                        bytes=size if isinstance(size, int) and size > 0 else 0,
                        created_at=created_at,
                    )
                else:
                    created_at = entry.stat().st_mtime
                if created_at > deadline:
                    continue
            except OSError:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            swept.append(dict(outcome, removed=not entry.exists()))
        return swept

    def _stored_owner(self, metadata: Dict[str, Any]) -> Optional[str]:
        """The owner a stored record belongs to, or `None` when it has none.

        Version 1 predates ownership. It resolves to the legacy owner this
        deployment maps ownerless uploads to (or to nothing in required mode).
        Version 2 must carry the opaque 64-character owner digest. Any other
        version fails closed.
        """
        metadata_version = metadata.get("metadata_version", 1)
        if metadata_version == 1:
            return self._legacy_owner_id
        if metadata_version != _METADATA_VERSION:
            return None
        stored_owner = metadata.get("owner_id")
        if not isinstance(stored_owner, str) or not _OWNER_ID_PATTERN.fullmatch(stored_owner):
            return None
        return stored_owner

    def _store(
        self, reader: BinaryIO, target: Path, quota: Optional[Tuple[int, QuotaLimits]] = None
    ) -> Tuple[int, str]:
        digest = hashlib.sha256()
        written = 0
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with open(descriptor, "wb") as writer:
            while True:
                chunk = reader.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > self._max_bytes:
                    raise ApiError(
                        "upload_size_limit_exceeded",
                        "The uploaded model exceeds the configured size limit.",
                        413,
                    )
                if quota is not None and written > quota[0]:
                    raise storage_quota_error(quota[1])
                digest.update(chunk)
                writer.write(chunk)
        if written == 0:
            raise ApiError("empty_upload", "The uploaded model is empty.", 400)
        return written, digest.hexdigest()

    def _write_metadata(self, directory: Path, record: UploadRecord) -> None:
        payload = json.dumps(record._metadata(), separators=(",", ":")).encode("utf-8")
        temporary = directory / f".{_METADATA_NAME}.partial"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with open(descriptor, "wb") as writer:
            writer.write(payload)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, directory / _METADATA_NAME)
