"""Abuse controls: request-body caps and a per-owner request rate.

Both run in one ASGI middleware, ahead of every protected route, because the
framework reads and spools a request body before it resolves any dependency.
Checked in a dependency, an unauthenticated caller could make the API store a
full-size upload before being refused. Here a request is sized by its declared
length, authenticated, and charged against its owner's rate before one body
byte is read, and a body that streams past its cap is cut off.

A refusal from this guard happens before any route runs, so it has no side
effect: a client may always resend a request refused with
`request_rate_limited`. Job outcomes are untouched: a job that is already
queued or running keeps its own state and stable error codes however often
its status reads are refused.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import threading
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Collection, Dict, Tuple

from starlette.requests import Request

from .errors import ApiError, error_response
from .quotas import positive_ints_from_environment, validate_positive_ints


RATE_LIMIT_STATUS = 429
# Room for the multipart framing and the filename field around an upload's
# file part. The file part itself is still held to `max_input_bytes` by the
# upload store, with its own `upload_size_limit_exceeded` code.
MULTIPART_OVERHEAD_BYTES = 1024 * 1024

logger = logging.getLogger("orca.web.api")


@dataclasses.dataclass(frozen=True)
class RateLimits:
    # A token bucket per owner: `request_burst` requests at once, refilled at
    # `requests_per_minute`. The browser polls a running job about three times
    # a second and fetches scene objects and preview layers one request each,
    # so the defaults leave an interactive session well inside the budget.
    requests_per_minute: int = 600
    request_burst: int = 300
    # Every non-upload request body is JSON; the largest one the schema admits
    # (256 settings of 4096 characters each) is about 1 MiB.
    max_json_body_bytes: int = 4 * 1024 * 1024
    # Bounds the limiter's memory. The least recently seen owner is forgotten
    # first, and an owner idle long enough to refill is forgotten for free.
    max_tracked_owners: int = 100_000

    def validate(self) -> None:
        validate_positive_ints(self, "rate limit")


_ENVIRONMENT = {
    "requests_per_minute": "ORCA_WEB_RATE_REQUESTS_PER_MINUTE",
    "request_burst": "ORCA_WEB_RATE_REQUEST_BURST",
    "max_json_body_bytes": "ORCA_WEB_MAX_JSON_BODY_BYTES",
    "max_tracked_owners": "ORCA_WEB_RATE_TRACKED_OWNERS",
}


def limits_from_environment() -> RateLimits:
    return positive_ints_from_environment(RateLimits, _ENVIRONMENT)


class RateLimiter:
    """Per-key token buckets with a bounded key table."""

    def __init__(self, limits: RateLimits, clock: Callable[[], float] = time.monotonic) -> None:
        limits.validate()
        self._rate = limits.requests_per_minute / 60.0
        self._burst = float(limits.request_burst)
        self._max_keys = limits.max_tracked_owners
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: "OrderedDict[str, Tuple[float, float]]" = OrderedDict()

    def acquire(self, key: str) -> float:
        """Spend one token. Returns 0 when admitted, else the seconds to wait."""
        with self._lock:
            now = self._clock()
            tokens, updated = self._buckets.pop(key, (self._burst, now))
            tokens = min(self._burst, tokens + (now - updated) * self._rate)
            wait = 0.0
            if tokens >= 1.0:
                tokens -= 1.0
            else:
                wait = (1.0 - tokens) / self._rate
            self._buckets[key] = (tokens, now)
            while len(self._buckets) > self._max_keys:
                self._buckets.popitem(last=False)
            return wait


def rate_limited_error(wait: float) -> ApiError:
    error = ApiError("request_rate_limited", "Too many requests; retry after the indicated delay.", RATE_LIMIT_STATUS)
    error.retry_after = max(1, math.ceil(wait))
    return error


def body_too_large_error(limit: int) -> ApiError:
    return ApiError("request_body_too_large", f"The request body exceeds {limit} bytes.", 413)


class _BodyTooLarge(Exception):
    pass


ASGIApp = Callable[..., Awaitable[None]]


class AbuseGuard:
    """Size, authenticate, and rate-limit every protected request before its body.

    The principal it authenticates is left on the request state, where
    `get_principal` reads it; a protected route this guard did not see fails
    closed there rather than running unauthenticated.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        prefix: str,
        public_paths: Collection[str],
        upload_path: str,
        max_upload_body_bytes: int,
        max_json_body_bytes: int,
        authenticate: Callable[[Any], Any],
        limiter: RateLimiter,
    ) -> None:
        self.app = app
        self._prefix = prefix
        self._public_paths = frozenset(public_paths)
        self._upload_path = upload_path
        self._max_upload_body_bytes = max_upload_body_bytes
        self._max_json_body_bytes = max_json_body_bytes
        self._authenticate = authenticate
        self._limiter = limiter

    def _protects(self, path: str) -> bool:
        if path in self._public_paths:
            return False
        return path == self._prefix or path.startswith(self._prefix + "/")

    async def __call__(self, scope: Dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope["type"] != "http" or not self._protects(scope["path"]):
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        is_upload = scope["method"] == "POST" and scope["path"] == self._upload_path
        limit = self._max_upload_body_bytes if is_upload else self._max_json_body_bytes
        request = Request(scope)
        try:
            declared = request.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > limit:
                raise body_too_large_error(limit)
            principal = self._authenticate(request)
            wait = self._limiter.acquire(principal.owner_id)
            if wait > 0:
                raise rate_limited_error(wait)
        except ApiError as error:
            logger.info("request refused code=%s correlation_id=%s", error.code, state.get("correlation_id", ""))
            await error_response(error, state.get("correlation_id", ""))(scope, receive, send)
            return
        state["principal"] = principal

        # A chunked body declares no length, so it is counted as it streams.
        # Raising here reaches the framework as a body-parsing failure, whose
        # response is swapped for the stable one below.
        received = 0
        exceeded = False

        async def counted_receive() -> Dict[str, Any]:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise _BodyTooLarge()
            return message

        started = False

        async def refuse_oversized() -> None:
            nonlocal started
            started = True
            await error_response(body_too_large_error(limit), state.get("correlation_id", ""))(scope, receive, send)

        async def guarded_send(message: Dict[str, Any]) -> None:
            nonlocal started
            if not exceeded:
                started = True
                await send(message)
            elif message["type"] == "http.response.start":
                await refuse_oversized()

        try:
            await self.app(scope, counted_receive, guarded_send)
        except _BodyTooLarge:
            # Only reached when no framework layer converted the failure.
            if not started:
                await refuse_oversized()
