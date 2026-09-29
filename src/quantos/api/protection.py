"""Single-process request protection for the anonymous Public Preview."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from enum import Enum
import time
from typing import Callable
from uuid import uuid4

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class DeploymentMode(str, Enum):
    """HTTP exposure boundary, independent from the product data mode."""

    LOCAL = "local"
    PUBLIC_PREVIEW = "public_preview"


@dataclass(frozen=True, slots=True)
class PublicPreviewPolicy:
    """Bounded defaults for one process and one anonymous Demo replica."""

    max_body_bytes: int = 64 * 1024
    max_concurrency: int = 8
    request_timeout_seconds: float = 15.0
    ask_rate_limit: int = 12
    ask_rate_window_seconds: float = 60.0
    trace_max_records: int = 200
    trace_ttl_seconds: float = 60 * 60

    def __post_init__(self) -> None:
        values = (
            self.max_body_bytes, self.max_concurrency, self.request_timeout_seconds,
            self.ask_rate_limit, self.ask_rate_window_seconds,
            self.trace_max_records, self.trace_ttl_seconds,
        )
        if any(isinstance(value, bool) or value <= 0 for value in values):
            raise ValueError("public preview limits must be positive")


class PublicPreviewGuard:
    """Deterministic in-memory limiter for a single preview process."""

    def __init__(
        self, policy: PublicPreviewPolicy,
        *, monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.policy = policy
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._active = 0
        self._ask_requests: deque[float] = deque()

    async def allow_ask(self) -> bool:
        now = self._monotonic()
        cutoff = now - self.policy.ask_rate_window_seconds
        async with self._lock:
            while self._ask_requests and self._ask_requests[0] <= cutoff:
                self._ask_requests.popleft()
            if len(self._ask_requests) >= self.policy.ask_rate_limit:
                return False
            self._ask_requests.append(now)
            return True

    async def acquire(self) -> bool:
        async with self._lock:
            if self._active >= self.policy.max_concurrency:
                return False
            self._active += 1
            return True

    async def release(self) -> None:
        async with self._lock:
            self._active = max(0, self._active - 1)


class PublicPreviewTimeoutMiddleware:
    """ASGI-level body/deadline boundary around the complete request task."""

    def __init__(
        self, app: ASGIApp, *, timeout_seconds: float, max_body_bytes: int,
        on_rejection: Callable[[Scope, int, float, str], None] | None = None,
    ) -> None:
        self.app = app
        self.timeout_seconds = timeout_seconds
        self.max_body_bytes = max_body_bytes
        self.on_rejection = on_rejection

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("path") == "/v1/health"
            or not str(scope.get("path", "")).startswith("/v1/")
        ):
            await self.app(scope, receive, send)
            return
        started = False
        received = 0
        started_at = time.monotonic()

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise RequestBodyTooLarge
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await asyncio.wait_for(
                self.app(scope, limited_receive, tracked_send),
                timeout=self.timeout_seconds,
            )
        except RequestBodyTooLarge:
            if not started:
                await self._error(
                    scope, receive, send, 413, "REQUEST_BODY_TOO_LARGE",
                    "Request body exceeds the public preview limit.",
                    started_at,
                )
        except asyncio.TimeoutError:
            if started:
                return
            await self._error(
                scope, receive, send, 504, "REQUEST_TIMEOUT",
                "Public preview request timed out.",
                started_at,
            )

    async def _error(
        self, scope: Scope, receive: Receive, send: Send, status_code: int,
        code: str, message: str, started_at: float,
    ) -> None:
        state = scope.get("state", {})
        request_id = state.get("correlation_id", uuid4().hex)
        if self.on_rejection is not None:
            self.on_rejection(
                scope, status_code, (time.monotonic() - started_at) * 1000,
                request_id,
            )
        response = JSONResponse(status_code=status_code, content={
            "code": code,
            "message": message,
            "request_id": request_id,
            "details": [],
        }, headers={
            "X-Correlation-ID": request_id,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        })
        await response(scope, receive, send)


class RequestBodyTooLarge(Exception):
    pass
