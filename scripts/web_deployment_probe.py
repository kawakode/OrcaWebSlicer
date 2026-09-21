#!/usr/bin/env python3
"""Exercise a deployed API from readiness through a validated G-code download."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: Optional[bytes] = None,
    content_type: Optional[str] = None,
    timeout: float = 10,
) -> Tuple[bytes, Dict[str, str]]:
    headers = {"Accept": "application/json", "X-Correlation-Id": f"deployment-probe-{uuid.uuid4().hex}"}
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}", data=body, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"{method} {path} failed: {error.reason}") from error


def _json_request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    value: Optional[Dict[str, Any]] = None,
    timeout: float = 10,
) -> Dict[str, Any]:
    body = json.dumps(value, separators=(",", ":")).encode("utf-8") if value is not None else None
    raw, _headers = _request(
        base_url,
        path,
        method=method,
        body=body,
        content_type="application/json" if body is not None else None,
        timeout=timeout,
    )
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{method} {path} did not return JSON") from error
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{method} {path} did not return a JSON object")
    return parsed


def _upload_body(model: Path) -> Tuple[bytes, str]:
    boundary = f"orca-web-probe-{uuid.uuid4().hex}"
    filename = f"probe{model.suffix.lower()}"
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {media_type}\r\n\r\n"
    ).encode("utf-8")
    return header + model.read_bytes() + f"\r\n--{boundary}--\r\n".encode("ascii"), boundary


def _wait_ready(args: argparse.Namespace) -> Dict[str, Any]:
    deadline = time.monotonic() + args.ready_timeout
    last_error = "readiness did not report ready"
    while True:
        try:
            ready = _json_request(
                args.base_url,
                "/api/v1/health/ready",
                timeout=min(args.request_timeout, max(0.1, deadline - time.monotonic())),
            )
            if ready.get("status") == "ready":
                return ready
            last_error = json.dumps(ready, separators=(",", ":"))
        except RuntimeError as error:
            last_error = str(error)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                f"API did not become ready within {args.ready_timeout:g}s: {last_error}"
            )
        time.sleep(min(0.5, remaining))


def probe(args: argparse.Namespace) -> Dict[str, Any]:
    ready = _wait_ready(args)

    upload_body, boundary = _upload_body(args.model)
    raw_upload, _headers = _request(
        args.base_url,
        "/api/v1/uploads",
        method="POST",
        body=upload_body,
        content_type=f"multipart/form-data; boundary={boundary}",
        timeout=args.request_timeout,
    )
    upload = json.loads(raw_upload.decode("utf-8"))
    if not isinstance(upload, dict):
        raise RuntimeError("upload response was not a JSON object")
    submitted = _json_request(
        args.base_url,
        "/api/v1/jobs",
        method="POST",
        value={
            "upload_id": upload["upload_id"],
            "machine_profile": args.machine,
            "process_profile": args.process,
            "filament_profile": args.filament,
        },
        timeout=args.request_timeout,
    )
    job_id = str(submitted["job_id"])
    deadline = time.monotonic() + args.job_timeout
    while True:
        job = _json_request(
            args.base_url, f"/api/v1/jobs/{job_id}", timeout=args.request_timeout
        )
        if job.get("state") in {"succeeded", "failed", "canceled"}:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(f"job {job_id} did not finish within {args.job_timeout:g}s")
        time.sleep(0.2)
    if job.get("state") != "succeeded":
        raise RuntimeError(f"job {job_id} ended as {job.get('state')}: {job.get('error')}")

    gcode, _headers = _request(
        args.base_url,
        f"/api/v1/jobs/{job_id}/artifacts/gcode",
        timeout=args.request_timeout,
    )
    raw_result, _headers = _request(
        args.base_url,
        f"/api/v1/jobs/{job_id}/artifacts/result",
        timeout=args.request_timeout,
    )
    result = json.loads(raw_result.decode("utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError("downloaded result.json was not a JSON object")
    if result.get("job_id") != job_id or result.get("outcome") != "succeeded":
        raise RuntimeError("downloaded result.json does not describe the successful probe job")
    artifact = next(
        (item for item in job.get("artifacts", []) if item.get("name") == "gcode"), None
    )
    digest = hashlib.sha256(gcode).hexdigest()
    if not artifact or artifact.get("size_bytes") != len(gcode) or artifact.get("sha256") != digest:
        raise RuntimeError("downloaded G-code does not match the API artifact metadata")
    return {
        "status": "ok",
        "api_version": ready.get("api_version"),
        "protocol_version": ready.get("protocol_version"),
        "job_id": job_id,
        "gcode_bytes": len(gcode),
        "gcode_sha256": digest,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--machine", required=True)
    parser.add_argument("--process", required=True)
    parser.add_argument("--filament", required=True)
    parser.add_argument("--request-timeout", type=float, default=10)
    parser.add_argument("--ready-timeout", type=float, default=120)
    parser.add_argument("--job-timeout", type=float, default=300)
    args = parser.parse_args()
    if not args.model.is_file():
        parser.error(f"model does not exist: {args.model}")
    if args.model.suffix.lower() not in {".stl", ".obj", ".3mf"}:
        parser.error("model must be an STL, OBJ, or 3MF file")
    try:
        result = probe(args)
    except (OSError, KeyError, TypeError, ValueError, RuntimeError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, separators=(",", ":")))
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
