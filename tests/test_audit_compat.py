"""La cadena de auditoría la escriben Next.js (lib/audit.ts) y FastAPI: el HMAC debe ser idéntico."""
import hashlib
import hmac

from app.core.audit import canonical_json

# Vector generado con la implementación TypeScript (canonicalJson + HMAC-SHA256, clave 'k' * 40).
RECORD = {
    "seq_num": 7, "event_id": "e-1", "timestamp_utc": "2026-11-12T10:00:00.123Z", "user_id": "usr_1",
    "user_email": "josé@presidencia.gob.do", "user_roles": ["admin", "editor"], "action": "DOC_PARSE_COMPLETE",
    "entity_type": "DOCUMENT", "entity_id": "DOC-1", "status": "SUCCESS", "ip_address": "10.0.0.1",
    "request_id": "req_1", "prev_record_hash": "GENESIS_NEWS_ANALIZE", "key_version": "v1",
    "details": {"filename": 'Resumen "Ñ"\n.pdf', "total_pages": 39, "nested": {"b": None, "a": True},
                "list": [1, "dos", {"z": 1, "y": 2}]},
}
EXPECTED_CANONICAL = (
    '{"action":"DOC_PARSE_COMPLETE","details":{"filename":"Resumen \\"Ñ\\"\\n.pdf","list":[1,"dos",{"y":2,"z":1}],'
    '"nested":{"a":true,"b":null},"total_pages":39},"entity_id":"DOC-1","entity_type":"DOCUMENT","event_id":"e-1",'
    '"ip_address":"10.0.0.1","key_version":"v1","prev_record_hash":"GENESIS_NEWS_ANALIZE","request_id":"req_1",'
    '"seq_num":7,"status":"SUCCESS","timestamp_utc":"2026-11-12T10:00:00.123Z","user_email":"josé@presidencia.gob.do",'
    '"user_id":"usr_1","user_roles":["admin","editor"]}'
)
EXPECTED_HMAC = "d3b7d8fd116128ed8a72e320b939f8bc9976380d1c17b5a2e12197a56ee00fde"


def test_canonical_json_matches_typescript():
    assert canonical_json(RECORD) == EXPECTED_CANONICAL


def test_hmac_matches_typescript():
    digest = hmac.new(b"k" * 40, canonical_json(RECORD).encode("utf-8"), hashlib.sha256).hexdigest()
    assert digest == EXPECTED_HMAC
