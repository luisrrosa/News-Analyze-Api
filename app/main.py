import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.deps import ApiError
from app.api.routes import audit, auth, catalogs, config, documents, news, reports, review, roles, rules, users
from app.core import db
from app.core.bootstrap import run_bootstrap
from app.core.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("news-analyze-api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_settings()  # falla al arrancar si falta configuración obligatoria
    database = await db.connect()
    await run_bootstrap(database)
    # Noticias anteriores a la Fase 5 sin temas provisionales guardados: se calculan en segundo plano.
    pending = None
    if await database["news"].find_one({"$or": [{"provisional_topics_version": {"$exists": False}},
                                                 {"provisional_topic_terms": {"$exists": False}, "topics_manual": {"$ne": True}}]},
                                        projection={"_id": 1}):
        from app.worker import enqueue_topic_recompute
        if not enqueue_topic_recompute():
            from app.services.topics import recompute_news_topics
            pending = asyncio.create_task(recompute_news_topics())
    yield
    if pending and not pending.done():
        pending.cancel()
    await db.close()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title="NEWS ANALYZE API",
        version="0.1.0",
        lifespan=lifespan,
        # La documentación interactiva solo en desarrollo.
        docs_url="/api/docs" if s.app_env == "development" else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if s.app_env == "development" else None,
    )

    from app.core.body_limit import BodyLimitMiddleware
    app.add_middleware(BodyLimitMiddleware)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        # Cabeceras defensivas también en la API (OWASP ZAP 10021 / 90004), aunque solo la llame la web.
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, exc: ApiError):
        content = {"error": exc.error}
        if exc.code:
            content["code"] = exc.code
        return JSONResponse(content, status_code=exc.status, headers=exc.headers)

    from app.core.audit import AuditUnavailable

    @app.exception_handler(AuditUnavailable)
    async def audit_unavailable(_: Request, exc: AuditUnavailable):
        # Falla cerrada: sin registro de auditoría no se entrega la descarga ni la exportación.
        log.error("auditoría no disponible para %s: acción cancelada", exc)
        return JSONResponse({"error": "No se pudo registrar la acción en la auditoría; inténtelo de nuevo.",
                             "code": "AUDIT_UNAVAILABLE"}, status_code=503)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError):
        return JSONResponse({"error": "Solicitud inválida", "code": "VALIDATION_ERROR"}, status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def http_handler(_: Request, exc: StarletteHTTPException):
        return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception):
        log.exception("error no controlado")  # sin trazas internas hacia el cliente
        return JSONResponse({"error": "Error interno del servidor"}, status_code=500)

    @app.get("/", include_in_schema=False)
    async def root():
        info = {"service": "NEWS ANALYZE API", "status": "ok", "health": "/api/health"}
        if s.app_env == "development":
            info["docs"] = "/api/docs"
        return info

    @app.get("/api/health", tags=["health"])
    async def health():
        await db.get_db().command("ping")
        return {"status": "ok"}

    # Sondas de Kubernetes (chart devops-template): vida = el proceso responde; listo = además llega a MongoDB.
    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz():
        try:
            await db.get_db().command("ping")
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ok"}

    for r in (auth.router, users.router, roles.router, audit.router, documents.router, news.router, reports.router, rules.router, catalogs.router, review.router, config.router):
        app.include_router(r, prefix="/api/v1")
    return app


app = create_app()
