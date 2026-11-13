"""OCR como ruta de excepción para páginas escaneadas (Plan V3.0 §5.6).

La extracción normal es digital (PyMuPDF). Solo una página con muy poco texto y con imágenes se considera
escaneada y pasa por OCR; las noticias que salen de ahí se marcan `ocr_text` y van a revisión humana.

Motor configurable con OCR_ENGINE: `tesseract` o `none`. MuPDF incluye el motor; solo hacen falta los modelos
de idioma (`spa.traineddata`) en TESSDATA_PREFIX o en la instalación de Tesseract (la imagen Docker los trae). Si no hay motor, la página no se inventa: se avisa y, si el documento queda sin noticias,
el procesamiento falla con un mensaje explícito.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache

import pymupdf

log = logging.getLogger("ocr")

OCR_MIN_CHARS = 40   # menos caracteres digitales que esto + imágenes = página escaneada
OCR_DPI = 300
OCR_LANGUAGE = "spa"


class OcrUnavailable(RuntimeError):
    pass


def _settings():
    from app.core.config import get_settings
    try:
        return get_settings()
    except Exception:  # sin configuración completa (p. ej. herramientas sueltas): solo variables de entorno
        return None


def _engine() -> str:
    s = _settings()
    return (os.environ.get("OCR_ENGINE") or (s.ocr_engine if s else "tesseract")).strip().lower()


@lru_cache(maxsize=4)
def _find_tessdata(explicit: str) -> str | None:
    candidates = [explicit] if explicit else []
    try:
        candidates.append(pymupdf.get_tessdata())
    except Exception:
        pass
    for path in candidates:
        if path and os.path.exists(os.path.join(path, f"{OCR_LANGUAGE}.traineddata")):
            return path
    return None


def _tessdata() -> str | None:
    """Carpeta tessdata con el idioma español, o None si no hay modelos instalados."""
    s = _settings()
    return _find_tessdata((s.tessdata_prefix if s else "") or os.environ.get("TESSDATA_PREFIX", ""))


def ocr_available() -> bool:
    return _engine() == "tesseract" and _tessdata() is not None


def unavailable_reason() -> str:
    if _engine() != "tesseract":
        return f"OCR desactivado (OCR_ENGINE={_engine() or 'vacío'})"
    return f"no se encontró el modelo de idioma '{OCR_LANGUAGE}' de Tesseract (configure TESSDATA_PREFIX)"


def is_scanned(page: pymupdf.Page) -> bool:
    return len(page.get_text().strip()) < OCR_MIN_CHARS and bool(page.get_images(full=False))


def ocr_textpage(page: pymupdf.Page) -> pymupdf.TextPage:
    if not ocr_available():
        raise OcrUnavailable(unavailable_reason())
    return page.get_textpage_ocr(language=OCR_LANGUAGE, dpi=OCR_DPI, full=True, tessdata=_tessdata())
