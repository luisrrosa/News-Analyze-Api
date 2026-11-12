"""Contraseñas: Argon2id (doc 02 / doc 07) con verificación de hashes scrypt heredados de Next.js.

Un hash scrypt válido se reemplaza por Argon2id en el mismo inicio de sesión (needs_rehash).
"""
import base64
import hashlib
import hmac
import re

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_ph = PasswordHasher()  # Argon2id, parámetros recomendados por argon2-cffi
_DUMMY_HASH = _ph.hash("contraseña-ficticia-para-igualar-tiempos")


def hash_password(password: str) -> str:
    return _ph.hash(password)


def _verify_scrypt(password: str, stored: str) -> bool:
    # Formato Next.js: scrypt$N$r$p$saltB64$hashB64
    try:
        _, n, r, p, salt_b64, hash_b64 = stored.split("$")
        expected = base64.b64decode(hash_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p),
            maxmem=64 * 1024 * 1024,
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def verify_password(password: str, stored: str | None) -> tuple[bool, bool]:
    """Devuelve (válida, requiere_rehash)."""
    if not stored:
        try:
            _ph.verify(_DUMMY_HASH, password)
        except VerificationError:
            pass
        return False, False
    if stored.startswith("scrypt$"):
        ok = _verify_scrypt(password, stored)
        return ok, ok
    try:
        _ph.verify(stored, password)
        return True, _ph.check_needs_rehash(stored)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, False


def password_policy_error(password: object) -> str | None:
    if not isinstance(password, str) or len(password) < 12:
        return "La contraseña debe tener al menos 12 caracteres."
    if len(password) > 256:
        return "La contraseña es demasiado larga."
    if not (re.search(r"[A-Z]", password) and re.search(r"[a-z]", password) and re.search(r"[0-9]", password)):
        return "La contraseña debe incluir mayúsculas, minúsculas y dígitos."
    return None
