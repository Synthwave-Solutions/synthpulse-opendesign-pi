#!/usr/bin/env python3
"""Build a PPTX or DOCX from an OpenDesign artifact screenshot.

Two input modes:
  - single tall PNG (document)  -> sliced into page-ratio chunks
  - a directory of slide-*.png  -> one PPTX slide / one DOCX page per image (deck)

PPTX: each deck slide becomes one full-bleed 16:9 slide; documents are sliced 16:9.
DOCX: one image per page.
Runs in the synthwave brand venv (python-pptx + python-docx + Pillow live there).

Usage: export_office.py <png|slidedir> <out.pptx|out.docx> [title]
"""
import sys
from pathlib import Path

from PIL import Image


def slide_images(src):
    """Return a list of per-slide PIL images if src is a deck dir, else None."""
    p = Path(src)
    if p.is_dir():
        pngs = sorted(p.glob("slide-*.png"))
        if pngs:
            return [Image.open(x).convert("RGB") for x in pngs]
    return None


def slices(img, ratio):
    """Yield PIL images: vertical slices of `img` with aspect `ratio` (w/h), last one padded white."""
    w, h = img.size
    chunk = int(w / ratio)
    y = 0
    while y < h:
        part = img.crop((0, y, w, min(y + chunk, h)))
        if part.size[1] < chunk:
            padded = Image.new("RGB", (w, chunk), "white")
            padded.paste(part, (0, 0))
            part = padded
        yield part
        y += chunk


def build_pptx(src, out, title):
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]
    tmpdir = Path(out).parent

    deck = slide_images(src)
    # deck: one real slide per image; document: slice the tall screenshot into 16:9
    parts = deck if deck is not None else list(slices(Image.open(src).convert("RGB"), 16 / 9))
    for i, part in enumerate(parts):
        p = tmpdir / f"_slice_{i}.png"
        part.save(p)
        slide = prs.slides.add_slide(blank)
        slide.shapes.add_picture(str(p), 0, 0, width=prs.slide_width, height=prs.slide_height)
        p.unlink()
    prs.save(out)


def build_docx(src, out, title):
    from docx import Document
    from docx.shared import Inches, Pt

    doc = Document()
    for s in doc.sections:
        s.top_margin = s.bottom_margin = Inches(0.5)
        s.left_margin = s.right_margin = Inches(0.5)
    if title:
        h = doc.add_heading(title, level=1)
        for run in h.runs:
            run.font.size = Pt(20)
    tmpdir = Path(out).parent

    deck = slide_images(src)
    # deck: one image per page; document: slice into printable-page chunks (~0.79 ratio)
    parts = deck if deck is not None else list(slices(Image.open(src).convert("RGB"), 7.5 / 9.2))
    for i, part in enumerate(parts):
        p = tmpdir / f"_dslice_{i}.png"
        part.save(p)
        doc.add_picture(str(p), width=Inches(7.5))
        p.unlink()
    doc.save(out)


def build_pdf(src, out, title):
    """Deck-aware PDF: one page per slide image (exact WYSIWYG, no drift)."""
    deck = slide_images(src)
    imgs = deck if deck is not None else [Image.open(src).convert("RGB")]
    imgs[0].save(out, "PDF", save_all=True, append_images=imgs[1:], resolution=96.0)


if __name__ == "__main__":
    png, out = sys.argv[1], sys.argv[2]
    title = sys.argv[3] if len(sys.argv) > 3 else ""
    if out.endswith(".pptx"):
        build_pptx(png, out, title)
    elif out.endswith(".docx"):
        build_docx(png, out, title)
    elif out.endswith(".pdf"):
        build_pdf(png, out, title)
    else:
        sys.exit("out must be .pptx, .docx or .pdf")
    print("ok", out)
