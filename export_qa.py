#!/usr/bin/env python3
"""export_qa.py — programmatic export QA for OpenDesign templates.

Renders each template's PDF via the live studio export and checks, with NO AI/eyeball:
  1. export succeeds (HTTP 200, valid PDF, >= 1 page)   -> catches timeouts/render failures
  2. no near-blank page (ink coverage per page >= MIN_INK) -> catches footer-bleed /
     over-routed / wasted pages

Usage:
  export_qa.py --all                 # QA every template in ~/opendesign-projects
  export_qa.py <proj>/<slug> [...]   # QA specific templates
Exit code 0 = all PASS, 1 = at least one FAIL (so it can gate a push).
"""
import subprocess, sys, tempfile, urllib.request, urllib.error
from pathlib import Path

ROOT = Path.home() / "opendesign-projects"
KEY = (Path.home() / ".config/synthwave/opendesign/api_key").read_text().strip()
PORT = 8289
MIN_INK = 0.012          # a page with < 1.2% non-white pixels is "near-blank"
MIN_PDF_BYTES = 1500
TIMEOUT = 200

def list_templates():
    out = []
    for d in sorted(ROOT.glob("*/opendesign/mockups/*/index.html")):
        # path: <ROOT>/<proj>/opendesign/mockups/<slug>/index.html
        proj = d.parents[3].name
        slug = d.parents[0].name
        out.append(f"{proj}/{slug}")
    return out

def export_pdf(proj, slug, dest):
    url = (f"http://127.0.0.1:{PORT}/api/export/{proj}/pdf"
           f"?path=mockups/{slug}/index.html")
    req = urllib.request.Request(url, headers={"X-OD-Key": KEY})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, f"export error: {str(e)[:60]}"
    if not data.startswith(b"%PDF"):
        return None, "not a PDF (render failed)"
    if len(data) < MIN_PDF_BYTES:
        return None, f"PDF too small ({len(data)}b)"
    dest.write_bytes(data)
    return dest, None

def page_count(pdf):
    r = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split()[1])
    return 0

def near_blank_pages(pdf, td):
    """Return list of 1-based page numbers whose ink coverage < MIN_INK."""
    from PIL import Image
    base = td / "p"
    subprocess.run(["pdftoppm", "-png", "-r", "30", str(pdf), str(base)],
                   capture_output=True, timeout=120)
    bad = []
    for png in sorted(td.glob("p-*.png")):
        try:
            im = Image.open(png).convert("L")
            h = im.histogram()                 # 256 luminance bins, no deprecation
            total = sum(h) or 1
            ink = sum(h[:245]) / total         # fraction of non-white pixels
            if ink < MIN_INK:
                n = int(png.stem.split("-")[-1])
                bad.append((n, round(ink * 100, 2)))
        except Exception:
            pass
    return bad

def qa_one(t):
    proj, slug = t.split("/", 1)
    with tempfile.TemporaryDirectory(prefix="odqa-") as td:
        td = Path(td)
        pdf, err = export_pdf(proj, slug, td / "out.pdf")
        if err:
            return ("FAIL", t, err)
        pages = page_count(pdf)
        if pages < 1:
            return ("FAIL", t, "0 pages")
        blanks = near_blank_pages(pdf, td)
        if blanks:
            desc = ", ".join(f"p{n}={ink}%" for n, ink in blanks)
            return ("FAIL", t, f"{pages}p, near-blank: {desc}")
        return ("PASS", t, f"{pages}p")

def main():
    args = sys.argv[1:]
    templates = list_templates() if (not args or args == ["--all"]) else args
    results = [qa_one(t) for t in templates]
    fails = [r for r in results if r[0] == "FAIL"]
    for status, t, msg in results:
        print(f"  {status}  {t:48s} {msg}")
    print(f"=== {len(results) - len(fails)}/{len(results)} PASS | {len(fails)} FAIL ===")
    return 1 if fails else 0

if __name__ == "__main__":
    sys.exit(main())
