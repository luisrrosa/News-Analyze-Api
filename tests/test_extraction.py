"""Regresión del extractor contra el PDF de referencia (Plan V3.0 §18).

Las cifras esperadas son las medidas del corpus (Plan V3.0 §2 y Observaciones §3). Si el extractor cambia
y alguna baja, la prueba falla y bloquea la fusión.
"""
import collections
import itertools
import os
from pathlib import Path

import pytest

from app.extraction.extractor import extract
from app.extraction.simhash import RELATED_MAX, hamming, simhash64

REFERENCE_PDF = Path(os.environ.get(
    "REFERENCE_PDF",
    Path(__file__).resolve().parents[2] / "newsAnalyze" / "storage" / "reference" / "RESUMEN_PRENSA_05_10_2026.pdf",
))
pytestmark = pytest.mark.skipif(not REFERENCE_PDF.exists(), reason=f"PDF de referencia no disponible: {REFERENCE_PDF}")


@pytest.fixture(scope="module")
def result():
    return extract(REFERENCE_PDF.read_bytes())


def count(items, attr):
    return dict(collections.Counter(getattr(i, attr) for i in items))


def test_document_metadata(result):
    assert result.page_count == 39
    assert result.document_type == "RESUMEN DE PRENSA DIARIA"
    assert result.summary_date == "2026-10-05"
    assert result.footer_document_number == "227"
    assert result.warnings == []  # pie encontrado en las 39 páginas, sin texto huérfano


def test_items_segmented_without_loss(result):
    assert len(result.items) == 156
    assert [i.news_index for i in result.items] == list(range(1, 157))
    assert count(result.items, "content_type") == {"Noticia": 118, "Artículo": 33, "Editorial": 5}


def test_media_sections_destaque(result):
    assert count(result.items, "media_name") == {
        "Diario Libre": 29, "Listín Diario": 19, "Hoy": 30, "El Día": 21, "El Caribe": 33, "El Nuevo Diario": 24}
    assert count(result.items, "article_section") == {
        "Nacionales": 73, "Opinión": 38, "Económicas": 20, "Deportes": 18, "Sociales": 7}
    assert count(result.items, "source_destaque") == {"Medio": 66, "Alto": 63, "Bajo": 20, "Muy Alto": 7}
    assert all(i.media_raw for i in result.items)  # el texto original del medio se conserva
    assert sum(1 for i in result.items if i.media_raw == "El Día (Diario)") == 21


def test_headlines_not_contaminated(result):
    for i in result.items:
        assert i.title, i.news_index
        assert i.title == i.title.upper(), (i.news_index, i.title)
        assert not i.body.startswith(i.title), i.news_index


def test_headers_footers_not_in_bodies(result):
    for i in result.items:
        assert "DIECOM" not in i.text_original
        assert "RESUMEN DE PRENSA DIARIA" not in i.text_original
        assert not any(m in i.text_original for m in (" - Tipo Noticia - ", " - Destaque "))


def test_continuation_across_pages(result):
    multi = [i for i in result.items if len(i.pdf_page_indices) > 1]
    assert len(multi) == 32
    # Ítem 4 (Latinobarómetro) continúa en la pág. 2 con "69 % de los dominicanos…"
    item4 = result.items[3]
    assert item4.pdf_page_indices == [1, 2] and "69 % de los dominicanos" in item4.body


def test_urls_authors_and_page_ranges(result):
    assert sum(1 for i in result.items if i.url) == 118
    assert sum(1 for i in result.items if not i.author_raw) == 82
    assert all(" " not in i.url for i in result.items if i.url)
    ranged = {i.news_index: (i.source_page_number, i.source_page_to) for i in result.items if i.source_page_to}
    assert ranged == {55: (8, 9), 103: (4, 5), 134: (8, 9)}
    assert result.items[133].author_raw.endswith("EDIBEL MONTERO DE LOS SANTOS")  # autor partido en dos líneas


def test_dates_never_confused(result):
    # La fecha del resumen no es la de publicación: en el corpus conviven 3, 4 y 5 de octubre.
    from_url = {i.publication_date for i in result.items if i.date_basis == "from_url"}
    assert {"2026-10-03", "2026-10-04", "2026-10-05"} <= from_url
    inferred = [i for i in result.items if i.date_basis == "from_summary"]
    assert all("publication_date_inferred" in i.flags for i in inferred)


def test_quality_does_not_trigger_ocr(result):
    assert not any(i.quality["text_quality_failed"] for i in result.items)
    assert not any(p["text_quality_failed"] for p in result.page_quality)
    assert sum(1 for i in result.items if "short_item" in i.flags) == 4


def test_simhash_known_related_pair(result):
    hashes = [(i.news_index, simhash64(i.text_original)) for i in result.items]
    close = [(a, b, hamming(ha, hb)) for (a, ha), (b, hb) in itertools.combinations(hashes, 2)
             if hamming(ha, hb) <= RELATED_MAX]
    assert len(close) == 1 and close[0][:2] == (102, 147)  # Banreservas/Mastercard en dos medios
