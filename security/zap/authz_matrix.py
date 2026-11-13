"""Matriz de autorización por rol: cada endpoint contra cada rol de prueba.

    python authz_matrix.py     (lo llama run-zap.sh, con roles.json de seed.py)

Toma de la propia API qué claim exige cada endpoint (atributo required_claims de require/require_any) y, para cada
rol, compara con lo que responde de verdad:
  - si al rol le falta el claim → la petición debe responder 403 (se envía aunque sea POST/PUT/DELETE: la API
    rechaza antes de tocar nada);
  - si lo tiene → los GET no deben responder 401/403 (las escrituras permitidas NO se envían: cambiarían datos).
Escribe reports/authz-matrix.md y termina con error si encuentra una diferencia.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.environ.get("ZAP_API_URL", "http://127.0.0.1:8130")
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))
# Solo para importar la app y leer sus rutas (no se conecta a nada).
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:1")
os.environ.setdefault("JWT_SECRET_KEY", "x" * 48)
os.environ.setdefault("HMAC_AUDIT_SECRET", "y" * 48)


def call(method: str, path: str, token: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def route_claims():
    from fastapi.routing import APIRoute
    from app.main import app

    def find(dependant):
        for d in dependant.dependencies:
            meta = getattr(d.call, "required_claims", None)
            if meta:
                return meta
            inner = find(d)
            if inner:
                return inner
        return None

    out = []
    for r in app.routes:
        if isinstance(r, APIRoute):
            meta = find(r.dependant)
            if meta and meta[1]:
                for m in r.methods:
                    out.append((m, r.path, meta))
    return out


def main() -> None:
    roles = json.load(open(os.path.join(HERE, "roles.json"), encoding="utf-8"))
    admin = roles["admin"]["token"]
    role_claims = json.loads(call("GET", "/api/v1/roles", admin)[1])["role_claims"]

    # Identificadores reales para rellenar las rutas
    def first(path, key, field="id"):
        st, b = call("GET", path, admin)
        try:
            items = json.loads(b).get(key) or []
            return items[0][field] if items else "no-existe"
        except Exception:
            return "no-existe"
    latest = json.loads(call("GET", "/api/v1/news/summary-dates", admin)[1]).get("latest") or "2026-01-01"
    ids = {
        "news_id": first(f"/api/v1/news?date_from={latest}&date_to={latest}&limit=1", "items"),
        "document_id": first(f"/api/v1/documents?date_from={latest}&date_to={latest}", "documents"),
        "user_id": first("/api/v1/users", "users"),
        "media_id": first("/api/v1/catalogs/media", "media"),
        "oid": first("/api/v1/catalogs/officeholders", "officeholders"),
        "review_id": "rev_inexistente", "code": "T01", "version": "inexistente", "page": "1",
    }
    fill = lambda p: re.sub(r"\{(\w+)\}", lambda m: str(ids.get(m.group(1), "x")), p)

    rows, errors = [], []
    for method, path, (mode, needed) in sorted(route_claims(), key=lambda x: (x[1], x[0])):
        for role, info in roles.items():
            have = set(role_claims.get(role, []))
            allowed = all(c in have for c in needed) if mode == "all" else any(c in have for c in needed)
            url = fill(path) + ("?date_from=2026-01-01&date_to=2026-01-02" if method == "GET" and "{" not in path else "")
            if not allowed:
                st, _ = call(method, url, info["token"], {} if method in ("POST", "PUT", "PATCH") else None)
                ok = st == 403
                rows.append((method, path, role, "denegado", st, ok))
            elif method == "GET":
                st, _ = call(method, url, info["token"])
                ok = st not in (401, 403)
                rows.append((method, path, role, "permitido", st, ok))
            else:
                rows.append((method, path, role, "permitido (no se envía)", "-", True))
            if not rows[-1][5]:
                errors.append(rows[-1])

    os.makedirs(os.path.join(HERE, "reports"), exist_ok=True)
    with open(os.path.join(HERE, "reports", "authz-matrix.md"), "w", encoding="utf-8") as f:
        f.write("# Matriz de autorización por rol\n\n")
        f.write(f"{len(rows)} comprobaciones, {len(errors)} diferencias.\n\n")
        f.write("| Método | Ruta | Rol | Esperado | Respuesta | OK |\n|---|---|---|---|---|---|\n")
        for m, p, r, exp, st, ok in rows:
            f.write(f"| {m} | `{p}` | {r} | {exp} | {st} | {'✓' if ok else '✗'} |\n")
    print(f"Matriz de autorización: {len(rows)} comprobaciones, {len(errors)} diferencias")
    for e in errors:
        print("  ✗", e)
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
