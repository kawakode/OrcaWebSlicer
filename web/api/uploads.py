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


# The worker rejects anything else before it invokes an importer; the API
# rejects it before the bytes are ever stored.
SUPPORTED_FORMATS = {".stl": "stl", ".obj": "obj", ".3mf": "3mf"}
UPLOAD_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
MAX_FILENAME_CHARS = 200
_UNSAFE_FILENAME_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_METADATA_NAME = "upload.json"


@dataclasses.dataclass(frozen=True)
class UploadRecord:
    upload_id: str
    filename: str
    model_format: str
    size_bytes: int
    sha256: str
    path: Path
    created_at: float

    def describe(self) -> Dict[str, Any]:
        return {
            "upload_id": self.upload_id,
            "filename": self.filename,
            "format": self.model_format,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "created_at": self.created_at,
        }


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
    """One directory per upload, holding the model and its metadata."""

    def __init__(self, root: Path, max_bytes: int) -> None:
        self._root = Path(root)
        self._max_bytes = max_bytes

    def create(self, filename: Optional[str], reader: BinaryIO) -> UploadRecord:
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
            size_bytes, digest = self._store(reader, target)
            record = UploadRecord(
                upload_id=upload_id,
                filename=display,
                model_format=model_format,
                size_bytes=size_bytes,
                sha256=digest,
                path=target,
                created_at=time.time(),
            )
            # Metadata lands last, so a directory holding it is always complete.
            self._write_metadata(directory, record)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return record

    def get(self, upload_id: str) -> UploadRecord:
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
        return UploadRecord(
            upload_id=upload_id,
            filename=str(metadata.get("filename", "model")),
            model_format=model_format,
            size_bytes=int(metadata.get("size_bytes", 0)),
            sha256=str(metadata.get("sha256", "")),
            path=path,
            created_at=float(metadata.get("created_at", 0.0)),
        )

    def sweep(self, retention_seconds: int, now: Optional[float] = None) -> List[str]:
        """Reclaim uploads older than the retention window."""
        deadline = (time.time() if now is None else now) - retention_seconds
        removed: List[str] = []
        try:
            entries = sorted(self._root.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            return removed
        except OSError as error:
            raise ApiError("upload_root_unavailable", "The upload root could not be listed.", 500) from error
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_dir():
                    entry.unlink()
                    removed.append(entry.name)
                    continue
                if entry.stat().st_mtime > deadline:
                    continue
                shutil.rmtree(entry)
                removed.append(entry.name)
            except OSError:
                continue
        return removed

    def _store(self, reader: BinaryIO, target: Path) -> Tuple[int, str]:
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
                digest.update(chunk)
                writer.write(chunk)
        if written == 0:
            raise ApiError("empty_upload", "The uploaded model is empty.", 400)
        return written, digest.hexdigest()

    def _write_metadata(self, directory: Path, record: UploadRecord) -> None:
        payload = json.dumps(record.describe(), separators=(",", ":")).encode("utf-8")
        temporary = directory / f".{_METADATA_NAME}.partial"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with open(descriptor, "wb") as writer:
            writer.write(payload)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, directory / _METADATA_NAME)
