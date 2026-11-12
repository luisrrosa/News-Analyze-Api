"""Almacenamiento del PDF original (Plan V3.0 §5.1). Interfaz común; hoy disco local, Azure Blob después.

La clave interna es el SHA-256 del contenido: no usa el nombre del usuario (sin path traversal)
y el original es inmutable (no se sobrescribe).
"""
import os
import re
from pathlib import Path
from typing import Protocol

from app.core.config import get_settings

_SHA_RE = re.compile(r"^[a-f0-9]{64}$")


class DocumentStorage(Protocol):
    def put(self, sha256: str, data: bytes) -> str: ...
    def get(self, key: str) -> bytes | None: ...


class LocalStorage:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        (self.root / "documents").mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        m = re.fullmatch(r"documents/([a-f0-9]{64})\.pdf", key)
        if not m:
            raise ValueError("Clave de almacenamiento inválida")
        return self.root / "documents" / f"{m.group(1)}.pdf"

    def put(self, sha256: str, data: bytes) -> str:
        if not _SHA_RE.match(sha256):
            raise ValueError("SHA-256 inválido")
        key = f"documents/{sha256}.pdf"
        path = self._path(key)
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return key

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.exists() else None


_storage: DocumentStorage | None = None


def get_storage() -> DocumentStorage:
    global _storage
    if _storage is None:
        s = get_settings()
        if s.storage_backend != "local":
            raise RuntimeError("Solo está implementado el almacenamiento local (Azure Blob pendiente de cuenta).")
        _storage = LocalStorage(s.storage_root)
    return _storage
