"""Render the generated Markdown to a PDF with ReportLab.

Supports the subset of Markdown this app produces: headings, paragraphs,
bullet/numbered lists, blockquotes, fenced code blocks, horizontal rules,
simple pipe tables, and inline bold/italic/code/links.
"""

from __future__ import annotations

import re
from html import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch

from .diagrams import mermaid_to_flowable
from .textutil import latin1_safe
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

ACCENT = colors.HexColor("#1f4e79")
CODE_BG = colors.HexColor("#f4f4f6")
MUTED = colors.HexColor("#555555")
STRONG = colors.HexColor("#173e60")   # highlighted key terms
MARGIN = 0.85 * inch
CONTENT_WIDTH = LETTER[0] - 2 * MARGIN


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    body = ParagraphStyle(
        "Body", parent=base["BodyText"], fontName="Helvetica", fontSize=10.5,
        leading=15.5, spaceAfter=8, alignment=TA_LEFT,
    )
    return {
        "title": ParagraphStyle(
            "DocTitle", parent=body, fontName="Helvetica-Bold", fontSize=22,
            leading=27, textColor=ACCENT, spaceAfter=6,
        ),
        "meta": ParagraphStyle(
            "Meta", parent=body, fontSize=9, leading=13, textColor=MUTED, spaceAfter=4
        ),
        "h2": ParagraphStyle(
            "H2", parent=body, fontName="Helvetica-Bold", fontSize=15, leading=20,
            textColor=ACCENT, spaceBefore=16, spaceAfter=6,
        ),
        "h3": ParagraphStyle(
            "H3", parent=body, fontName="Helvetica-Bold", fontSize=12.5, leading=17,
            spaceBefore=11, spaceAfter=4,
        ),
        "h4": ParagraphStyle(
            "H4", parent=body, fontName="Helvetica-BoldOblique", fontSize=11,
            leading=15, spaceBefore=9, spaceAfter=3,
        ),
        "body": body,
        "bullet": ParagraphStyle("Bullet", parent=body, spaceAfter=3),
        "quote": ParagraphStyle(
            "Quote", parent=body, leftIndent=16, textColor=MUTED,
            fontName="Helvetica-Oblique", borderPadding=0,
        ),
        "code": ParagraphStyle(
            "Code", parent=body, fontName="Courier", fontSize=9, leading=12.5,
            backColor=CODE_BG, borderPadding=6, spaceBefore=4, spaceAfter=8,
        ),
    }


_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def inline(text: str) -> str:
    """Convert inline Markdown to ReportLab's mini-HTML."""
    text = latin1_safe(text)
    placeholders: list[str] = []

    def stash(match: re.Match[str]) -> str:
        placeholders.append(
            f'<font face="Courier" backColor="#f0f0f2">'
            f"{escape(match.group(1))}</font>"
        )
        return f"\x00{len(placeholders) - 1}\x00"

    text = _INLINE_CODE.sub(stash, text)
    text = escape(text)
    text = _LINK.sub(r'<link href="\2" color="#1f4e79"><u>\1</u></link>', text)
    text = _BOLD.sub(rf'<b><font color="#{STRONG.hexval()[2:]}">\1</font></b>', text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: placeholders[int(m.group(1))], text)


