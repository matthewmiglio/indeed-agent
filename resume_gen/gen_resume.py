#!/usr/bin/env python3
"""Generate a resume PDF from JSON, matching the Matthew Miglio template.

Usage:  python gen_resume.py input.json [output.pdf]

JSON shape (see example_resume.json / fullstack_clone.json):
  name      : str                       -> centered bold header
  contact   : [ {text, url?, icon?} ]   -> pipe-separated line; url = link, icon = inline image
  title     : str                       -> bold role line
  summary   : str                       -> italic blurb (use **bold** inline)
  sections  : [ { heading, type, ... } ]
      type "entries": items = [ {place, date?, bullets?[]} ]   (jobs, projects, education)
      type "skills" : items = [ {label, value} ]

Any bullet / summary / value string may use **double-asterisk** for bold keywords.

Metrics matched to the source PDF: Calibri, 12pt body / 14pt headings / 17pt name,
0.5in side margins, bullet at 0.75in and text at 1.0in.
"""
import json
import re
import sys
from pathlib import Path

from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer, Table, TableStyle,
    HRFlowable,
)

# --- font: use real Calibri if present (Windows), else fall back to Helvetica ---
BASE, BOLD, ITAL, BITAL = "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique"
_fonts = {
    "Calibri": r"C:\Windows\Fonts\calibri.ttf",
    "Calibri-Bold": r"C:\Windows\Fonts\calibrib.ttf",
    "Calibri-Italic": r"C:\Windows\Fonts\calibrii.ttf",
    "Calibri-BoldItalic": r"C:\Windows\Fonts\calibriz.ttf",
}
try:
    for n, p in _fonts.items():
        pdfmetrics.registerFont(TTFont(n, p))
    pdfmetrics.registerFontFamily("Calibri", normal="Calibri", bold="Calibri-Bold",
                                  italic="Calibri-Italic", boldItalic="Calibri-BoldItalic")
    BASE, BOLD, ITAL, BITAL = "Calibri", "Calibri-Bold", "Calibri-Italic", "Calibri-BoldItalic"
except Exception:
    pass

GREY = colors.HexColor("#555555")
RULE = colors.HexColor("#cccccc")

S = {
    "name":    ParagraphStyle("name", fontName=BOLD, fontSize=17, leading=20,
                              alignment=TA_CENTER, spaceAfter=3),
    "contact": ParagraphStyle("contact", fontName=ITAL, fontSize=11, leading=14,
                              alignment=TA_CENTER, textColor=GREY, spaceAfter=9),
    "title":   ParagraphStyle("title", fontName=BOLD, fontSize=14, leading=17,
                              spaceBefore=2, spaceAfter=4),
    "summary": ParagraphStyle("summary", fontName=ITAL, fontSize=12, leading=15,
                              spaceAfter=5),
    "heading": ParagraphStyle("heading", fontName=BOLD, fontSize=14, leading=17,
                              spaceBefore=8, spaceAfter=5),
    "place":   ParagraphStyle("place", fontName=BASE, fontSize=12, leading=14),
    "date":    ParagraphStyle("date", fontName=ITAL, fontSize=12, leading=14,
                              alignment=TA_RIGHT, textColor=GREY),
    "bullet":  ParagraphStyle("bullet", fontName=BASE, fontSize=12, leading=15,
                              leftIndent=36, bulletIndent=18, bulletFontName=BASE,
                              bulletFontSize=9, spaceAfter=5),
}


def md(text):
    """Escape XML then turn **bold** into <b>bold</b> for reportlab markup."""
    esc = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)


def contact_line(items, base):
    parts = []
    for it in items:
        t = md(it["text"])
        if it.get("url"):
            seg = f'<a href="{it["url"]}" color="#1155cc"><u>{t}</u></a>'
        else:
            seg = t
        if it.get("icon"):
            p = Path(it["icon"])
            if not p.is_absolute():
                p = base / it["icon"]
            seg += f'&nbsp;<img src="{p.as_posix()}" width="10" height="10" valign="-2"/>'
        parts.append(seg)
    return " &nbsp;|&nbsp; ".join(parts)


def entry(item, width):
    """A place/date row (date right-aligned) followed by bullets."""
    flow = []
    place = Paragraph(md(item["place"]), S["place"])
    if item.get("date"):
        date = Paragraph(md(item["date"]), S["date"])
        t = Table([[place, date]], colWidths=[width - 2.1 * inch, 2.1 * inch])
        t.hAlign = "LEFT"
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        flow.append(t)
    else:
        flow.append(place)
    for b in item.get("bullets", []):
        flow.append(Paragraph(md(b), S["bullet"], bulletText="●"))
    flow.append(Spacer(1, 6))
    return flow


def build(data, out, base):
    doc = BaseDocTemplate(str(out), pagesize=LETTER,
                          leftMargin=0.5 * inch, rightMargin=0.5 * inch,
                          topMargin=0.58 * inch, bottomMargin=0.5 * inch)
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height,
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame])])
    width = doc.width
    story = [
        Paragraph(md(data["name"]), S["name"]),
        Paragraph(contact_line(data["contact"], base), S["contact"]),
        Paragraph(md(data["title"]), S["title"]),
        Paragraph(md(data["summary"]), S["summary"]),
        HRFlowable(width="100%", thickness=0.6, color=RULE,
                   spaceBefore=4, spaceAfter=2),
    ]
    last = len(data["sections"]) - 1
    for i, sec in enumerate(data["sections"]):
        story.append(Paragraph(md(sec["heading"]), S["heading"]))
        if sec.get("type") == "skills":
            for it in sec["items"]:
                story.append(Paragraph(
                    f'<b>{md(it["label"])}:</b> {md(it["value"])}',
                    S["bullet"], bulletText="●"))
        else:  # entries
            for it in sec["items"]:
                story.extend(entry(it, width))
        if i != last:  # no divider after the final section
            story.append(HRFlowable(width="100%", thickness=0.6, color=RULE,
                                    spaceBefore=4, spaceAfter=2))
    doc.build(story)


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: python gen_resume.py input.json [output.pdf]")
    inp = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else inp.with_suffix(".pdf")
    build(json.loads(inp.read_text(encoding="utf-8")), out, inp.parent)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
