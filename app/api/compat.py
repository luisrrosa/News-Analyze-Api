"""Proyección pública de noticias y documentos (nombres del modelo del Plan V3.0, sin alias heredados).

Se añaden solo campos calculados útiles para la interfaz:
  source_page_label  «Pág. 8-9» (página del periódico)
  triage_status      «not_evaluated» hasta la capa de IA y la aprobación del triage (D-04)
"""
HIDDEN_NEWS = {"_id", "simhash", "quality"}
HIDDEN_DOCUMENT = {"_id", "storage_key", "page_quality"}


def news_out(n: dict) -> dict:
    out = {k: v for k, v in n.items() if k not in HIDDEN_NEWS}
    page = n.get("source_page_number")
    out["source_page_label"] = (f"Pág. {page}" + (f"-{n['source_page_to']}" if n.get("source_page_to") else "")) if page else None
    out["triage_status"] = "not_evaluated"
    return out


def document_out(d: dict, by_type: dict | None = None, by_media: dict | None = None, multi_page: int | None = None) -> dict:
    out = {k: v for k, v in d.items() if k not in HIDDEN_DOCUMENT}
    out["items_by_type"] = by_type or {}
    out["items_by_media"] = by_media or {}
    out["multi_page_items"] = multi_page or 0
    from app.storage import describe_source
    out["original_source"] = describe_source(d.get("storage_key", ""))
    return out
