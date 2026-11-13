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
    if await database["news"].find_one({"provisional_topics_version": {"$exists": False}}, projection={"_id": 1}):
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

    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, exc: ApiError):
        content = {"error": exc.error}
        if exc.code:
            content["code"] = exc.code
        return JSONResponse(content, status_code=exc.status, headers=exc.headers)

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
