"""Proyección compatible con la interfaz actual (nombres de campo heredados de la app Next.js).

Se entrega el registro completo del modelo nuevo y, además, los alias que la interfaz ya consume
(medio, seccion, destaque, body_text, autor, pdf_pages…), hasta que la interfaz migre a los nombres del plan.
"""
# Triage sin evaluar hasta tener factores aprobados (modo simulación, decisión D-04).
TRIAGE_NOT_EVALUATED = {
    "status": "NOT_EVALUATED", "score": None, "alert_level": "SIN_EVALUAR", "factors": None,
    "applied_factors_count": 0, "rescaled": False, "presidential_brief": "", "primary_actors": [],
    "institutional_topic": None, "grounding_verified": False, "needs_review": False,
}


def news_out(n: dict) -> dict:
    n = {k: v for k, v in n.items() if k != "_id"}
    page = f"Pág. {n['source_page_number']}" + (f"-{n['source_page_to']}" if n.get("source_page_to") else "")
    return {
        **n,
        "medio": n.get("media_name") or n.get("media_raw"),
        "seccion": n.get("article_section"),
        "tipo": n.get("content_type"),
        "destaque": n.get("source_destaque"),
        "autor": n.get("author_raw"),
        "body_text": n.get("body"),
        "pdf_pages": n.get("pdf_page_indices", []),
        "urls": [n["url"]] if n.get("url") else [],
        "newspaper_page": page,
        "original_page_str": f"{page} (PDF Pág. {', '.join(map(str, n.get('pdf_page_indices', [])))})",
        "continues_next_page": len(n.get("pdf_page_indices", [])) > 1,
        "triage": TRIAGE_NOT_EVALUATED,
    }


def document_out(d: dict, by_type: dict | None = None, by_media: dict | None = None, multi_page: int | None = None) -> dict:
    d = {k: v for k, v in d.items() if k not in ("_id", "storage_key")}
    return {
        **d,
        "filename": d.get("file_name"),
        "sha256": d.get("file_hash"),
        "total_pages": d.get("page_count"),
        "total_items": d.get("items_total", 0),
        "processed_at": d.get("updated_at") if d.get("status", "").startswith("completed") else None,
        "items_by_type": by_type or {},
        "items_by_medio": by_media or {},
        "multi_page_items_count": multi_page or 0,
    }
