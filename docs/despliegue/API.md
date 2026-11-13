# Despliegue en AKS (Azure DevOps)

Mismo esquema que RecargaDO (`Ms.RecargaMobile.Api`) y LECO (`Ms.Leco.Counter.Reader`): cada `pipeline.yml`
extiende `pipelines/service-pipeline.yml` de `devops.templates` (tag **1.0.86**), compila la imagen, la publica en
el ACR y la instala con el chart `devops-template` **1.0.91**. No hace falta cambiar las plantillas.

```
Navegador ──HTTPS──► news-analyze.<dominio>        NewsAnalyze.Web (única entrada pública)
                          │ red interna del namespace
                          ▼
                     http://news-analyze-api         este repo, APP_ROLE=api (sin ingress)
                          │                ▲
                          ▼                │ MongoDB Atlas (BD por entorno)
                     news-analyze-redis ◄── news-analyze-worker (APP_ROLE=worker) ──SFTP──► file server
```

| Release | Pipeline | Qué es | Réplicas dev / prod |
|---|---|---|---|
| `news-analyze-redis` | `deployment/redis/pipeline.yml` | Redis 7 con contraseña, sin disco | 1 / 1 |
| `news-analyze-api` | `deployment/pipeline.yml` | FastAPI, puerto 80, sondas `/healthz` y `/readyz` | 1 / 2 |
| `news-analyze-worker` | `deployment/worker/pipeline.yml` | Celery con programador incluido (outbox, reconciliación, SFTP) | 1 / 1 (siempre 1) |

| Entorno | Namespace | Base de datos | Valores |
|---|---|---|---|
| Desarrollo | `placebet-develop` | `news_analize` | `values.nonprod.yml` |
| Producción | `placebet` | `news_analyze_prod` | `values.prod.yml` |

Producción solo se despliega desde ramas `release/*` o `releases/*`; `main` va solo a desarrollo.

Los tres releases usan nombres fijos (`nameOverride`): dentro del namespace la web llega a la API como
`http://news-analyze-api` y la API y el worker a Redis como `news-analyze-redis:6379`.

## 1. Crear los pipelines (una vez)

Azure DevOps → *Pipelines → New pipeline → Azure Repos Git* → `NewsAnalyze.Api` → *Existing Azure Pipelines YAML
file*, tres veces: `/deployment/redis/pipeline.yml`, `/deployment/pipeline.yml` y `/deployment/worker/pipeline.yml`.
Cada uno necesita permiso para los grupos `Placebet.Shared`, `Placebet.Dev`, `Placebet.Prod` y para el repo
`devops.templates`. **Orden del primer despliegue:** Redis → API → worker (y luego la web).

El pipeline de Redis solo se dispara con cambios en `deployment/redis/`; el de la API ignora `deployment/worker/` y
`deployment/redis/`; el del worker ignora `deployment/redis/`.

## 2. Secretos (grupos de variables)

`AzureResourceGroup` y `KubernetesCluster` están en `Placebet.Dev` y `Placebet.Prod`; `AcrServiceConnection`,
`AcrName` y `AzureSubscription` en `Placebet.Shared`. Crear en **`Placebet.Dev` y `Placebet.Prod`**, como secretos
y **distintos en cada uno**:

| Variable del grupo | Llega al contenedor como | Cómo generarla |
|---|---|---|
| `NEWS_ANALYZE_CONNECTION_STRING` | `MONGODB_URI` | Cadena `mongodb+srv://…` del Atlas actual (la BD va en los values). |
| `NEWS_ANALYZE_JWT_SECRET` | `JWT_SECRET_KEY` | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `NEWS_ANALYZE_HMAC_AUDIT_SECRET` | `HMAC_AUDIT_SECRET` | Igual que el anterior, **distinto** del JWT. No se cambia después: firma la cadena de auditoría. |
| `NEWS_ANALYZE_REDIS_PASSWORD` | `REDIS_PASSWORD` (Redis) y dentro de `REDIS_URL` | `python -c "import secrets; print(secrets.token_hex(24))"` (solo letras y dígitos: va dentro de una URL). |
| `NEWS_ANALYZE_SFTP_HOST` | `SFTP_HOST` | Nombre o IP del file server. Vacía = sin ingesta SFTP (solo carga manual). |
| `NEWS_ANALYZE_SFTP_USER` | `SFTP_USER` | Usuario de **solo lectura**. |
| `NEWS_ANALYZE_SFTP_PASSWORD` | `SFTP_PASSWORD` | Contraseña del usuario. |
| `NEWS_ANALYZE_SFTP_PATH` | `SFTP_PATH` | Carpeta donde se dejan los resúmenes, p. ej. `/resumenes`. |
| `NEWS_ANALYZE_SFTP_HOST_KEY` | `SFTP_HOST_KEY` | Huella del servidor: `ssh-keyscan -t ed25519 <host> \| ssh-keygen -lf -` → `SHA256:…`. Recomendada: sin ella se acepta cualquier llave (se registra advertencia). |

Si una variable no existe en el grupo, Azure deja el texto `$(NOMBRE)`; la API lo trata como vacío.
Las variables no secretas (BD, almacenamiento, OCR, intervalos) están en `values.*.yml` bajo `image.env`.

## 3. Los PDF (decisión del propietario: no se mueven ni se copian)

