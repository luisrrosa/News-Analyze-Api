"""Límite de tasa por clave. Usa Redis si REDIS_URL está definido (varias réplicas); si no, memoria del proceso."""
import time

from redis import asyncio as aioredis

from app.core.config import get_settings

_memory: dict[str, tuple[int, float]] = {}
_redis: aioredis.Redis | None = None


async def _get_redis() -> aioredis.Redis | None:
    global _redis
    url = get_settings().redis_url
    if not url:
        return None
    if _redis is None:
        _redis = aioredis.from_url(url)
    return _redis


async def hit(key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Registra un intento. Devuelve (permitido, segundos_para_reintentar)."""
    r = await _get_redis()
    if r is not None:
        try:
            k = f"rl:{key}"
            # Atómico: la clave nace con su vencimiento (no puede quedar sin TTL y bloquear para siempre).
            async with r.pipeline(transaction=True) as pipe:
                pipe.set(k, 0, ex=window_seconds, nx=True)
                pipe.incr(k)
                pipe.ttl(k)
                _, count, ttl = await pipe.execute()
            return count <= limit, max(int(ttl), 0)
        except Exception:
            pass  # Redis caído: se degrada a memoria antes que bloquear el login
    now = time.monotonic()
    count, reset_at = _memory.get(key, (0, now + window_seconds))
    if reset_at <= now:
        count, reset_at = 0, now + window_seconds
    count += 1
    _memory[key] = (count, reset_at)
    if len(_memory) > 10_000:
        for k in [k for k, (_, r_at) in _memory.items() if r_at <= now]:
            _memory.pop(k, None)
    return count <= limit, int(reset_at - now) + 1


def reset_memory() -> None:
    _memory.clear()
