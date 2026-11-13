"""Datos de prueba del entorno de escaneo: un usuario por rol (con su token) y un resumen PDF procesado.

    python seed.py            (lo llama run-zap.sh; lee .env.zap y escribe roles.json y e2e.env)

Solo habla con la API del entorno desechable (http://127.0.0.1:8130). No toca ninguna base real.
"""
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.environ.get("ZAP_API_URL", "http://127.0.0.1:8130")


def env_file() -> dict:
    out = {}
    for line in open(os.path.join(HERE, ".env.zap"), encoding="utf-8"):
        if "=" in line and not line.startswith("#"):
            k, v = line.rstrip("\n").split("=", 1)
            out[k] = v
    return out


def call(method: str, path: str, token: str | None = None, body=None, raw: bytes | None = None, ctype: str | None = None):
    headers = {"X-Forwarded-For": f"10.99.{secrets.randbelow(250)}.{secrets.randbelow(250)}"}
    data = None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    if raw is not None:
        data, headers["Content-Type"] = raw, ctype
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def strong() -> str:
    return "Zap" + secrets.token_hex(8) + "A9z"


def login(email: str, password: str) -> dict:
    st, data = call("POST", "/api/v1/auth/login", body={"email": email, "password": password})
    if st != 200:
        sys.exit(f"login {email}: {st} {data}")
    return data


def ready_user(email: str, initial: str) -> tuple[str, str]:
    """Inicia sesión, cambia la clave inicial y devuelve (clave final, token)."""
    data = login(email, initial)
    final = strong()
    st, resp = call("POST", "/api/v1/auth/change-password", data["access_token"],
                    {"current_password": initial, "new_password": final})
    if st != 200:
        sys.exit(f"cambio de clave {email}: {st} {resp}")
    return final, login(email, final)["access_token"]


def main() -> None:
    env = env_file()
    for _ in range(90):
        try:
            if urllib.request.urlopen(API + "/readyz", timeout=3).status == 200:
                break
        except Exception:
            time.sleep(2)
    else:
        sys.exit("La API del entorno de escaneo no respondió")

    admin_email = env["BOOTSTRAP_ADMIN_EMAIL"]
    admin_pw, admin_token = ready_user(admin_email, env["BOOTSTRAP_ADMIN_PASSWORD"])
    roles = {"admin": {"email": admin_email, "password": admin_pw, "token": admin_token}}
    for role in ("editor", "consulta", "auditor"):
        email = f"{role}.{uuid.uuid4().hex[:6]}@zap.test"
        initial = strong()
        st, resp = call("POST", "/api/v1/users", admin_token, {"name": f"ZAP {role}", "email": email, "roles": [role], "password": initial})
        if st != 201:
            sys.exit(f"crear {role}: {st} {resp}")
        pw, token = ready_user(email, initial)
        roles[role] = {"email": email, "password": pw, "token": token}

    pdf = os.environ.get("REFERENCE_PDF", "")
    if pdf and os.path.exists(pdf):
        boundary = uuid.uuid4().hex
        payload = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"resumen.pdf\"\r\n"
                   f"Content-Type: application/pdf\r\n\r\n").encode() + open(pdf, "rb").read() + f"\r\n--{boundary}--\r\n".encode()
        st, resp = call("POST", "/api/v1/documents/upload", admin_token, raw=payload, ctype=f"multipart/form-data; boundary={boundary}")
        print("PDF de referencia:", st)
        for _ in range(60):   # el worker real lo procesa
            st, docs = call("GET", "/api/v1/documents?limit=5", admin_token)
            if any(str(d.get("status", "")).startswith("completed") for d in docs.get("documents", [])):
                break
            time.sleep(3)

    json.dump(roles, open(os.path.join(HERE, "roles.json"), "w", encoding="utf-8"), indent=2)
    with open(os.path.join(HERE, "e2e.env"), "w", encoding="utf-8") as f:
        f.write(f"E2E_EMAIL={admin_email}\nE2E_PASSWORD={admin_pw}\nE2E_NEW_PASSWORD={admin_pw}\n")
    print("Usuarios de prueba listos:", ", ".join(roles))


if __name__ == "__main__":
    main()
