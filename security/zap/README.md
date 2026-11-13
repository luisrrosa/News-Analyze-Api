# Escaneo de seguridad completo (OWASP ZAP) en un entorno desechable

Herramienta de pruebas: **no forma parte del funcionamiento de la aplicación** ni se despliega.

```bash
security/zap/run-zap.sh                   # escaneo completo; el entorno se destruye al terminar
KEEP=1 security/zap/run-zap.sh            # deja el entorno arriba para revisar
REFERENCE_PDF=/ruta/resumen.pdf security/zap/run-zap.sh   # con un resumen real procesado (más superficie)
```

Requisitos: Docker, Node (repo de la web con `npm ci` y `npx playwright install chromium`) y el Python de la API.

## Qué hace

1. **Entorno desechable** (`docker-compose.zap.yml`): MongoDB y Redis locales en memoria, API, worker y web
   construidos desde el código, y ZAP en modo servicio. Secretos aleatorios en `.env.zap` (no se versiona).
   Sin Atlas, sin SMTP ni SFTP reales. Puertos solo en 127.0.0.1: web 3130, API 8130, ZAP 8095.
2. **Datos de prueba** (`seed.py`): un usuario por rol (admin, editor, consulta, auditor) y, si hay
   `REFERENCE_PDF`, un resumen procesado por el worker real.
3. **Tráfico real**: las pruebas de Playwright de la web pasan por el proxy de ZAP. ZAP registra cada petición
   que hace la interfaz (inicio de sesión, cargas, explorador, ficha, reportes, auditoría, roles).
4. **Plan de ZAP** (`plan.template.yaml`): importa el contrato OpenAPI (con los cuerpos de ejemplo de
   `app/api/schemas.py`), recorre la parte pública con navegador (araña AJAX) y ataca todo lo conocido con sesión
   de administrador de prueba. No ataca `logout`, `change-password` ni `refresh` (cerrarían la sesión del escáner).
   Los avisos ya revisados (10049, 10109) se rebajan a informativos.
5. **Matriz de autorización** (`authz_matrix.py`): cada endpoint contra cada rol. Lo que el rol no puede hacer debe
   responder 403 (se envía: la API rechaza antes de tocar nada); los GET permitidos no deben dar 401/403. Las
   escrituras permitidas no se envían.
6. Reportes en `reports/`: `zap-reporte.html`, `zap-reporte.json` y `authz-matrix.md`.

## Seguridad del propio escaneo

- Nunca apuntar a producción ni a desarrollo compartido: todo ocurre en la red privada del compose.
- El límite de intentos de login y la vida del token se amplían **solo** en este entorno.
- El entorno, la base de datos y los secretos se borran al terminar (`docker compose down -v`).

## En Azure DevOps

`azure-pipelines-zap.yml`: ejecución nocturna (03:00 RD) en un agente hospedado; publica los reportes como
artefacto `zap-reportes`. No usa grupos de variables ni toca los clústeres.
