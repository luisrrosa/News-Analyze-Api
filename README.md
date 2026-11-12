# NEWS ANALYZE API

Backend FastAPI de NEWS ANALYZE. El frontend Next.js (repo `newsAnalyze`) reenvía `/api/v1/{auth,users,roles,audit,documents,news,review}`
a esta API; el navegador nunca la llama directamente.

## Fase 1 — Seguridad

| Área | Detalle |
|---|---|
| Contraseñas | Argon2id. Los hashes scrypt heredados de Next.js se migran a Argon2id en el siguiente inicio de sesión. |
| Access token | JWT HS256, 15 min, `iss`/`aud` validados; solo lleva usuario (`sub`) y sesión (`sid`). Roles y claims se resuelven en la BD en cada petición. |
| Refresh token | Opaco, 7 días, en cookie `HttpOnly; SameSite=Strict; Path=/api/v1/auth`. Solo se guarda su SHA-256. |
| Rotación | Reutilización dentro de 45 s = carrera legítima; después = robo: se revoca la familia. **No se bloquea la cuenta.** |
| CSRF | Doble envío: cookie `na_csrf` + cabecera `X-CSRF-Token` en `/auth/refresh`. |
| Fuerza bruta | Bloqueo de 15 min tras 5 fallos; límite por IP (Redis si `REDIS_URL`, si no memoria). |
| Auditoría | Cadena HMAC-SHA256 en `audit_logs`, compatible bit a bit con `lib/audit.ts` de Next.js. `GET /api/v1/audit/verify` la recorre completa. |

## Fase 2 — Gestión documental

| Pieza | Detalle |
|---|---|
| Recepción | Validación por contenido (firma, cifrado, páginas, tamaño), SHA-256, **rechazo de PDF repetidos (409)**, original en almacenamiento privado por hash. |
| Cola | Celery + Redis. `documents` + `processing_jobs` + `outbox_events` en una transacción; el despachador publica cada 5 s y el reconciliador devuelve a la cola los trabajos con concesión vencida cada 60 s. |
| Extractor | PyMuPDF determinista (`app/extraction/extractor.py`): titular por negrita, gramática de cabecera, pie DIECOM tolerante, continuación entre páginas, `summary_date` / `publication_date` / `date_basis`, alias de medios, calidad de texto medida. |
| Duplicados | SimHash 64 bits (5-gramas de caracteres) contra la ventana de 30 días: ≤ 3 duplicado enlazado a la canónica, 4–8 relacionada. Nada se borra. |
| Visor | `GET /documents/{id}/pages/{n}/image`: WebP con marca de agua del usuario (Consulta y Auditor ven sin descargar). |
| Carpeta | `python -m app.agents.folder_ingest`: espera a que el archivo esté estable, ignora enlaces simbólicos, mueve a `procesados/` o `rechazados/` con motivo. |

Validado contra el PDF de referencia: 156 ítems (118 Noticia, 33 Artículo, 5 Editorial), 32 repartidos en varias páginas,
118 con URL, 82 sin autor, 0 cabeceras o pies dentro de los cuerpos (`tests/test_extraction.py`).

## Desarrollo local (Windows)

```bash
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
cp .env.example .env        # JWT_SECRET_KEY y HMAC_AUDIT_SECRET iguales a los de newsAnalyze/.env.local
docker run -d --name news-analyze-redis -p 6379:6379 redis:7-alpine
```

Cuatro procesos (cada uno en su terminal):

```bash
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
.venv/Scripts/celery -A app.worker worker --pool=solo --loglevel=info      # Windows exige --pool=solo
.venv/Scripts/celery -A app.worker beat --loglevel=info                     # en Windows, beat va aparte
.venv/Scripts/python -m app.agents.folder_ingest                            # opcional: ingesta por carpeta
```

Con todo en Docker: `docker compose up --build` (api, worker, beat, agent, web y redis; MongoDB en Atlas).

## Pruebas

Requieren un MongoDB accesible (`TEST_MONGODB_URI`, por defecto `mongodb://localhost:27017`). Cada ejecución crea una BD
`news_analyze_pytest_*` y la borra al terminar; la cola corre en modo síncrono. Las pruebas del extractor usan el PDF de
referencia (`REFERENCE_PDF`, por defecto `../newsAnalyze/storage/reference/…`) y se omiten si no está.

```bash
.venv/Scripts/python -m pytest -q
```
