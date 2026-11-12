"""Extractor determinista de resúmenes de prensa (Plan V3.0 §5).

Capa determinista: extrae, suprime cabeceras y pies, segmenta, reconstruye continuaciones entre páginas,
identifica metadatos y mide calidad. No interpreta, no clasifica y no valora (eso es la capa semántica).

Formato observado en el PDF de referencia (generado con Word, texto digital):
  - Cabecera de página:  "RESUMEN DE PRENSA DIARIA" + "Lunes 5 octubre 2026"   (zona superior)
  - Pie de página:       "Presidencia de la República Dominicana – DIECOM / DMAII / IRP / 227 / 5/10/2026 / Pág. N de M"
  - Bloque del resumen:  "DIARIO LIBRE: PORTADA"  (negrita, centrado)
  - Cabecera de ítem:    "Diario Libre - Nacionales - Tipo Noticia - Destaque Medio - Pág. 4   Autor"  (10 pt)
  - Titular:             corrida en MAYÚSCULAS y negrita al inicio del primer párrafo, en la misma línea que el cuerpo
  - URL opcional al final, a veces partida en dos líneas.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlparse

import pymupdf

EXTRACTOR_VERSION = "pymupdf-det-1.0"

HEADER_RE = re.compile(
    r"^(?P<media>.+?)\s+-\s+(?P<section>.+?)\s+-\s+Tipo\s+(?P<type>Noticia|Art[íi]culo|Editorial)"
    r"\s+-\s+Destaque\s+(?P<destaque>Muy Alto|Alto|Medio|Bajo)\s+-\s+P[áa]g\.\s*(?P<page>\d+)(?:\s*[-–]\s*(?P<page_to>\d+))?\s*(?P<author>.*)$"
)
BLOCK_RE = re.compile(r"^(?P<media>.+?):\s*(?P<block>PORTADA|NOTICIA|EDITORIAL|ART[ÍI]CULO)$")
# Pie institucional tolerante a fragmentación ("I RP", "22 7"): se compara sin espacios.
FOOTER_RE = re.compile(
    r"DIECOM/DMAII/IRP/(?P<num>\d+)/(?P<d>\d{1,2})/(?P<m>\d{1,2})/(?P<y>\d{4})/P[áa]g\.(?P<p>\d+)de(?P<n>\d+)", re.I
)
URL_RE = re.compile(r"https?://\S+")
URL_DATE_RE = re.compile(r"/(?P<y>20\d{2})/(?P<m>\d{1,2})/(?P<d>\d{1,2})/")
SPANISH_MONTHS = {m: i for i, m in enumerate(
    ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
     "septiembre", "octubre", "noviembre", "diciembre"], start=1)}
SUMMARY_DATE_RE = re.compile(r"(?P<d>\d{1,2})\s+(?:de\s+)?(?P<m>[a-záéíóú]+)\s+(?:de\s+)?(?P<y>\d{4})", re.I)

# Tabla de alias de medios (Plan V3.0 §5.4). Un medio desconocido no se inventa: se marca media_unresolved.
MEDIA_ALIASES = {
    "diario libre": "Diario Libre",
    "listín diario": "Listín Diario", "listin diario": "Listín Diario",
    "hoy": "Hoy",
    "el día": "El Día", "el día (diario)": "El Día", "el dia": "El Día", "el dia (diario)": "El Día",
    "el caribe": "El Caribe",
    "el nuevo diario": "El Nuevo Diario",
}

TOP_BAND = 0.07      # franja superior (cabecera de página)
BOTTOM_BAND = 0.94   # franja inferior (pie de página)
SHORT_ITEM_CHARS = 200
MOJIBAKE = ("Ã", "Â", "â€", "�")


@dataclass
class Line:
    page: int
    y: float
    x: float
    text: str
    size: float
    spans: list[tuple[str, bool]]  # (texto, negrita)

    @property
    def bold_all(self) -> bool:
        return all(b for t, b in self.spans if t.strip())


@dataclass
class ExtractedItem:
    news_index: int
    header_raw: str
    media_raw: str
    media_name: str | None
    article_section: str
    content_type: str
    source_destaque: str
    source_page_number: int
    source_page_to: int | None
    author_raw: str | None
    document_block: str | None
    title: str
    body: str
    text_original: str
    url: str | None
    url_domain: str | None
    publication_date: str | None
    date_basis: str
    pdf_page_indices: list[int]
    word_count: int
    quality: dict
    segmentation_confidence: float
    flags: list[str] = field(default_factory=list)


@dataclass
class ExtractionResult:
    extractor_version: str
    page_count: int
    document_type: str | None
    summary_date: str | None
    footer_document_number: str | None
    items: list[ExtractedItem]
    page_quality: list[dict]
    warnings: list[str]


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text).replace(" ", " ")


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _page_lines(page: pymupdf.Page, pno: int) -> list[Line]:
    lines: list[Line] = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for ln in block["lines"]:
            spans = [(_normalize(s["text"]), bool(s["flags"] & 16) or "Bold" in s["font"]) for s in ln["spans"]]
            text = "".join(t for t, _ in spans)
            if not text.strip():
                continue
            size = max((s["size"] for s in ln["spans"] if s["text"].strip()), default=0)
            lines.append(Line(pno, ln["bbox"][1], ln["bbox"][0], text, size, spans))
    lines.sort(key=lambda l: (round(l.y, 1), l.x))
    return lines


def text_quality(text: str) -> dict:
    """Métrica compuesta (Plan V3.0 §5.6). El diccionario no se usa como criterio único."""
    n = len(text) or 1
    printable = sum(1 for c in text if c.isprintable() or c in "\n\t") / n
    mojibake = sum(text.count(m) for m in MOJIBAKE)
    tokens = re.findall(r"\w+", text)
    single = (sum(1 for t in tokens if len(t) == 1 and not t.isdigit()) / len(tokens)) if tokens else 0.0
    failed = printable < 0.90 or mojibake > 0
    score = max(0.0, min(1.0, printable - 0.1 * mojibake - max(0.0, single - 0.15)))
    return {
        "pct_printable": round(printable, 4),
        "mojibake_markers": mojibake,
        "pct_single_letter": round(single, 4),
        "extraction_quality": round(score, 4),
        "text_quality_failed": failed,
        "method": "digital",
    }


def _parse_summary_date(text: str) -> str | None:
    m = SUMMARY_DATE_RE.search(text)
    if not m:
        return None
    month = SPANISH_MONTHS.get(m["m"].lower())
    if not month:
        return None
    try:
        return date(int(m["y"]), month, int(m["d"])).isoformat()
    except ValueError:
        return None


def _resolve_media(raw: str) -> str | None:
    return MEDIA_ALIASES.get(_collapse(raw).lower())


class _Builder:
    def __init__(self, header: Line, match: re.Match, block: str | None, index: int):
        self.header = header
        self.match = match
        self.block = block
        self.index = index
        self.author_extra: list[str] = []
        self.title_parts: list[str] = []
        self.title_open = True
        self.body_parts: list[str] = []
        self.raw_lines: list[str] = []
        self.urls: list[str] = []
        self.pages: list[int] = [header.page]
        self.flags: list[str] = []
        self.in_url = False

    def add(self, line: Line) -> None:
        if line.page not in self.pages:
            self.pages.append(line.page)
        self.raw_lines.append(line.text)
        stripped = line.text.strip()

        # URL (posiblemente partida en varias líneas sin espacios internos)
        if URL_RE.match(stripped):
            self.urls.append(stripped)
            self.in_url = True
            self.title_open = False
            return
        if self.in_url and " " not in stripped and not self.body_parts_ends_sentence_needed(stripped):
            self.urls[-1] += stripped
            return
        self.in_url = False

        for text, bold in line.spans:
            if self.title_open and bold and text.strip():
                self.title_parts.append(text)
            elif text.strip():
                if self.title_open and self.title_parts:
                    self.title_open = False
                elif self.title_open and not self.title_parts:
                    self.title_open = False  # el párrafo no empieza en negrita: sin titular identificable
                self.body_parts.append(text)
            else:
                (self.title_parts if self.title_open and self.title_parts else self.body_parts).append(text)

    @staticmethod
    def body_parts_ends_sentence_needed(stripped: str) -> bool:
        # Una línea sin espacios tras una URL es su continuación salvo que parezca texto (termina en punto y tiene mayúsculas).
        return bool(re.match(r"^[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+[.,;:]$", stripped))

    def build(self, summary_date: str | None) -> ExtractedItem:
        m = self.match
        author = _collapse(" ".join([m["author"] or ""] + self.author_extra)) or None
        title = _collapse("".join(self.title_parts))
        body = _collapse("".join(self.body_parts))
        url = self.urls[0] if self.urls else None
        if len(self.urls) > 1:
            self.flags.append("multiple_urls")
        media_name = _resolve_media(m["media"])
        if media_name is None:
            self.flags.append("media_unresolved")
        if not title:
            self.flags.append("title_not_found")
        text_original = _collapse(f"{title} {body}")
        if len(text_original) < SHORT_ITEM_CHARS:
            self.flags.append("short_item")

        publication_date, basis = None, "unknown"
        if url and (dm := URL_DATE_RE.search(url)):
            try:
                publication_date = date(int(dm["y"]), int(dm["m"]), int(dm["d"])).isoformat()
                basis = "from_url"
            except ValueError:
                pass
        if publication_date is None and summary_date:
            publication_date, basis = summary_date, "from_summary"
            self.flags.append("publication_date_inferred")

        quality = text_quality(text_original)
        confidence = 1.0
        if "title_not_found" in self.flags:
            confidence -= 0.3
        if "short_item" in self.flags:
            confidence -= 0.1
        if quality["text_quality_failed"]:
            confidence -= 0.3
        if "media_unresolved" in self.flags:
            confidence -= 0.1

        return ExtractedItem(
            news_index=self.index,
            header_raw=_collapse(self.header.text),
            media_raw=_collapse(m["media"]),
            media_name=media_name,
            article_section=_collapse(m["section"]),
            content_type=m["type"].replace("Articulo", "Artículo"),
            source_destaque=m["destaque"],
            source_page_number=int(m["page"]),
            source_page_to=int(m["page_to"]) if m["page_to"] else None,
            author_raw=author,
            document_block=self.block,
            title=title,
            body=body,
            text_original=text_original,
            url=url,
            url_domain=urlparse(url).netloc.lower() if url else None,
            publication_date=publication_date,
            date_basis=basis,
            pdf_page_indices=self.pages,
            word_count=len(text_original.split()),
            quality=quality,
            segmentation_confidence=round(max(confidence, 0.0), 2),
            flags=self.flags,
        )


def extract(pdf_bytes: bytes) -> ExtractionResult:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        warnings: list[str] = []
        items: list[ExtractedItem] = []
        page_quality: list[dict] = []
        document_type = summary_date = footer_number = None
        current: _Builder | None = None
        block: str | None = None
        previous_was_header = False

        for pno, page in enumerate(doc, start=1):
            height = page.rect.height
            lines = _page_lines(page, pno)
            body_text: list[str] = []
            footer_found = False

            for line in lines:
                stripped = line.text.strip()
                rel_y = line.y / height

                # Cabecera de página (franja superior): metadatos del documento, no contenido.
                if rel_y < TOP_BAND:
                    if "RESUMEN" in stripped.upper() and document_type is None:
                        document_type = _collapse(stripped)
                    if summary_date is None:
                        summary_date = _parse_summary_date(stripped)
                    continue

                # Pie institucional: dos criterios (franja inferior + patrón tolerante).
                compact = re.sub(r"\s+", "", stripped)
                fm = FOOTER_RE.search(compact)
                if rel_y > BOTTOM_BAND and (fm or "DIECOM" in compact.upper() or "Presidencia" in stripped):
                    footer_found = True
                    if fm:
                        footer_number = footer_number or fm["num"]
                        if int(fm["p"]) != pno:
                            warnings.append(f"Pág. {pno}: el pie indica página {fm['p']}")
                    continue
                if rel_y > BOTTOM_BAND:
                    warnings.append(f"Pág. {pno}: texto en la franja inferior conservado como contenido: {stripped[:60]}")

                body_text.append(stripped)

                # Bloque del resumen (p. ej. "DIARIO LIBRE: PORTADA")
                if line.bold_all and (bm := BLOCK_RE.match(_collapse(stripped))):
                    block = bm["block"].replace("ARTICULO", "ARTÍCULO")
                    previous_was_header = False
                    continue

                # Cabecera de ítem
                hm = HEADER_RE.match(_collapse(stripped)) if not line.bold_all and line.size < 11 else None
                if hm:
                    if current:
                        items.append(current.build(summary_date))
                    current = _Builder(line, hm, block, len(items) + 1)
                    previous_was_header = True
                    continue

                # Autor partido en una segunda línea de la cabecera (mismo cuerpo de letra, sin negrita).
                if previous_was_header and current and line.size < 11 and not line.bold_all and not current.raw_lines:
                    current.author_extra.append(stripped)
                    continue
                previous_was_header = False

                if current is None:
                    warnings.append(f"Pág. {pno}: texto antes de la primera cabecera de ítem descartado: {stripped[:60]}")
                    continue
                current.add(line)

            if not footer_found:
                warnings.append(f"Pág. {pno}: no se encontró el pie institucional")
            page_quality.append({"page": pno, **text_quality(" ".join(body_text))})

        if current:
            items.append(current.build(summary_date))

        return ExtractionResult(
            extractor_version=EXTRACTOR_VERSION,
            page_count=doc.page_count,
            document_type=document_type,
            summary_date=summary_date,
            footer_document_number=footer_number,
            items=items,
            page_quality=page_quality,
            warnings=warnings,
        )
    finally:
        doc.close()
