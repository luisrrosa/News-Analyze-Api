#!/bin/sh
# Un solo contenedor, varios roles: primer argumento o APP_ROLE. El mismo Dockerfile sirve a todos los releases.
#   api     FastAPI (uvicorn) en $PORT
#   worker  Celery worker con el programador incluido (-B): outbox, reconciliación y revisión del SFTP.
#           Una sola réplica (el programador no debe duplicarse).
#   worker_only  Celery worker sin programador (docker-compose, donde beat va aparte)
#   beat    solo el programador (docker-compose de desarrollo)
#   agent   ingesta por carpeta local (docker-compose de desarrollo)
set -e
ROLE="${1:-${APP_ROLE:-api}}"
case "$ROLE" in
  api)
    exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers "${WORKERS:-2}" \
      --proxy-headers --forwarded-allow-ips "*" --no-server-header ;;
  worker)
    exec celery -A app.worker worker -B --loglevel=info --concurrency="${WORKER_CONCURRENCY:-2}" \
      --schedule /tmp/celerybeat-schedule ;;
  worker_only)
    exec celery -A app.worker worker --loglevel=info --concurrency="${WORKER_CONCURRENCY:-2}" ;;
  beat)
    exec celery -A app.worker beat --loglevel=info --schedule /tmp/celerybeat-schedule ;;
  agent)
    exec python -m app.agents.folder_ingest ;;
  *)
    echo "Rol desconocido: ${ROLE}" >&2; exit 64 ;;
esac
