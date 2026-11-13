from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración externa (variables de entorno o .env). Ningún secreto tiene valor por defecto."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"

    mongodb_uri: str
    mongodb_db_name: str = "news_analize"
    mongodb_dns_servers: str = ""

    jwt_secret_key: str = Field(min_length=32)
    jwt_issuer: str = "news-analize-api"
    jwt_audience: str = "news-analize-web"
    access_token_expire_minutes: int = Field(default=15, ge=1, le=1440)
    refresh_token_expire_days: int = Field(default=7, ge=1, le=90)
    refresh_grace_seconds: int = Field(default=45, ge=0, le=300)

    hmac_audit_secret: str = Field(min_length=32)

    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""

    # Cookies: Secure obligatorio salvo en desarrollo sobre http://localhost
    cookie_secure: bool = True

    # FastAPI solo es accesible a través del proxy de Next.js: se confía en su X-Forwarded-For.
    trust_proxy_headers: bool = True

    redis_url: str = ""
    login_rate_limit: int = 10
    login_rate_window_seconds: int = 300

    # Documentos (Plan V3.0 §13 y §17)
    storage_backend: str = "local"            # local | azure (pendiente de cuenta)
    storage_root: str = "./storage"
    documents_max_size_mb: int = Field(default=50, ge=1, le=200)
    documents_max_pages: int = Field(default=200, ge=1, le=1000)
    dedupe_window_days: int = Field(default=30, ge=1, le=365)

    # Cola de trabajos (Celery + Redis). En pruebas, ejecución síncrona.
    celery_broker_url: str = ""
    celery_always_eager: bool = False
    job_max_attempts: int = Field(default=3, ge=1, le=10)
    job_lease_seconds: int = Field(default=120, ge=30, le=3600)

    # Agente de ingesta por carpeta (Plan V3.0 §15)
    ingest_folder: str = "./storage/inbox"
    ingest_poll_seconds: int = Field(default=60, ge=5)
    ingest_stable_seconds: int = Field(default=30, ge=1)

    # OCR de excepción para páginas escaneadas: tesseract | none. MuPDF trae el motor; solo necesita los
    # modelos de idioma (tessdata con spa.traineddata). Vacío = TESSDATA_PREFIX o la instalación de Tesseract.
    ocr_engine: str = "tesseract"
    tessdata_prefix: str = ""

    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url or "memory://"

    @field_validator("jwt_secret_key", "hmac_audit_secret")
    @classmethod
    def no_placeholder(cls, v: str) -> str:
        if v.startswith("CAMBIAR_") or v.startswith("CLAVE_"):
            raise ValueError("secreto de ejemplo: configure un valor real")
        return v

    @model_validator(mode="after")
    def distinct_secrets(self) -> "Settings":
        if self.jwt_secret_key == self.hmac_audit_secret:
            raise ValueError("HMAC_AUDIT_SECRET debe ser distinto de JWT_SECRET_KEY")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
