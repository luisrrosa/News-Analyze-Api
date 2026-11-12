from datetime import datetime, timezone

import dns.resolver
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import get_settings

_client: AsyncMongoClient | None = None


def _apply_dns_servers() -> None:
    """Opcional: DNS explícitos para resolver mongodb+srv (mismo criterio que la app Next.js)."""
    servers = [s.strip() for s in get_settings().mongodb_dns_servers.split(",") if s.strip()]
    if servers:
        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = servers
        dns.resolver.default_resolver = resolver


async def connect() -> AsyncDatabase:
    global _client
    if _client is None:
        _apply_dns_servers()
        _client = AsyncMongoClient(get_settings().mongodb_uri, serverSelectionTimeoutMS=10000, tz_aware=True)
    return _client[get_settings().mongodb_db_name]


def get_db() -> AsyncDatabase:
    if _client is None:
        raise RuntimeError("MongoDB no inicializado")
    return _client[get_settings().mongodb_db_name]


async def close() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


def iso_now() -> str:
    """Formato idéntico a Date.prototype.toISOString() (milisegundos y 'Z'), compartido con Next.js."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
