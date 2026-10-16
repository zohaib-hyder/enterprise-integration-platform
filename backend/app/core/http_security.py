"""Bound incoming bodies before JSON decoding, including direct-backend traffic."""

import hashlib
import threading
import time

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.webhook_security import MAX_BODY


class RequestBodyLimit:
    def __init__(self, app: ASGIApp, max_bytes: int = 16384, webhook_limit: int = 120):
        self.app, self.max_bytes = app, max_bytes
        self.webhook_limit = webhook_limit
        self.buckets: dict[str, tuple[int, int]] = {}
        self.lock = threading.Lock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return
        # Preserve ingress's existing typed rejection receipts and streaming limit.
        if scope["path"].startswith("/api/v1/webhooks/ingest/"):
            window = int(time.monotonic()) // 60
            peer = hashlib.sha256(str(scope.get("client", ("unknown",))[0]).encode()).hexdigest()
            with self.lock:
                self.buckets = {k: v for k, v in self.buckets.items() if v[0] == window}
                previous = self.buckets.get(peer, (window, 0))[1]
                blocked = previous >= self.webhook_limit or (
                    peer not in self.buckets and len(self.buckets) >= 4096
                )
                if not blocked:
                    self.buckets[peer] = (window, previous + 1)
            if blocked:
                await JSONResponse(
                    {"detail": "Webhook ingress request limit reached"},
                    429,
                    headers={"Retry-After": "60"},
                )(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return
        limit = MAX_BODY if scope["path"].startswith("/api/v1/webhooks/ingest/") else self.max_bytes
        headers = dict(scope["headers"])
        encoding = headers.get(b"content-encoding", b"identity")
        length = headers.get(b"content-length")
        if encoding != b"identity":
            await JSONResponse({"detail": "Content encoding unsupported"}, 415)(
                scope, receive, send
            )
            return
        if length is not None and (not length.isdigit() or len(length) > 10 or int(length) > limit):
            await JSONResponse({"detail": "Request too large"}, 413)(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > limit:
                await JSONResponse({"detail": "Request too large"}, 413)(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)
