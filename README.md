# NEWS ANALYZE API

Backend FastAPI de NEWS ANALYZE (Fase 1 del Plan V3.0): autenticación, usuarios, roles/claims y auditoría.
El frontend Next.js (repo `newsAnalyze`) reenvía `/api/v1/{auth,users,roles,audit}` a esta API; el navegador nunca la llama directamente.

## Qué incluye

| Área | Detalle |
|---|---|
| Contraseñas | Argon2id. Los hashes scrypt heredados de Next.js se migran a Argon2id en el siguiente inicio de sesión. |
| Access token | JWT HS256, 15 min, `iss`/`aud` validados; solo lleva usuario (`sub`) y sesión (`sid`). Roles y claims se resuelven en la BD en cada petición. |
| Refresh token | Opaco, 7 días, en cookie `HttpOnly; SameSite=Strict; Path=/api/v1/auth`. Solo se guarda su SHA-256. |
| Rotación | Cada refresh consume el token y emite otro de la misma familia. Reutilización dentro de 45 s = carrera legítima (se emite solo access token); después = robo: se revoca la familia. **No se bloquea la cuenta.** |
| CSRF | Doble envío: cookie `na_csrf` + cabecera `X-CSRF-Token` en `/auth/refresh`. |
| Fuerza bruta | Bloqueo de 15 min tras 5 fallos; límite por IP (Redis si `REDIS_URL`, si no memoria). |
| Auditoría | Cadena HMAC-SHA256 en `audit_logs`, compatible bit a bit con `lib/audit.ts` de Next.js (ambos escriben en la misma cadena). `GET /api/v1/audit/verify` la recorre completa. |
| Bootstrap | Índices, matriz inicial de roles y admin inicial (`BOOTSTRAP_ADMIN_*`) si no hay usuarios. |

## Desarrollo local

```bash
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
cp .env.example .env        # JWT_SECRET_KEY y HMAC_AUDIT_SECRET iguales a los de newsAnalyze/.env.local
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
```

Con la API en el puerto 8000, `npm run dev` en `../newsAnalyze` reenvía las rutas automáticamente.
Documentación interactiva (solo `APP_ENV=development`): http://localhost:8000/api/docs

### Todo con Docker

```bash
docker compose up --build     # api + web (../newsAnalyze) + redis; MongoDB en Atlas
```

## Pruebas

Requieren un MongoDB accesible (local o contenedor). Cada ejecución crea una BD `news_analyze_pytest_*` y la borra al terminar.

```bash
.venv/Scripts/python -m pytest -q          # TEST_MONGODB_URI, por defecto mongodb://localhost:27017
```

`azure-pipelines.yml` ejecuta las mismas pruebas con un contenedor `mongo:7` y construye la imagen Docker.
