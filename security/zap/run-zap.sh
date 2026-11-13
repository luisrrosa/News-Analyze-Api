#!/usr/bin/env bash
# Escaneo de seguridad completo de NEWS ANALYZE en un entorno DESECHABLE (ver README.md).
#
#   security/zap/run-zap.sh                 # escaneo completo y destruye el entorno al terminar
#   KEEP=1 security/zap/run-zap.sh          # deja el entorno arriba para revisar
#
# Variables: WEB_REPO (ruta del repo de la web; por defecto ../../../newsAnalyze), REFERENCE_PDF (opcional),
#            PYTHON (intérprete con las dependencias de la API; por defecto el .venv del repo o python3).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
export WEB_REPO="${WEB_REPO:-$(cd "$HERE/../../../newsAnalyze" && pwd)}"
COMPOSE=(docker compose -f docker-compose.zap.yml)
if [ -z "${PYTHON:-}" ]; then
  if [ -x "$HERE/../../.venv/Scripts/python.exe" ]; then PYTHON="$HERE/../../.venv/Scripts/python.exe"
  elif [ -x "$HERE/../../.venv/bin/python" ]; then PYTHON="$HERE/../../.venv/bin/python"
  else PYTHON=python3; fi
fi
ZAP_PROXY="http://127.0.0.1:8095"
# A la API de ZAP se le habla a través de su propio proxy con el host especial http://zap/
# (una llamada directa a 127.0.0.1:8095 la reenviaría como tráfico normal).
zap() { curl -s -m 60 -x "$ZAP_PROXY" "http://zap/JSON/$1"; }

cleanup() {
  if [ "${KEEP:-0}" != "1" ]; then
    echo "== Destruyendo el entorno de escaneo"
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    rm -f .env.zap roles.json e2e.env plan.generated.yaml
  fi
}
trap cleanup EXIT

echo "== 1/7 Secretos aleatorios del entorno (no se versionan)"
"$PYTHON" - <<'PY'
import secrets
pw = secrets.token_hex(16)
open(".env.zap", "w").write(
    f"JWT_SECRET_KEY={secrets.token_urlsafe(48)}\nHMAC_AUDIT_SECRET={secrets.token_urlsafe(48)}\n"
    f"REDIS_PASSWORD={pw}\nREDIS_URL=redis://:{pw}@redis:6379/0\n"
    f"BOOTSTRAP_ADMIN_EMAIL=admin@zap.test\nBOOTSTRAP_ADMIN_PASSWORD=Zap{secrets.token_hex(8)}Aa9\n")
PY

echo "== 2/7 Levantando el entorno desechable (MongoDB y Redis locales, API, worker, web, ZAP)"
mkdir -p reports
"${COMPOSE[@]}" up -d --build

echo "== 3/7 Usuarios de prueba por rol y resumen de referencia"
"$PYTHON" seed.py

echo "== 4/7 Esperando a ZAP"
for _ in $(seq 1 90); do zap "core/view/version/" | grep -q version && break; sleep 2; done
# Filtros de alertas revisadas, antes de cualquier tráfico (ver plan.filters.yaml).
FID="$(zap "automation/action/runPlan/?filePath=/zap/wrk/plan.filters.yaml" | "$PYTHON" -c "import sys,json;print(json.load(sys.stdin)['planId'])")"
for _ in $(seq 1 30); do zap "automation/view/planProgress/?planId=$FID" | grep -q '"finished":"[^"]' && break; sleep 2; done

echo "== 5/7 Playwright a través del proxy de ZAP (registra el tráfico real: POST, cargas, revisión, reportes)"
(
  cd "$WEB_REPO"
  set -a; . "$HERE/e2e.env"; set +a
  E2E_BASE_URL="http://web:8080" E2E_PROXY="$ZAP_PROXY" npx playwright test --reporter=line || echo "(Playwright con fallos: el escaneo continúa)"
)

echo "== 6/7 Plan de ZAP: contrato OpenAPI + recorrido AJAX + escaneo activo"
ADMIN_TOKEN="$("$PYTHON" -c "import json;print(json.load(open('roles.json'))['admin']['token'])")"
sed "s|__ADMIN_TOKEN__|$ADMIN_TOKEN|" plan.template.yaml > plan.generated.yaml
PLAN_ID="$(zap "automation/action/runPlan/?filePath=/zap/wrk/plan.generated.yaml" | "$PYTHON" -c "import sys,json;print(json.load(sys.stdin)['planId'])")"
while true; do
  PROGRESS="$(zap "automation/view/planProgress/?planId=$PLAN_ID")"
  echo "$PROGRESS" | "$PYTHON" -c "import sys,json;d=json.load(sys.stdin);print('   ', (d.get('info') or [''])[-1][:110])" || true
  echo "$PROGRESS" | grep -q '"finished":"[^"]' && break
  sleep 30
done

echo "== 7/7 Matriz de autorización por rol"
MATRIX=0
"$PYTHON" authz_matrix.py || MATRIX=$?

"$PYTHON" - <<'PY'
import json
d = json.load(open("reports/zap-reporte.json", encoding="utf-8"))
counts = {}
for site in d.get("site", []):
    for a in site.get("alerts", []):
        counts[a["riskdesc"].split(" ")[0]] = counts.get(a["riskdesc"].split(" ")[0], 0) + 1
        if a["riskcode"] in ("3", "2"):
            print(f"  [{a['riskdesc']}] {a['name']} ({a['pluginid']}) x{a.get('count')}")
print("Alertas ZAP por riesgo:", counts or "ninguna")
PY
echo "Reportes: $HERE/reports (zap-reporte.html, zap-reporte.json, authz-matrix.md)"
exit $MATRIX
