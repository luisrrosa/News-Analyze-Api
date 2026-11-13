"""Reporte Presidencial en PDF formal (Plan V3.0 §16, decisión D-08).

Contenido (decisión del propietario): resumen ejecutivo en cifras, titulares de mayor destaque por medio,
distribuciones (medio, sección, tipo, destaque) y cobertura compartida (mismo hecho en varios medios).
El triage está en modo simulación: el reporte no incluye valoraciones ni prioridades de IA.
"""
import html
import io
from datetime import date, datetime, timezone
from pathlib import Path

import pymupdf

from app.core.db import get_db
from app.services.stats import period_stats

LOGO = Path(__file__).resolve().parents[1] / "assets" / "news-analyze-logo.jpg"
MAX_HEADLINES = 200
MAX_SHARED = 100
MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
          "noviembre", "diciembre"]
NAVY = (15 / 255, 41 / 255, 66 / 255)

CSS = """
* { font-family: sans-serif; }
body { font-size: 9.5pt; color: #1e293b; }
h1 { font-size: 17pt; color: #0f2942; margin: 0 0 4pt 0; }
h2 { font-size: 12pt; color: #0f2942; border-bottom: 1.2pt solid #0f2942; padding-bottom: 2pt; margin: 14pt 0 6pt 0; }
h3 { font-size: 10.5pt; color: #0f2942; margin: 10pt 0 4pt 0; }
p { margin: 0 0 5pt 0; line-height: 1.35; }
.muted { color: #64748b; font-size: 8.5pt; }
.note { border-left: 2pt solid #0f2942; padding: 2pt 6pt; font-size: 8.5pt; color: #334155; }
table { width: 100%; border-collapse: collapse; margin: 2pt 0 8pt 0; }
th { color: #0f2942; font-weight: bold; font-size: 8.5pt; text-align: left; padding: 3pt 4pt; border-bottom: 1.2pt solid #0f2942; }
td { border-bottom: 0.5pt solid #e2e8f0; padding: 3pt 4pt; font-size: 8.5pt; vertical-align: top; }
td.num { text-align: right; }
.kpi td { font-size: 9.5pt; }
.title { font-weight: bold; color: #0f172a; }
.tag { color: #9a3412; font-weight: bold; }
a { color: #1d4ed8; }
"""


def _e(v) -> str:
    return html.escape(str(v)) if v is not None else ""


def _long_date(iso: str | None) -> str:
    if not iso:
        return "—"
    d = date.fromisoformat(iso[:10])
    return f"{d.day} de {MONTHS[d.month - 1]} de {d.year}"


def _period(d1: str, d2: str) -> str:
    return _long_date(d1) if d1 == d2 else f"{_long_date(d1)} al {_long_date(d2)}"


def _dist_table(title: str, data: dict[str, int], total: int) -> str:
    if not data:
        return ""
    rows = "".join(
        f"<tr><td>{_e(k)}</td><td class='num'>{v}</td><td class='num'>{(v / total * 100 if total else 0):.1f} %</td></tr>"
        for k, v in data.items())
    return f"<h3>{_e(title)}</h3><table><tr><th>{_e(title)}</th><th>Noticias</th><th>%</th></tr>{rows}</table>"


async def _headlines(match: dict) -> tuple[list[dict], int]:
    q = {**match, "source_destaque": {"$in": ["Muy Alto", "Alto"]}, "is_duplicate": False}
    total = await get_db()["news"].count_documents(q)
    items = [n async for n in get_db()["news"].find(q, projection={
        "title": 1, "media_name": 1, "media_raw": 1, "article_section": 1, "content_type": 1, "source_destaque": 1,
        "source_page_number": 1, "source_page_to": 1, "author_raw": 1, "url": 1, "summary_date": 1, "pdf_page_indices": 1,
    }).sort([("source_destaque", -1), ("media_name", 1), ("summary_date", -1), ("news_index", 1)]).limit(MAX_HEADLINES)]
    # Un bloque por medio; dentro de cada medio, "Muy Alto" antes que "Alto".
    items.sort(key=lambda n: (n.get("media_name") or "~", 0 if n.get("source_destaque") == "Muy Alto" else 1,
                              n.get("summary_date") or ""))
    return items, total


