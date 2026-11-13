"""Ruta de excepción OCR: PDF escaneado (solo imagen) sintético."""
import pymupdf
import pytest

from app.extraction import ocr
from app.extraction.extractor import extract

LINES = [
    "Diario Libre - Nacionales - Tipo Noticia - Destaque Alto - Pág. 4 Juan Pérez",
    "EL GOBIERNO ANUNCIA UN PLAN NACIONAL DE AGUA POTABLE",
    "El Poder Ejecutivo presentó este lunes un plan para ampliar el servicio de agua",
    "potable en todas las provincias del país durante los próximos cuatro años.",
]


def scanned_pdf() -> bytes:
    """Crea una página digital, la rasteriza y la vuelve a meter como imagen sola (sin capa de texto)."""
    src = pymupdf.open()
    page = src.new_page(width=612, height=792)
    y = 120
    for text in LINES:
        page.insert_text((40, y), text, fontsize=11, fontname="helv")
        y += 22
    pix = page.get_pixmap(dpi=300)
    out = pymupdf.open()
    scan = out.new_page(width=612, height=792)
    scan.insert_image(scan.rect, pixmap=pix)
    data = out.tobytes()
    src.close()
    out.close()
    return data


def test_scanned_page_is_detected():
    doc = pymupdf.open(stream=scanned_pdf(), filetype="pdf")
    assert ocr.is_scanned(doc[0])
    doc.close()


def test_scanned_without_ocr_fails_explicitly(monkeypatch):
    monkeypatch.setenv("OCR_ENGINE", "none")
    with pytest.raises(ocr.OcrUnavailable, match="escaneado.*OCR_ENGINE=none"):
        extract(scanned_pdf())


@pytest.mark.skipif(not ocr.ocr_available(), reason="Tesseract con idioma spa no instalado (se prueba en la imagen Docker)")
def test_scanned_with_ocr_extracts_and_flags_for_review():
    result = extract(scanned_pdf())
    assert result.items, result.warnings
    item = result.items[0]
    assert "ocr_text" in item.flags
    assert item.media_name == "Diario Libre"
    assert "AGUA" in item.text_original.upper()
    assert result.page_quality[0]["method"] == "ocr"
    assert any("OCR" in w for w in result.warnings)