def _table(rows: list[list[str]], st: dict[str, ParagraphStyle]) -> Table:
    head_style = ParagraphStyle("TH", parent=st["body"], fontName="Helvetica-Bold",
                                fontSize=9.5, leading=13, spaceAfter=0)
    cell_style = ParagraphStyle("TD", parent=st["body"], fontSize=9.5, leading=13,
                                spaceAfter=0)
    data = [[Paragraph(inline(c), head_style if i == 0 else cell_style) for c in row]
            for i, row in enumerate(rows)]
    table = Table(data, hAlign="LEFT", repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef5")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b8c4d4")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def markdown_to_flowables(
    md: str, st: dict[str, ParagraphStyle], content_width: float = CONTENT_WIDTH
) -> list:
    flow: list = []
    lines = md.splitlines()
    i = 0
    in_meta = False

    def flush_list(items: list[str], ordered: bool) -> None:
        if not items:
            return
        flow.append(ListFlowable(
            [ListItem(Paragraph(inline(t), st["bullet"]), leftIndent=18) for t in items],
            bulletType="1" if ordered else "bullet",
            bulletFontSize=10, start="1" if ordered else None,
            leftIndent=18, spaceAfter=8,
        ))

    bullets: list[str] = []
    ordered = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            flush_list(bullets, ordered); bullets = []
            lang = stripped[3:].strip().lower()
            i += 1
            code: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            source = "\n".join(code)

            if lang in ("mermaid", "diagram"):
                diagram = mermaid_to_flowable(source, content_width)
                if diagram is not None:
                    flow.append(Spacer(1, 4))
                    flow.append(KeepTogether(diagram))
                    flow.append(Spacer(1, 10))
                    continue  # rendered as a picture; skip the source dump

            body = escape(latin1_safe(source)).replace(" ", "&nbsp;").replace("\n", "<br/>")
            flow.append(Paragraph(body, st["code"]))
            continue

        if not stripped:
            flush_list(bullets, ordered); bullets = []
            i += 1
            continue

        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            flush_list(bullets, ordered); bullets = []
            flow.append(Spacer(1, 4))
            flow.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#c8c8c8")))
            flow.append(Spacer(1, 6))
            i += 1
            continue

        if stripped == "<!-- pagebreak -->":
            flow.append(PageBreak())
            i += 1
            continue

        heading = re.match(r"(#{1,6})\s+(.*)", stripped)
        if heading:
            flush_list(bullets, ordered); bullets = []
            level, text = len(heading.group(1)), heading.group(2)
            if level == 1:
                flow.append(Paragraph(inline(text), st["title"]))
                flow.append(HRFlowable(width="100%", thickness=1.2, color=ACCENT,
                                       spaceBefore=2, spaceAfter=10))
                in_meta = True
            else:
                flow.append(Paragraph(inline(text), st.get(f"h{min(level, 4)}", st["h4"])))
            i += 1
            continue

        # pipe table
        if stripped.startswith("|") and i + 1 < len(lines) and re.fullmatch(
            r"\|[\s:\-|]+\|", lines[i + 1].strip()
        ):
            flush_list(bullets, ordered); bullets = []
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                row = lines[i].strip().strip("|")
                if not re.fullmatch(r"[\s:\-|]+", row):
                    rows.append([c.strip() for c in row.split("|")])
                i += 1
            if rows:
                flow.append(_table(rows, st))
                flow.append(Spacer(1, 8))
            continue

        bullet = re.match(r"[-*+]\s+(.*)", stripped)
        number = re.match(r"(\d+)[.)]\s+(.*)", stripped)
        if bullet or number:
            want_ordered = bool(number)
            if bullets and want_ordered != ordered:
                flush_list(bullets, ordered)
                bullets = []
            ordered = want_ordered
            bullets.append(bullet.group(1) if bullet else number.group(2))
            i += 1
            continue
        flush_list(bullets, ordered); bullets = []

        if stripped.startswith(">"):
            flow.append(Paragraph(inline(stripped.lstrip("> ")), st["quote"]))
            i += 1
            continue

        # paragraph: gather until blank line
        para = [stripped]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(
            r"(#{1,6}\s|[-*+]\s|\d+[.)]\s|```|>|\|)", lines[i].strip()
        ):
            para.append(lines[i].strip())
            i += 1
        text = "\n".join(para)
        style = st["meta"] if in_meta else st["body"]
        flow.append(Paragraph(inline(text).replace("\n", "<br/>" if in_meta else " "), style))
        if in_meta:
            flow.append(Spacer(1, 6))
            in_meta = False

    flush_list(bullets, ordered)
    return flow


def _decorate(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(MUTED)
    canvas.drawRightString(LETTER[0] - inch * 0.75, inch * 0.5, str(canvas.getPageNumber()))
    title = latin1_safe(getattr(doc, "_doc_title", ""))
    if title and canvas.getPageNumber() > 1:
        canvas.drawString(inch * 0.85, inch * 0.5, title[:90])
    canvas.restoreState()


def write_pdf(markdown: str, path: str, title: str = "") -> str:
    doc = SimpleDocTemplate(
        path, pagesize=LETTER,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=0.8 * inch, bottomMargin=0.8 * inch,
        title=title or None, author="videoCaptionToTextFile",
    )
    doc._doc_title = title
    doc.build(markdown_to_flowables(markdown, _styles(), doc.width),
              onFirstPage=_decorate, onLaterPages=_decorate)
    return path
