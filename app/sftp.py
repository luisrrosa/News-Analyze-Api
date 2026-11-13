"""Cliente SFTP del file server de resúmenes (solo lectura).

El sistema nunca escribe, mueve ni borra en el servidor: lista la carpeta, lee cada PDF en memoria
y lo vuelve a leer cuando el visor lo necesita. Autenticación por contraseña o por llave privada.

Seguridad: con SFTP_HOST_KEY (huella "SHA256:..." de `ssh-keygen -lf` o la llave pública "ssh-ed25519 AAAA...")
se verifica la identidad del servidor. En producción (APP_ENV=production) es obligatoria: sin ella no se conecta.
En desarrollo se acepta la llave y se registra una advertencia.
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import posixpath
import stat
from contextlib import contextmanager
from dataclasses import dataclass

from app.core.config import get_settings

log = logging.getLogger("sftp")


class SftpNotConfigured(RuntimeError):
    pass


@dataclass
class RemoteFile:
    path: str
    name: str
    size: int
    mtime: int


def configured() -> bool:
    s = get_settings()
    return bool(s.sftp_host and s.sftp_user and (s.sftp_password or s.sftp_private_key))


def _fingerprint(key) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


class _HostKeyPolicy:
    def __init__(self, expected: str):
        self.expected = expected.strip()

    def missing_host_key(self, client, hostname, key):  # interfaz de paramiko.MissingHostKeyPolicy
        if not self.expected:
            if get_settings().app_env == "production":
                # En producción no se habla con un servidor no verificado (robo de la contraseña, PDF falsos).
                raise RuntimeError(f"SFTP_HOST_KEY es obligatorio en producción; huella de {hostname}: {_fingerprint(key)}")
            log.warning("SFTP_HOST_KEY no definido: se acepta la llave de %s (%s)", hostname, _fingerprint(key))
            return
        if self.expected.startswith("SHA256:"):
            ok = _fingerprint(key) == self.expected
        else:
            parts = self.expected.split()
            ok = len(parts) >= 2 and parts[0] == key.get_name() and parts[1] == key.get_base64()
        if not ok:
            raise RuntimeError(f"La llave del servidor SFTP {hostname} no coincide con SFTP_HOST_KEY")


def _load_private_key(text: str):
    import paramiko
    pem = text.replace("\\n", "\n").strip() + "\n"
    errors = []
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return cls.from_private_key(io.StringIO(pem))
        except Exception as e:  # se prueba el siguiente tipo
            errors.append(f"{cls.__name__}: {e}")
    raise RuntimeError("SFTP_PRIVATE_KEY no es una llave privada válida (" + "; ".join(errors) + ")")


@contextmanager
def session():
    """Conexión SFTP de corta duración (una por lote de operaciones)."""
    if not configured():
        raise SftpNotConfigured("SFTP no configurado (SFTP_HOST, SFTP_USER y SFTP_PASSWORD o SFTP_PRIVATE_KEY)")
    import paramiko
    s = get_settings()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(_HostKeyPolicy(s.sftp_host_key))
    kwargs = dict(hostname=s.sftp_host, port=s.sftp_port, username=s.sftp_user, timeout=20, banner_timeout=20,
                  auth_timeout=20, allow_agent=False, look_for_keys=False)
    if s.sftp_private_key:
        kwargs["pkey"] = _load_private_key(s.sftp_private_key)
    else:
        kwargs["password"] = s.sftp_password
    client.connect(**kwargs)
    try:
        yield client.open_sftp()
    finally:
        client.close()


def _inside_root(path: str) -> str:
    """Normaliza la ruta y exige que esté dentro de SFTP_PATH (no se lee fuera de la carpeta configurada)."""
    root = posixpath.normpath(get_settings().sftp_path or "/")
    norm = posixpath.normpath(path)
    if norm != root and not norm.startswith(root.rstrip("/") + "/"):
        raise ValueError("Ruta fuera de la carpeta SFTP configurada")
    return norm


def list_pdfs() -> list[RemoteFile]:
    """PDF de la carpeta configurada (sin subcarpetas)."""
    root = posixpath.normpath(get_settings().sftp_path or "/")
    out: list[RemoteFile] = []
    with session() as sftp:
        for entry in sftp.listdir_attr(root):
            if not stat.S_ISREG(entry.st_mode or 0) or not entry.filename.lower().endswith(".pdf"):
                continue
            out.append(RemoteFile(posixpath.join(root, entry.filename), entry.filename, entry.st_size or 0, int(entry.st_mtime or 0)))
    return out


def read_file(path: str, max_bytes: int | None = None) -> bytes:
    path = _inside_root(path)
    limit = max_bytes or get_settings().documents_max_size_mb * 1024 * 1024
    with session() as sftp:
        size = sftp.stat(path).st_size or 0
        if size > limit:
            raise ValueError(f"{path}: excede el tamaño máximo ({size} bytes)")
        with sftp.open(path, "rb") as fh:
            fh.prefetch(size)
            return fh.read()