async def _shared(match: dict) -> list[tuple[dict, list[dict]]]:
    col = get_db()["news"]
    groups: list[tuple[dict, list[dict]]] = []
    seen: set[str] = set()
    proj = {"id": 1, "title": 1, "media_name": 1, "summary_date": 1, "related_news_ids": 1, "canonical_news_id": 1,
            "is_duplicate": 1, "duplicate_similarity": 1}
    async for n in col.find({**match, "$or": [{"related_news_ids.0": {"$exists": True}}, {"is_duplicate": True}]},
                            projection=proj).limit(MAX_SHARED * 3):
        if n["id"] in seen:
            continue
        other_ids = n.get("related_news_ids") or ([n["canonical_news_id"]] if n.get("canonical_news_id") else [])
        others = [o async for o in col.find({"id": {"$in": other_ids}}, projection=proj)]
        if not others:
            continue
        seen.add(n["id"])
        seen.update(o["id"] for o in others)
        groups.append((n, others))
        if len(groups) >= MAX_SHARED:
            break
    return groups


async def build_presidential_report(date_from: str, date_to: str, generated_by: str) -> bytes:
    match = {"summary_date": {"$gte": date_from, "$lte": date_to}}
    stats = await period_stats(match, match)
    headlines, headlines_total = await _headlines(match)
    shared = await _shared(match)
    total = stats["total_news"]
    now = datetime.now(timezone.utc)

    parts = [f"""
<h1>Reporte Presidencial de Prensa</h1>
<p><b>Período:</b> {_e(_period(date_from, date_to))}</p>
<p class='muted'>Generado el {_e(_long_date(now.date().isoformat()))} a las {now.strftime('%H:%M')} UTC por {_e(generated_by)} ·
Fuente: {stats['documents']} resumen(es) de prensa procesado(s){(' (' + ', '.join(_long_date(d) for d in stats['days'][:6]) + ('…' if len(stats['days']) > 6 else '') + ')') if stats['days'] else ''}.</p>
<p class='note'>Este reporte presenta información extraída de forma determinista de los resúmenes de prensa. El «destaque» es el
asignado por cada medio, no una valoración del sistema. El triage y la valoración por inteligencia artificial están en modo
de simulación y no forman parte de este documento. Uso exclusivamente institucional.</p>
"""]

    if total == 0:
        parts.append("<h2>Sin datos</h2><p>No hay noticias procesadas en el período seleccionado.</p>")
    else:
        mv = stats["by_destaque"]
        parts.append(f"""
<h2>1. Resumen ejecutivo en cifras</h2>
<table class='kpi'>
<tr><th>Indicador</th><th>Valor</th></tr>
<tr><td>Resúmenes de prensa procesados</td><td class='num'>{stats['documents']}</td></tr>
<tr><td>Páginas analizadas</td><td class='num'>{stats['pages']}</td></tr>
<tr><td>Piezas periodísticas</td><td class='num'>{total}</td></tr>
<tr><td>Medios de comunicación</td><td class='num'>{len(stats['by_media'])}</td></tr>
<tr><td>Destaque Muy Alto / Alto</td><td class='num'>{mv.get('Muy Alto', 0)} / {mv.get('Alto', 0)}</td></tr>
<tr><td>Noticias, artículos y editoriales</td><td class='num'>{stats['by_type'].get('Noticia', 0)} / {stats['by_type'].get('Artículo', 0)} / {stats['by_type'].get('Editorial', 0)}</td></tr>
<tr><td>Piezas que continúan en varias páginas</td><td class='num'>{stats['multi_page']}</td></tr>
<tr><td>Cobertura compartida (relacionadas / duplicadas)</td><td class='num'>{stats['related']} / {stats['duplicates']}</td></tr>
<tr><td>Con enlace a la publicación original</td><td class='num'>{stats['with_url']}</td></tr>
</table>
""")

        parts.append("<h2>2. Titulares de mayor destaque</h2>")
        parts.append("<p class='muted'>Piezas con destaque Muy Alto y Alto asignado por el medio (sin duplicados), agrupadas por medio.</p>")
        current_media = None
        for n in headlines:
            media = n.get("media_name") or n.get("media_raw") or "Medio sin resolver"
            if media != current_media:
                if current_media is not None:
                    parts.append("</table>")
                parts.append(f"<h3>{_e(media)}</h3><table><tr><th>Destaque</th><th>Titular</th><th>Sección · Pág.</th><th>Fecha</th></tr>")
                current_media = media
            page = f"{n.get('source_page_number')}" + (f"-{n['source_page_to']}" if n.get("source_page_to") else "")
            link = f" <a href='{_e(n['url'])}'>enlace</a>" if n.get("url") else ""
            author = f"<br/><span class='muted'>{_e(n['author_raw'])}</span>" if n.get("author_raw") else ""
            parts.append(
                f"<tr><td class='tag'>{_e(n.get('source_destaque'))}</td><td class='title'>{_e(n.get('title'))}{author}</td>"
                f"<td>{_e(n.get('article_section'))} · {_e(n.get('content_type'))} · Pág. {_e(page)}{link}</td>"
                f"<td>{_e(n.get('summary_date'))}</td></tr>")
        if current_media is not None:
            parts.append("</table>")
        if headlines_total > len(headlines):
            parts.append(f"<p class='muted'>Se muestran {len(headlines)} de {headlines_total} titulares; acote el período para verlos todos.</p>")
        if not headlines:
            parts.append("<p>No hay piezas con destaque Muy Alto o Alto en el período.</p>")

        parts.append("<h2>3. Distribuciones</h2>")
        parts.append(_dist_table("Medio", stats["by_media"], total))
        parts.append(_dist_table("Sección", stats["by_section"], total))
        parts.append(_dist_table("Tipo de contenido", stats["by_type"], total))
        parts.append(_dist_table("Destaque del medio", stats["by_destaque"], total))

        parts.append("<h2>4. Cobertura compartida</h2>")
        parts.append("<p class='muted'>El mismo hecho publicado por más de un medio (similitud de texto SimHash). "
                     "«Duplicada» = reproducción casi idéntica; «relacionada» = mismo hecho con otra redacción.</p>")
        if shared:
            parts.append("<table><tr><th>Pieza</th><th>Aparece también en</th></tr>")
            for n, others in shared:
                rel = "; ".join(f"<b>{_e(o.get('media_name'))}</b>: {_e(o.get('title'))}" for o in others)
                kind = "duplicada" if n.get("is_duplicate") else "relacionada"
                parts.append(f"<tr><td><b>{_e(n.get('media_name'))}</b>: <span class='title'>{_e(n.get('title'))}</span>"
                             f"<br/><span class='muted'>{kind} · {_e(n.get('summary_date'))}</span></td><td>{rel}</td></tr>")
            parts.append("</table>")
        else:
            parts.append("<p>No se detectó cobertura compartida en el período.</p>")

    story = pymupdf.Story(html="".join(parts), user_css=CSS)
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    page_rect = pymupdf.paper_rect("letter")
    content = page_rect + (50, 88, -50, -56)
    more = True
    while more:
        device = writer.begin_page(page_rect)
        more, _ = story.place(content)
        story.draw(device)
        writer.end_page()
    writer.close()

    # Encabezado y pie institucionales en cada página.
    doc = pymupdf.open(stream=buf.getvalue(), filetype="pdf")
    total_pages = doc.page_count
    period_text = _period(date_from, date_to)
    for i, page in enumerate(doc, start=1):
        w, h = page.rect.width, page.rect.height
        if LOGO.exists():
            page.insert_image(pymupdf.Rect(50, 26, 210, 61), filename=str(LOGO), keep_proportion=True)
        page.insert_text((w - 50 - pymupdf.get_text_length("REPORTE PRESIDENCIAL DE PRENSA", "hebo", 9), 40),
                         "REPORTE PRESIDENCIAL DE PRENSA", fontname="hebo", fontsize=9, color=NAVY)
        page.insert_text((w - 50 - pymupdf.get_text_length(period_text, "helv", 8), 53), period_text,
                         fontname="helv", fontsize=8, color=(0.39, 0.45, 0.55))
        page.draw_line((50, 70), (w - 50, 70), color=NAVY, width=1.2)
        page.draw_line((50, h - 44), (w - 50, h - 44), color=(0.8, 0.84, 0.88), width=0.6)
        footer = "Presidencia de la República Dominicana · DIECOM · Uso exclusivamente institucional"
        page.insert_text((50, h - 30), footer, fontname="helv", fontsize=7.5, color=(0.39, 0.45, 0.55))
        num = f"Página {i} de {total_pages}"
        page.insert_text((w - 50 - pymupdf.get_text_length(num, "helv", 7.5), h - 30), num,
                         fontname="helv", fontsize=7.5, color=(0.39, 0.45, 0.55))
    doc.set_metadata({"title": f"Reporte Presidencial de Prensa — {period_text}", "author": "NEWS ANALYZE",
                      "creator": "NEWS ANALYZE", "subject": "Reporte institucional de prensa"})
    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return out