- **Desde el file server (SFTP):** cada `SFTP_POLL_SECONDS` (300) el worker lista `SFTP_PATH`, espera a que cada PDF
  nuevo no cambie entre dos revisiones, lo lee **en memoria** y lo procesa. No lo mueve ni lo borra; recuerda lo ya
  visto en la colección `sftp_seen`. El documento guarda la ruta (`sftp:/resumenes/archivo.pdf`) y el visor la vuelve
  a leer cada vez (caché de 10 minutos en memoria). Si el archivo se mueve, se borra o cambia (huella SHA-256), el visor
  muestra solo el texto.
- **Subidos desde una PC:** la API los deja en Redis (`STORAGE_BACKEND=memory`) solo mientras el worker los procesa
  (máximo `UPLOAD_TTL_SECONDS`) y los descarta al terminar. Sus noticias quedan con texto, sin imagen de página.
- Nada se escribe en disco. Redis no persiste: si se reinicia, los trabajos se recuperan del outbox de MongoDB
  (un PDF subido que aún no se procesaba hay que volver a subirlo).

## 4. Puerto, salud, réplicas

- API: `PORT=80`, sin root (UID 10001), 2 workers de uvicorn por réplica. Sondas `/healthz` (vida) y `/readyz`
  (llega a MongoDB). Las sesiones, la auditoría y los límites de intentos están en MongoDB/Redis: varias réplicas son seguras.
- Worker: sin HTTP ni sondas; **una sola réplica** (lleva el programador). Concurrencia 2 (`WORKER_CONCURRENCY`).
- Redis: sin HTTP ni sondas; límite 200 MB con desalojo de claves temporales.
- OCR: la imagen incluye Tesseract con español para páginas escaneadas.

## 5. Base de datos y primer acceso

- Los índices, roles y migraciones de permisos se crean solos al arrancar.
- **Producción:** la BD `news_analyze_prod` empieza vacía. El administrador se copia desde desarrollo (colección `users`);
  al entrar se le obliga a cambiar la contraseña. Los catálogos (temas, triage, medios) se crean con sus valores iniciales.

## 6. Después del primer despliegue

1. `kubectl -n placebet-develop get pods -l app.kubernetes.io/name=news-analyze-api` → `Running` y `1/1`.
2. Desde la web: iniciar sesión, cargar un PDF y ver sus noticias en *Noticias Extraídas*.
3. Con SFTP: dejar un PDF en `SFTP_PATH`; en ≤ 10 minutos aparece en *Carga & Ingesta* con origen `sftp`.
4. Logs del worker: `kubectl -n placebet-develop logs deploy/news-analyze-worker`.

## 7. Probar las imágenes en el PC

```bash
docker build -t news-analyze-api:local .
docker build -t news-analyze-redis:local deployment/redis
docker network create na
docker run -d --rm --name news-analyze-redis --network na -e REDIS_PASSWORD=clave news-analyze-redis:local
docker run --rm --network na --env-file .env -e STORAGE_BACKEND=memory -e REDIS_URL=redis://:clave@news-analyze-redis:6379/0 -e PORT=80 -p 18000:80 news-analyze-api:local
docker run --rm --network na --env-file .env -e STORAGE_BACKEND=memory -e REDIS_URL=redis://:clave@news-analyze-redis:6379/0 news-analyze-api:local worker
```

## 8. Seguridad de red, secretos y capacidad (revisión 2026-11)

- **NetworkPolicy:** los values la activan (solo la web llega a la API; solo la API y el worker a Redis). Requiere el
  chart `devops-template` con `networkPolicy` (la versión actual de `devops.templates` la tiene) y un clúster con
  política de red (Azure CNI `azure` o `calico`). Si el chart 1.0.91 no la genera: `kubectl -n <ns> apply -f deployment/networkpolicy.yaml`.
- **SFTP en producción:** `NEWS_ANALYZE_SFTP_HOST_KEY` es obligatoria (sin ella la API no se conecta al file server).
- **Secretos:** hoy viajan como `image.env` (igual que RecargaDO/LECO), visibles para quien pueda leer el Deployment.
  Recomendado al subir el chart: `secretEnv` desde un Secret de Kubernetes o `keyVault` (CSI de Azure Key Vault), y
  restringir el RBAC del namespace. Con `JWT_SECRET_KEY` se pueden falsificar sesiones; con `HMAC_AUDIT_SECRET`, la auditoría.
- **Auditoría:** el worker escribe cada hora un ancla firmada de la cabeza de la cadena (también en su log:
  `AUDIT_ANCHOR seq=…`) y verifica la cadena completa cada día (`AUDIT_CHAIN_BROKEN` en el log si falla). Conviene
  enviar esos logs a Log Analytics: un ancla fuera de MongoDB detecta que se borren los últimos registros.
- **Capacidad de MongoDB Atlas:** en la prueba de carga (k6, 20 usuarios) el límite fue Atlas, no la API (los procesos
  estaban casi siempre esperando a la BD). Para producción usar un clúster dedicado (M10 o superior); el gratuito M0
  limita las operaciones por segundo.

## Por confirmar con DevOps

- Que el chart `devops-template` 1.0.91 lea `image.env`, `service.port`, `ingress.enabled: false` y las sondas como el chart actual de `devops.templates`.
- Que los pods de `placebet-develop` y `placebet` lleguen por red al file server (puerto 22) y a MongoDB Atlas (lista de IP de Atlas).
- Que `news-analyze-api`, `news-analyze-worker` y `news-analyze-redis` no choquen con releases existentes.
