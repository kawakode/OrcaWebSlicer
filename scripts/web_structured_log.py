"""Structured log records shared by the web API, the executor, and worker events.

A record is an event name plus typed fields, emitted through the standard
`logging` module so any handler can take it. `JsonFormatter` writes one JSON
object per line; `TextFormatter` writes the same record as `event key=value`
for a terminal. Fields are the only payload: callers pass identifiers, codes,
counts, and durations, never a filename, a request body, model content, or an
identity claim.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Iterable


FORMAT_JSON = "json"
FORMAT_TEXT = "text"
FORMATS = (FORMAT_JSON, FORMAT_TEXT)
# The loggers one API process owns: its own, and Uvicorn's, which it reformats
# so a deployment's log stream has a single shape.
OWNED_LOGGERS = ("orca.web", "uvicorn", "uvicorn.error", "uvicorn.access")


def log_event(logger: logging.Logger, level: int, event: str, /, **fields: Any) -> None:
    """Emit one structured record. `None` fields are dropped."""
    if logger.isEnabledFor(level):
        logger.log(level, event, extra={"fields": {key: value for key, value in fields.items() if value is not None}})


def record_fields(record: logging.LogRecord) -> Dict[str, Any]:
    fields = getattr(record, "fields", None)
    return dict(fields) if isinstance(fields, dict) else {}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        document: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        # A field never overwrites the envelope, so a caller cannot forge one.
        for key, value in record_fields(record).items():
            document.setdefault(key, value)
        if record.exc_info:
            document["exception"] = self.formatException(record.exc_info)
        return json.dumps(document, default=str, ensure_ascii=False, separators=(",", ":"))


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = " ".join(f"{key}={value}" for key, value in record_fields(record).items())
        line = f"{record.levelname} {record.name} {record.getMessage()}" + (f" {fields}" if fields else "")
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure(log_format: str, level: int = logging.INFO, loggers: Iterable[str] = OWNED_LOGGERS) -> None:
    """Give each named logger exactly one stderr handler in `log_format`.

    Idempotent, so a process that builds several apps (tests do) never stacks
    handlers. The loggers stop propagating so the root logger, which the
    process may not own, never prints the same record a second time.
    """
    if log_format not in FORMATS:
        raise ValueError(f"log format must be one of {', '.join(FORMATS)}")
    formatter = JsonFormatter() if log_format == FORMAT_JSON else TextFormatter()
    for name in loggers:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = False
