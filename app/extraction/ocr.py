"""OCR como ruta de excepción para páginas escaneadas (Plan V3.0 §5.6).

La extracción normal es digital (PyMuPDF). Solo una página con muy poco texto y con imágenes se considera
escaneada y pasa por OCR; las noticias que salen de ahí se marcan `ocr_text` y van a revisión humana.

Motor configurable con OCR_ENGINE: `tesseract` (requiere el binario y el idioma `spa`, incluidos en la imagen
Docker) o `none`. Si no hay motor, la página no se inventa: se avisa y, si el documento queda sin noticias,
el procesamiento falla con un mensaje explícito.
"""
from __future__ import annotations

import logging
import os
import shutil
from functools import lru_cache

import pymupdf

log = logging.getLogger("ocr")

OCR_MIN_CHARS = 40   # menos caracteres digitales que esto + imágenes = página escaneada
OCR_DPI = 300
OCR_LANGUAGE = "spa"


class OcrUnavailable(RuntimeError):
    pass


def _engine() -> str:
    return os.environ.get("OCR_ENGINE", "tesseract").strip().lower()


@lru_cache(maxsize=1)
def _tessdata() -> str | None:
    """Ruta de tessdata con el idioma español, o None si Tesseract no está instalado."""
    if not shutil.which("tesseract"):
        return None
    try:
        path = pymupdf.get_tessdata()
    except Exception:
        return None
    return path if path and os.path.exists(os.path.join(path, f"{OCR_LANGUAGE}.traineddata")) else None


def ocr_available() -> bool:
    return _engine() == "tesseract" and _tessdata() is not None


def unavailable_reason() -> str:
    if _engine() != "tesseract":
        return f"OCR desactivado (OCR_ENGINE={_engine() or 'vacío'})"
    if not shutil.which("tesseract"):
        return "Tesseract no está instalado en el servidor"
    return f"Tesseract no tiene el idioma '{OCR_LANGUAGE}' instalado"


def is_scanned(page: pymupdf.Page) -> bool:
    return len(page.get_text().strip()) < OCR_MIN_CHARS and bool(page.get_images(full=False))


def ocr_textpage(page: pymupdf.Page) -> pymupdf.TextPage:
    if not ocr_available():
        raise OcrUnavailable(unavailable_reason())
    return page.get_textpage_ocr(language=OCR_LANGUAGE, dpi=OCR_DPI, full=True, tessdata=_tessdata())
