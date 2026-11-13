"""Acceso al PDF original (Plan V3.0 §5.1).

Decisión del propietario (despliegue AKS): el PDF NO se mueve ni se copia a un almacenamiento propio.
Se procesa en memoria. Según cómo llegó, la clave (`storage_key`) indica dónde leerlo:

  documents/<sha256>.pdf   disco local (solo desarrollo y docker-compose; STORAGE_BACKEND=local)
  memory/<sha256>          copia temporal en Redis mientras el worker lo procesa (STORAGE_BACKEND=memory);
                           al vencer (UPLOAD_TTL_SECONDS) el original deja de estar disponible: el documento
                           conserva sus noticias, pero el visor ya no tiene la imagen de la página.
  sftp:<ruta remota>       el original sigue en el file server; se relee por SFTP cada vez que hace falta.

Toda lectura verifica el SHA-256 contra el registrado: si el archivo del origen cambió, no se usa.
La clave interna nunca usa el nombre que da el usuario (sin path traversal).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

from app.core.config import get_settings

log = logging.getLogger("storage")

_SHA_RE = re.compile(r"^[a-f0-9]{64}$")
_LOCAL_RE = re.compile(r"documents/([a-f0-9]{64})\.pdf")
_MEMORY_RE = re.compile(r"memory/([a-f0-9]{64})")


class LocalStorage:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        (self.root / "documents").mkdir(parents=True, exist_ok=True)

    def put(self, sha256: str, data: bytes) -> str:
        key = f"documents/{sha256}.pdf"
        path = self.root / key
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return key

    def get(self, sha256: str) -> bytes | None:
        path = self.root / "documents" / f"{sha256}.pdf"
        return path.read_bytes() if path.exists() else None


class MemoryStorage:
    """Copia temporal en Redis (memoria compartida entre la API y el worker). Nada se escribe en disco."""

    def __init__(self, redis_url: str, ttl_seconds: int):
        if not redis_url:
            raise RuntimeError("STORAGE_BACKEND=memory requiere REDIS_URL")
        import redis
        self.client = redis.Redis.from_url(redis_url)
        self.ttl = ttl_seconds

    def put(self, sha256: str, data: bytes) -> str:
        self.client.set(f"na:pdf:{sha256}", data, ex=self.ttl)
        return f"memory/{sha256}"

    def get(self, sha256: str) -> bytes | None:
        return self.client.get(f"na:pdf:{sha256}")

    def discard(self, sha256: str) -> None:
        self.client.delete(f"na:pdf:{sha256}")


class _LruCache:
    """Caché pequeña en memoria del proceso para no descargar el mismo PDF por cada página del visor."""

    def __init__(self, max_items: int = 6, ttl_seconds: int = 600):
        self.max_items, self.ttl = max_items, ttl_seconds
        self.items: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key: str) -> bytes | None:
        with self.lock:
            hit = self.items.get(key)
            if not hit or time.monotonic() - hit[0] > self.ttl:
                self.items.pop(key, None)
                return None
            self.items.move_to_end(key)
            return hit[1]

    def put(self, key: str, data: bytes) -> None:
        with self.lock:
            self.items[key] = (time.monotonic(), data)
            self.items.move_to_end(key)
            while len(self.items) > self.max_items:
                self.items.popitem(last=False)


_cache = _LruCache()
_uploads: LocalStorage | MemoryStorage | None = None


def _upload_storage() -> LocalStorage | MemoryStorage:
    global _uploads
    if _uploads is None:
        s = get_settings()
        if s.storage_backend == "local":
            _uploads = LocalStorage(s.storage_root)
        elif s.storage_backend == "memory":
            _uploads = MemoryStorage(s.redis_url, s.upload_ttl_seconds)
        else:
            raise RuntimeError(f"STORAGE_BACKEND no soportado: {s.storage_backend} (local | memory)")
    return _uploads


def keep_upload(sha256: str, data: bytes) -> str:
    """Guarda el PDF recibido por la API para que el worker lo procese. Devuelve la clave."""
    if not _SHA_RE.match(sha256):
        raise ValueError("SHA-256 inválido")
    return _upload_storage().put(sha256, data)


def sftp_key(remote_path: str) -> str:
    return f"sftp:{remote_path}"


def read_original(key: str, sha256: str) -> bytes | None:
    """Bytes del original, o None si ya no está disponible (vencido, movido, borrado o modificado)."""
    if not key or not _SHA_RE.match(sha256 or ""):
        return None
    cached = _cache.get(sha256)
    if cached is not None:
        return cached

    data: bytes | None
    if _LOCAL_RE.fullmatch(key):
        data = _upload_storage().get(sha256) if get_settings().storage_backend == "local" else LocalStorage(get_settings().storage_root).get(sha256)
    elif _MEMORY_RE.fullmatch(key):
        data = _upload_storage().get(sha256) if get_settings().storage_backend == "memory" else None
    elif key.startswith("sftp:"):
        from app.sftp import read_file
        try:
            data = read_file(key[len("sftp:"):])
        except Exception:
            log.warning("no se pudo leer %s del SFTP", key, exc_info=True)
            data = None
    else:
        raise ValueError("Clave de almacenamiento inválida")

    if data is None:
        return None
    if hashlib.sha256(data).hexdigest() != sha256:
        log.warning("el original de %s cambió en el origen: no se usa", key)
        return None
    _cache.put(sha256, data)
    return data


def release_upload(key: str, sha256: str) -> None:
    """Tras procesar: la copia temporal en memoria se descarta (el original no se conserva)."""
    if _MEMORY_RE.fullmatch(key or "") and get_settings().storage_backend == "memory":
        try:
            _upload_storage().discard(sha256)  # type: ignore[union-attr]
        except Exception:
            log.warning("no se pudo descartar la copia temporal %s", key, exc_info=True)


def describe_source(key: str) -> str:
    """Origen del original para la interfaz: local | upload | sftp."""
    if (key or "").startswith("sftp:"):
        return "sftp"
    if _MEMORY_RE.fullmatch(key or ""):
        return "upload"
    return "local"
