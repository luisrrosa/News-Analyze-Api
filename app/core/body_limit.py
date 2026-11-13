"""Límite de tamaño del cuerpo de las peticiones (middleware ASGI).

Se rechaza antes de leer: por Content-Length declarado y, si el cuerpo llega por partes, contando los bytes
recibidos. La carga de PDF tiene su propio límite (DOCUMENTS_MAX_SIZE_MB + margen del multipart); el resto
de la API acepta como máximo 1 MB (los formularios y JSON del sistema son pequeños).
"""
import json

from app.core.config import get_settings

UPLOAD_PATH = "/api/v1/documents/upload"
DEFAULT_LIMIT = 1 * 1024 * 1024
MULTIPART_MARGIN = 1 * 1024 * 1024


class BodyTooLarge(Exception):
    pass


def limit_for(path: str) -> int:
    if path == UPLOAD_PATH:
        return get_settings().documents_max_size_mb * 1024 * 1024 + MULTIPART_MARGIN
    return DEFAULT_LIMIT


async def _reject(send, limit: int) -> None:
    body = json.dumps({"error": f"La solicitud supera el tamaño máximo permitido ({limit // (1024 * 1024)} MB).",
                       "code": "PAYLOAD_TOO_LARGE"}).encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class BodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)
        limit = limit_for(scope.get("path", ""))
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    if int(value) > limit:
                        return await _reject(send, limit)
                except ValueError:
                    return await _reject(send, limit)

        received = 0
        started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLarge()
            return message

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except BodyTooLarge:
            if not started:
                await _reject(send, limit)
