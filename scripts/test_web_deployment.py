#!/usr/bin/env python3
"""Check a running docker/web/compose.prod.yml stack from outside, as a browser would.

Run it after `docker compose -f docker/web/compose.prod.yml up -d --wait` with
ORCA_WEB_DOMAIN=localhost, where Caddy signs the site with its own local CA.
The CA's root is copied out of the proxy so TLS is verified, not skipped. It
checks the HTTPS redirect, the login, the security headers, a slice from upload
to G-code, and the API container's isolation (user, read-only root, no route
out). Only the standard library is used, so it runs on any CI host.
"""

from __future__ import annotations

import argparse
import base64
import json
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

MACHINE = "Anycubic/machine/Anycubic Kobra 0.4 nozzle"
PROCESS = "Anycubic/process/0.20mm Standard @Anycubic Kobra"
FILAMENT = "Anycubic/filament/Anycubic Generic PLA"
TERMINAL = {"succeeded", "failed", "canceled"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class Site:
    def __init__(self, base: str, context: ssl.SSLContext, user: str, password: str) -> None:
        self.base = base
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.auth = {"Authorization": f"Basic {token}"}
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), NoRedirect())

    def request(
        self, path: str, data: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None, auth: bool = True
    ) -> Tuple[int, Dict[str, str], bytes]:
        merged = dict(self.auth if auth else {})
        merged.update(headers or {})
        request = urllib.request.Request(self.base + path, data=data, headers=merged)
        try:
            with self.opener.open(request, timeout=60) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def json(self, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = None if body is None else json.dumps(body).encode()
        status, _, payload = self.request(path, data, {"Content-Type": "application/json"} if data else None)
        if status >= 300:
            raise AssertionError(f"{path} answered {status}: {payload[:500]!r}")
        return json.loads(payload)

    def upload(self, model: Path) -> Dict[str, Any]:
        boundary = uuid.uuid4().hex
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{model.name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + model.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        status, _, payload = self.request(
            "/api/v1/uploads", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        if status != 201:
            raise AssertionError(f"upload answered {status}: {payload[:500]!r}")
        return json.loads(payload)

    def wait(self, job_id: str, timeout: float) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.json(f"/api/v1/jobs/{job_id}")
            if job["state"] in TERMINAL:
                if job["state"] != "succeeded":
                    raise AssertionError(f"job {job_id} ended {job['state']}: {json.dumps(job)[:1000]}")
                return job
            time.sleep(1)
        raise AssertionError(f"job {job_id} did not finish within {timeout} s")


def compose(compose_file: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "-f", str(compose_file), *arguments], capture_output=True, text=True, check=check
    )


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"ok - {message}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-file", type=Path, default=Path("docker/web/compose.prod.yml"))
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--model", type=Path, default=Path("tests/data/20mm_cube.obj"))
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as scratch:
        root_ca = Path(scratch) / "root.crt"
        compose(args.compose_file, "cp", "proxy:/data/caddy/pki/authorities/local/root.crt", str(root_ca))
        context = ssl.create_default_context(cafile=str(root_ca))
    site = Site(f"https://{args.host}", context, args.user, args.password)

    plain = urllib.request.build_opener(NoRedirect())
    try:
        plain.open(f"http://{args.host}/", timeout=30)
        redirect: Tuple[int, str] = (200, "")
    except urllib.error.HTTPError as error:
        redirect = (error.code, error.headers.get("Location", ""))
    check(redirect[0] in (301, 308) and redirect[1].startswith("https://"), f"HTTP redirects to HTTPS {redirect}")

    status, _, _ = site.request("/api/v1/health", auth=False)
    check(status == 401, f"an anonymous request is refused ({status})")

    status, headers, page = site.request("/")
    check(status == 200 and b"<div id=\"root\"" in page, "the browser screen is served over verified TLS")
    check("max-age" in headers.get("Strict-Transport-Security", ""), "HSTS is set")
    check(headers.get("X-Content-Type-Options") == "nosniff", "nosniff is set")
    check("Server" not in headers or "uvicorn" not in headers["Server"].lower(), "no server banner from the API")

    health = site.json("/api/v1/health")
    check(health.get("status") == "ok", f"the API is healthy {health}")

    upload = site.upload(args.model)
    site.wait(upload["inspection"]["job_id"], args.timeout)
    check(True, f"upload {upload['upload_id']} was inspected")
    job = site.json(
        "/api/v1/jobs",
        {
            "upload_id": upload["upload_id"],
            "machine_profile": MACHINE,
            "process_profile": PROCESS,
            "filament_profile": FILAMENT,
            "thumbnails": True,
        },
    )
    site.wait(job["job_id"], args.timeout)
    status, _, gcode = site.request(f"/api/v1/jobs/{job['job_id']}/artifacts/gcode")
    check(status == 200 and b"\nG1 " in gcode, f"the slice produced {len(gcode)} bytes of G-code")

    identity = compose(args.compose_file, "exec", "-T", "api", "id", "-u").stdout.strip()
    check(identity == "10001", f"the API and its workers run as uid {identity}")
    writable = compose(args.compose_file, "exec", "-T", "api", "touch", "/opt/orca-web/probe", check=False)
    check(writable.returncode != 0, "the root filesystem is read-only")
    outbound = compose(
        args.compose_file,
        "exec",
        "-T",
        "api",
        "python3",
        "-c",
        "import socket; socket.create_connection(('1.1.1.1', 443), timeout=5)",
        check=False,
    )
    check(outbound.returncode != 0, "the API container has no route to the internet")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as error:
        print(f"not ok - {error}", file=sys.stderr)
        sys.exit(1)
