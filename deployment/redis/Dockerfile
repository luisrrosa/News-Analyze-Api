# Redis de NEWS ANALYZE para AKS: sin root (el chart exige runAsNonRoot con UID numérico) y sin persistencia.
FROM redis:7.4-alpine

# El usuario "redis" de la imagen oficial (UID 999). Se arranca redis-server directamente, sin el
# entrypoint que cambia de usuario (necesita root).
USER 999:1000
WORKDIR /data
EXPOSE 6379

# Contraseña obligatoria (REDIS_PASSWORD, desde el grupo de variables). Límite de memoria con desalojo de las
# claves que vencen (copias temporales de PDF); la cola de Celery no vence. Sin RDB ni AOF.
ENTRYPOINT []
CMD ["sh", "-c", "test -n \"$REDIS_PASSWORD\" || { echo 'Falta REDIS_PASSWORD' >&2; exit 1; }; exec redis-server --port 6379 --requirepass \"$REDIS_PASSWORD\" --save '' --appendonly no --maxmemory ${REDIS_MAXMEMORY:-200mb} --maxmemory-policy volatile-lru --protected-mode no"]
