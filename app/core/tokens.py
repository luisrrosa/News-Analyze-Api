"""Access token JWT (HS256) compatible con la verificación de Next.js (lib/auth.ts):
sub = usuario, sid = sesión (familia de refresh), iss/aud validados.
Refresh token opaco: solo se guarda su SHA-256.
"""
import hashlib
import secrets
from datetime import timedelta

import jwt

from app.core.config import get_settings
from app.core.db import utcnow


def issue_access_token(user_id: str, session_id: str) -> str:
    s = get_settings()
    now = utcnow()
    payload = {
        "sub": user_id,
        "sid": session_id,
        "iss": s.jwt_issuer,
        "aud": s.jwt_audience,
        "iat": now,
        "exp": now + timedelta(minutes=s.access_token_expire_minutes),
    }
    return jwt.encode(payload, s.jwt_secret_key, algorithm="HS256", headers={"typ": "JWT"})


def decode_access_token(token: str) -> dict:
    s = get_settings()
    payload = jwt.decode(
        token,
        s.jwt_secret_key,
        algorithms=["HS256"],
        issuer=s.jwt_issuer,
        audience=s.jwt_audience,
        options={"require": ["sub", "sid", "exp", "iss", "aud"]},
    )
    return payload


def new_opaque_token() -> str:
    return secrets.token_urlsafe(32)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
