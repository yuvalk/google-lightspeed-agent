"""Request body size limit middleware (CWE-400 mitigation)."""

import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

# HTTP methods that may carry a request body.
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


class RequestBodyLimitMiddleware:
    """Pure ASGI middleware that rejects requests exceeding a body size limit.

    Uses a two-layer approach:
    1. Content-Length header check for immediate rejection (covers standard clients).
    2. Streaming byte counter that signals a disconnect when a chunked body
       (no Content-Length) exceeds the limit.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int = 10 * 1024 * 1024) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        if method not in _BODY_METHODS:
            await self.app(scope, receive, send)
            return

        # Layer 1: Fast-reject via Content-Length header
        for header_name, header_value in scope.get("headers", []):
            if header_name == b"content-length":
                try:
                    if int(header_value) > self.max_bytes:
                        logger.warning(
                            "Rejected request: Content-Length %s exceeds %d byte limit",
                            header_value.decode(),
                            self.max_bytes,
                        )
                        response = JSONResponse(
                            status_code=413,
                            content={
                                "error": "request_too_large",
                                "message": (
                                    f"Request body exceeds "
                                    f"the {self.max_bytes} byte limit"
                                ),
                            },
                        )
                        await response(scope, receive, send)
                        return
                except ValueError:
                    pass
                break

        # Layer 2: Streaming byte counter for bodies without Content-Length
        # (e.g. chunked transfer encoding).  When the limit is exceeded we
        # return an ``http.disconnect`` message which causes Starlette to
        # treat the connection as closed by the client and abort processing.
        bytes_received = 0

        async def limited_receive() -> Message:
            nonlocal bytes_received
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                bytes_received += len(body)
                if bytes_received > self.max_bytes:
                    logger.warning(
                        "Rejected request: streamed body (%d bytes) "
                        "exceeds %d byte limit",
                        bytes_received,
                        self.max_bytes,
                    )
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, limited_receive, send)
