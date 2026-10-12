#!/usr/bin/env python3
"""OpenDesign Studio: projects + OmniRoute design jobs + static viewer hosting.

Served behind the Tailscale Funnel path /design (prefix is stripped by tailscale,
so everything here is rooted at /). One project = one OpenDesign workspace in
~/opendesign-projects/<slug>/opendesign/.
"""
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HOME = Path.home()
ROOT = HOME / "opendesign-projects"
APPDIR = Path(__file__).resolve().parent
# patched copy (preview cache-buster); upstream clone stays pristine
VIEWER_SRC = APPDIR / "viewer.html"
CFG = HOME / ".config/synthwave/opendesign"
CFG.mkdir(parents=True, exist_ok=True)
if not (CFG / "api_key").exists():
    fd = os.open(CFG / "api_key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f: f.write(secrets.token_hex(32))
API_KEY = (CFG / "api_key").read_text().strip()
HERMES = os.environ.get("HERMES_BIN", str(HOME / ".local/bin/hermes"))
OMNIROUTE_MODEL = os.environ.get("OPENDESIGN_MODEL", "synthpulse-auto")
OMNIROUTE_PROVIDER = os.environ.get("OPENDESIGN_PROVIDER", "custom")
# skills the Hermes engine loads for a design job (creative + brand strategy kit)
HERMES_SKILLS = "opendesign,synthwave-pptx,synthwave-docx,synthwave-brand-html,gpt-image-2,ui-ux-pro-max,svg-png-visuals,brand-guidelines,brand-voice,brand-identity,brand-strategy,brand-messaging"
# Keep the old API values as aliases so queued clients do not break. Every path
# now invokes Hermes explicitly through OmniRoute; no provider CLI is called.
ENGINES = ("omniroute", "gemini", "hermes")
PORT = 8289
JOB_TIMEOUT = "900"  # seconds, via coreutils timeout

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")

WRAP = (
    "Use the opendesign skill workflow. The workspace ./opendesign/ already exists "
    "(viewer at index.html, manifest.json, mockups/, design-systems/). Do NOT start a "
    "preview server and do NOT run the run-opendesign skill, the folder is already "
    "served publicly. You are running headless: skip interactive intake questions, "
    "make sensible assumptions and state them briefly.\n"
    "STEP 1 — BRAND GUIDANCE (do this first for any design request): "
    "The OpenDesign, brand, document, visual and UX skills are already preloaded for this job. "
    "Apply their brand guidance (tone, values, visual identity) and UX/design recommendations "
    "(layout, spacing, typography, color) directly. Do NOT invoke `od-skill` or launch another "
    "Hermes process from this job. You may run `od-ux` directly when additional deterministic "
    "design-system guidance is useful.\n"
    "STEP 2 — PRIMARY METHOD (templates + OpenDesign, not from scratch): "
    "Match the request to a template and COPY it, then fill {{TOKENS}}. "
    "Template libraries:\n"
    "1. Synthwave: ~/opendesign-projects/synthwave-templates/opendesign/mockups/ "
    "(opdrachtbevestiging, scope-document, process-deep-dive, intentieverklaring, quickscan-rapport, "
    "technisch-plan, offerte-voorstel, brochure, pitchdeck, workshop-presentatie, linkedin-post, linkedin-carousel). "
    "Party block: Papaverhof 59 1032 LX Amsterdam KvK 92058590. Fixed-price, no uren client-facing, 15% MSP, inline SVG icons.\n"
    "2. Secure AI Suite: ~/opendesign-projects/secure-ai-suite-templates/opendesign/mockups/ "
    "(sas-pitch, sas-case-study, sas-onepager, sas-proposal, sas-whitepaper). "
    "Brand: Royal Blue #3a78e4, Legal Navy #1b2d46, Trust Green #5db673.\n"
    "3. Academy: ~/opendesign-projects/synthwave-academy-templates/opendesign/mockups/ "
    "(academy-course-deck, academy-curriculum, academy-certificate, academy-brochure, academy-linkedin-post). "
    "Brand: Wave Blue #009dff, Navy #0f172a, Inter font.\n"
    "Premium component source: Forever Components is mirrored locally at "
    "~/opendesign-projects/forever-components/opendesign/ and available via the OpenDesign project "
    "forever-components. Use it for kinetic type, loaders, animated backgrounds, widgets, reveals, "
    "and micro-interactions when appropriate. Copy local source only, re-tokenize to the active brand, "
    "add prefers-reduced-motion, and avoid heavy infinite-canvas effects in PDF/export work.\n"
    "STEP 3 — IF NO MATCHING TEMPLATE, use skill helpers: "
    "`od-html <spec.json> [out.html] [--pdf]` (branded HTML), "
    "`od-pptx <spec.json> [-o out.pptx]` (PowerPoint), "
    "`od-docx <spec.json> [-o out.docx]` (Word), "
    "`od-skill \"<skill>\" \"<prompt>\"` for any other Hermes skill.\n"
    "VISUALS: `od-image \"<prompt>\" opendesign/<artifact>/assets/<name>.png --size 1536x1024`. "
    "NEVER use external placeholder/stock URLs. Hero/section art only, be judicious.\n"
    "BRAND RULES (absolute): Deep Navy #0f172a + Wave Blue #009dff, Montserrat/Roboto, NO orange, "
    "no em dashes (use : or |). Logo always in COLOR — dark bg: use *-dark.png variant. "
    "NEVER page-break-after:always (breaks PDF). Logo in flex-column: align-self:flex-start;flex:0 0 auto.\n"
    "UPLOADED FILES: check opendesign/uploads/ for source material (briefs, content, reference images).\n"
    "After building, update ./opendesign/manifest.json "
    "(sections[].groups[]={slug,files:[{label,path}]}, paths relative to ./opendesign/). "
    "Keep existing entries intact. Task: "
)

_lock = threading.Lock()
_running = {}  # slug -> job id

NODE = "/usr/local/bin/node"
NODE_PATH = "/opt/opendesign/node_modules"
BRAND_PY = "/opt/hermes/venv/bin/python"

EXPORT_CTYPES = {
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "html": "application/zip",
}

# ---- secure document generation (cloud portal -> Funnel /design -> here) ----
# The Helix client portal calls /api/generate to fill an OpenDesign A4 template
# with portal data and get a clean concept PDF back, which it stores for staff
# review and later pushes to DocuSeal. Deterministic (no LLM): token-fill + the
# same paginated-A4 renderer as the `od-pdf` CLI.
MOCKUP_LIBS = (
    ROOT / "synthwave-templates/opendesign/mockups",
    ROOT / "secure-ai-suite-templates/opendesign/mockups",
    ROOT / "synthwave-academy-templates/opendesign/mockups",
)
# Allowlist of slugs the portal may render (no path traversal, no arbitrary html).
GEN_TEMPLATE_SLUGS = {
    "opdrachtbevestiging", "scope-document", "intentieverklaring",
    "accordatie", "change-request-acceptatie", "oplevering",
}
TOKEN_RE = re.compile(r"\{\{\s*([A-Z0-9_]+)\s*\}\}")


def _find_template(slug):
    if slug not in GEN_TEMPLATE_SLUGS or not SLUG_RE.match(slug):
        raise ValueError(f"unknown template_slug '{slug}'")
    for lib in MOCKUP_LIBS:
        idx = (lib / slug / "index.html").resolve()
        if idx.is_file() and str(idx).startswith(str(lib.resolve())):
            return idx
    raise FileNotFoundError(f"template not found: {slug}")


def generate_document(slug, tokens):
    """Fill mockups/<slug>/index.html with tokens, render to a PDF (bytes, pages).

    The filled HTML is written INSIDE the template dir so relative assets
    (assets/brand.css, logos) resolve, then removed. Unprovided {{TOKENS}} are
    blanked so a concept never shows raw placeholders to staff/clients.
    """
    tpl_html = _find_template(slug)
    tpl_dir = tpl_html.parent
    html = tpl_html.read_text(encoding="utf-8")
    tokens = {str(k): ("" if v is None else str(v)) for k, v in (tokens or {}).items()}
    html = TOKEN_RE.sub(lambda m: tokens.get(m.group(1), ""), html)

    job = uuid.uuid4().hex
    gen_html = tpl_dir / f".gen_{job}.html"
    with tempfile.TemporaryDirectory(prefix="od-generate-") as td:
        out_pdf = Path(td) / "out.pdf"
        try:
            gen_html.write_text(html, encoding="utf-8")
            env = {**os.environ, "NODE_PATH": NODE_PATH}
            r = subprocess.run([NODE, str(APPDIR / "render_a4_paginated.js"),
                                str(gen_html), str(out_pdf)],
                               env=env, capture_output=True, text=True, timeout=300)
            if r.returncode != 0 or not out_pdf.is_file():
                raise RuntimeError(f"render failed: {(r.stderr or r.stdout)[-500:]}")
            data = out_pdf.read_bytes()
        finally:
            try:
                gen_html.unlink()
            except OSError:
                pass
    pages = 1
    try:
        info = subprocess.check_output(["pdfinfo", "-"], input=data, timeout=30)
        for line in info.decode(errors="replace").splitlines():
            if line.startswith("Pages:"):
                pages = int(line.split()[1])
                break
    except Exception:
        pass
    return data, pages


def export_artifact(slug, rel, fmt):
    """Render an artifact to the requested format; returns (bytes, filename)."""
    base = (project_dir(slug) / "opendesign").resolve()
    src = (base / rel).resolve()
    if not str(src).startswith(str(base)) or not src.is_file():
        raise ValueError("artifact niet gevonden")
    stem = src.parent.name if src.name == "index.html" else src.stem
    name = f"{slug}-{stem}.{fmt if fmt != 'html' else 'zip'}"
    with tempfile.TemporaryDirectory(prefix="od-export-") as td:
        td = Path(td)
        if fmt == "html":
            # zip the artifact's directory so relative assets travel along
            root = src.parent
            arch = shutil.make_archive(str(td / "design"), "zip", root)
            return Path(arch).read_bytes(), name
        url = f"http://127.0.0.1:{PORT}/p/{slug}/{rel}"
        png = td / "page.png"
        pdf = td / "out.pdf"
        slidedir = td / "slides"
        slidedir.mkdir()
        env = dict(os.environ, NODE_PATH=NODE_PATH)

        # Multi-sheet authored documents (>=2 `.page` sheets, not a `.slide` deck)
        # export best via the canonical paginated-A4 renderer (same engine as the
        # `od-pdf` CLI): faithful @page pagination, full-bleed cover, full A4 width,
        # clean clause breaks, colofon pinned to the last page. This makes the studio
        # PDF button produce identical print-quality output to od-pdf.
        if fmt == "pdf":
            html = src.read_text(errors="replace")
            tok = lambda t: len(re.findall(r'class="(?:[^"]*\s)?' + t + r'(?:\s[^"]*)?"', html))
            # Route every `_brand`-kit DOCUMENT (built with .clause / .cover-band /
            # .colofon / .doc-foot per-sheet structure) to render_a4_paginated.js —
            # it gives true A4 pagination with the colofon pinned per page. Sending
            # these through export_render.js instead flowed the authored .page sheets
            # continuously, dropping PDF breaks mid-sheet (grey gutter bands + top
            # whitespace + stranded per-sheet footers). EXCLUDE: decks (.slide),
            # square social (.social) and CVs (.cv-page). The brochure (.hero cover)
            # DOES go through the paginator: with its fixed-height cover it paginates
            # far cleaner (cover + first section fill page 1, no grey gaps).
            is_kit_doc = ((tok("clause") >= 3 or tok("colofon") >= 1)
                          and tok("slide") < 2 and tok("social") < 1
                          and tok("cv-page") < 1)
            if is_kit_doc:
                out = td / "a4.pdf"
                a4 = subprocess.run([NODE, str(APPDIR / "render_a4_paginated.js"), str(src), str(out)],
                                    env=env, capture_output=True, text=True, timeout=180)
                if a4.returncode == 0 and out.exists() and out.stat().st_size > 1000:
                    return out.read_bytes(), name
                # else: fall through to the generic renderer below

        # Always capture per-slide PNGs (decks) plus a vector document PDF and a
        # full-page PNG (documents) in one render pass; route by what came out.
        cmd = [NODE, str(APPDIR / "export_render.js"), url,
               "--png", str(png), "--slidedir", str(slidedir), "--pdf", str(pdf)]
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            raise RuntimeError(f"render faalde: {(r.stderr or r.stdout)[-300:]}")
        is_deck = any(slidedir.glob("slide-*.png"))

        if fmt == "pdf":
            if is_deck:
                # one page per slide image (exact WYSIWYG) via Pillow
                out = td / "deck.pdf"
                r = subprocess.run([BRAND_PY, str(APPDIR / "export_office.py"), str(slidedir), str(out), stem],
                                   capture_output=True, text=True, timeout=180)
                if r.returncode != 0:
                    raise RuntimeError(f"pdf bouw faalde: {(r.stderr or r.stdout)[-300:]}")
                return out.read_bytes(), name
            return pdf.read_bytes(), name  # document: crisp vector PDF

        # pptx / docx: deck -> per-slide images; document -> slice the tall PNG
        office_src = slidedir if is_deck else png
        out = td / f"out.{fmt}"
        r = subprocess.run([BRAND_PY, str(APPDIR / "export_office.py"), str(office_src), str(out), stem],
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            raise RuntimeError(f"{fmt} bouw faalde: {(r.stderr or r.stdout)[-300:]}")
        return out.read_bytes(), name


def project_dir(slug):
    return ROOT / slug


def jobs_dir(slug):
    d = project_dir(slug) / ".jobs"
    d.mkdir(exist_ok=True)
    return d


def list_projects():
    out = []
    for d in sorted(ROOT.iterdir()):
        ws = d / "opendesign"
        if not ws.is_dir():
            continue
        meta = {}
        mf = d / "meta.json"
        if mf.exists():
            try:
                meta = json.loads(mf.read_text())
            except Exception:
                pass
        mockups = ws / "mockups"
        systems = ws / "design-systems"
        out.append({
            "slug": d.name,
            "name": meta.get("name", d.name),
            "mockups": len([p for p in mockups.iterdir() if p.is_dir()]) if mockups.is_dir() else 0,
            "systems": len([p for p in systems.iterdir() if p.is_dir()]) if systems.is_dir() else 0,
            "running": _running.get(d.name),
        })
    return out


def create_project(name):
    slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]", "-", name.lower().strip())).strip("-")[:40]
    if not SLUG_RE.match(slug or ""):
        raise ValueError("invalid name")
    d = project_dir(slug)
    if d.exists():
        raise ValueError("project bestaat al")
    ws = d / "opendesign"
    (ws / "mockups").mkdir(parents=True)
    (ws / "design-systems").mkdir()
    shutil.copy(VIEWER_SRC, ws / "index.html")
    (ws / "manifest.json").write_text(json.dumps({
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "sections": [
            {"id": "mockups", "label": "Mockups", "groups": []},
            {"id": "design-systems", "label": "Design Systems", "groups": []},
        ],
    }, indent=2))
    (d / "meta.json").write_text(json.dumps({"name": name, "created": time.strftime("%Y-%m-%d")}))
    return slug


def job_meta_path(slug, jid):
    return jobs_dir(slug) / f"{jid}.json"


def write_job(slug, jid, **kw):
    p = job_meta_path(slug, jid)
    meta = {}
    if p.exists():
        meta = json.loads(p.read_text())
    meta.update(kw)
    p.write_text(json.dumps(meta))
    return meta


def list_jobs(slug):
    out = []
    for p in sorted(jobs_dir(slug).glob("*.json")):
        try:
            out.append(json.loads(p.read_text()))
        except Exception:
            pass
    out.sort(key=lambda j: j.get("started", 0))
    return out


def job_cmd(engine, prompt):
    """Build the subprocess argv for the chosen design engine."""
    full = WRAP + prompt
    cmd = [
        "timeout", JOB_TIMEOUT, HERMES, "-z", full,
        "--model", OMNIROUTE_MODEL, "--provider", OMNIROUTE_PROVIDER,
        "--toolsets", "terminal,file", "--yolo",
    ]
    # All jobs get the full skill kit. Legacy engine names remain transport aliases only.
    cmd.extend(["--skills", HERMES_SKILLS])
    return cmd


def run_job(slug, jid, prompt, engine="omniroute"):
    log = jobs_dir(slug) / f"{jid}.log"
    try:
        with open(log, "wb") as fh:
            proc = subprocess.Popen(
                job_cmd(engine, prompt),
                cwd=project_dir(slug), stdout=fh, stderr=subprocess.STDOUT,
            )
            rc = proc.wait()
        write_job(slug, jid, status="done" if rc == 0 else "failed", rc=rc, ended=time.time())
    except Exception as e:  # noqa: BLE001
        write_job(slug, jid, status="failed", error=str(e), ended=time.time())
    finally:
        with _lock:
            if _running.get(slug) == jid:
                del _running[slug]


def start_job(slug, prompt, engine="omniroute"):
    if engine not in ENGINES:
        raise ValueError("onbekende engine")
    with _lock:
        if slug in _running:
            raise RuntimeError("er draait al een job voor dit project")
        jid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        _running[slug] = jid
    write_job(slug, jid, id=jid, prompt=prompt, engine=engine, status="running", started=time.time())
    threading.Thread(target=run_job, args=(slug, jid, prompt, engine), daemon=True).start()
    return jid


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        pass

    # ---------- helpers ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        key = self.headers.get("X-OD-Key", "")
        if not key:
            q = parse_qs(urlparse(self.path).query)
            key = (q.get("key") or [""])[0]
        return secrets.compare_digest(key, API_KEY)

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _serve_file(self, fp):
        ctype = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        data = fp.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---------- routing ----------
    def do_GET(self):
        path = urlparse(self.path).path
        pwa_files = {
            "/manifest.webmanifest": APPDIR / "manifest.webmanifest",
            "/service-worker.js": APPDIR / "service-worker.js",
            "/icons/icon-192.png": APPDIR / "icons/icon-192.png",
            "/icons/icon-512.png": APPDIR / "icons/icon-512.png",
            "/icons/icon-maskable-512.png": APPDIR / "icons/icon-maskable-512.png",
        }
        if path in pwa_files:
            return self._serve_file(pwa_files[path])
        if path in ("/", "/index.html"):
            html = (APPDIR / "shell.html").read_text()
            inject = f'<script>localStorage.setItem("odkey","{API_KEY}")</script>'
            html = html.replace("</head>", inject + "</head>", 1)
            data = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path.startswith("/api/"):
            return self.api("GET", path)
        if path.startswith("/p/"):
            return self.static_project(path)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            return self.api("POST", path)
        return self._send(404, {"error": "not found"})

    def static_project(self, path):
        parts = path.split("/", 3)  # '', 'p', slug, rest
        if len(parts) < 3 or not SLUG_RE.match(parts[2]):
            return self._send(404, {"error": "not found"})
        slug, rest = parts[2], (parts[3] if len(parts) > 3 else "")
        if rest in ("", None):
            rest = "index.html"
        base = (project_dir(slug) / "opendesign").resolve()
        fp = (base / rest).resolve()
        if not str(fp).startswith(str(base)) or not fp.is_file():
            # directory request -> index.html
            if fp.is_dir() and (fp / "index.html").is_file() and str(fp).startswith(str(base)):
                return self._serve_file(fp / "index.html")
            return self._send(404, {"error": "not found"})
        return self._serve_file(fp)

    def api(self, method, path):
        if not self._authed():
            return self._send(401, {"error": "invalid key"})
        try:
            if method == "GET" and path == "/api/projects":
                return self._send(200, list_projects())
            if method == "POST" and path == "/api/projects":
                name = (self._json_body().get("name") or "").strip()
                slug = create_project(name)
                return self._send(200, {"slug": slug})
            m = re.match(r"^/api/projects/([a-z0-9-]+)/jobs$", path)
            if m and SLUG_RE.match(m.group(1)) and project_dir(m.group(1)).is_dir():
                slug = m.group(1)
                if method == "GET":
                    return self._send(200, list_jobs(slug))
                body = self._json_body()
                prompt = (body.get("prompt") or "").strip()
                engine = (body.get("engine") or "omniroute").strip()
                if not prompt:
                    return self._send(400, {"error": "prompt is leeg"})
                if engine not in ENGINES:
                    return self._send(400, {"error": "onbekende engine"})
                try:
                    jid = start_job(slug, prompt, engine)
                except RuntimeError as e:
                    return self._send(409, {"error": str(e)})
                return self._send(200, {"id": jid, "engine": engine})
            m = re.match(r"^/api/projects/([a-z0-9-]+)/upload$", path)
            if method == "POST" and m and SLUG_RE.match(m.group(1)):
                slug = m.group(1)
                if not project_dir(slug).is_dir():
                    return self._send(404, {"error": "project niet gevonden"})
                fname = re.sub(r"[^\w.\-]", "_", self.headers.get("X-Filename", "upload"))[:100] or "upload"
                n = int(self.headers.get("Content-Length") or 0)
                if n > 50 * 1024 * 1024:
                    return self._send(413, {"error": "bestand te groot (max 50MB)"})
                data = self.rfile.read(n)
                uploads_dir = project_dir(slug) / "opendesign" / "uploads"
                uploads_dir.mkdir(exist_ok=True)
                (uploads_dir / fname).write_bytes(data)
                return self._send(200, {"path": f"uploads/{fname}", "name": fname})
            m = re.match(r"^/api/projects/([a-z0-9-]+)/uploads$", path)
            if method == "GET" and m and SLUG_RE.match(m.group(1)):
                slug = m.group(1)
                uploads_dir = project_dir(slug) / "opendesign" / "uploads"
                files = [f.name for f in uploads_dir.iterdir() if f.is_file()] if uploads_dir.is_dir() else []
                return self._send(200, {"files": sorted(files)})
            m = re.match(r"^/api/export/([a-z0-9-]+)/(pdf|pptx|docx|html)$", path)
            if method == "GET" and m:
                slug, fmt = m.groups()
                q = parse_qs(urlparse(self.path).query)
                rel = (q.get("path") or [""])[0]
                data, fname = export_artifact(slug, rel, fmt)
                self.send_response(200)
                self.send_header("Content-Type", EXPORT_CTYPES[fmt])
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return None
            if method == "POST" and path == "/api/generate":
                body = self._json_body()
                slug = (body.get("template_slug") or "").strip()
                tokens = body.get("tokens")
                if tokens is not None and not isinstance(tokens, dict):
                    return self._send(400, {"error": "tokens must be an object"})
                try:
                    data, pages = generate_document(slug, tokens)
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                except FileNotFoundError as e:
                    return self._send(404, {"error": str(e)})
                import base64 as _b64
                return self._send(200, {
                    "ok": True,
                    "template_slug": slug,
                    "file_b64": _b64.b64encode(data).decode(),
                    "pages": pages,
                    "bytes": len(data),
                })
            m = re.match(r"^/api/jobs/([a-z0-9-]+)/([a-zA-Z0-9-]+)$", path)
            if method == "GET" and m:
                slug, jid = m.groups()
                p = job_meta_path(slug, jid)
                if not p.exists():
                    return self._send(404, {"error": "job onbekend"})
                meta = json.loads(p.read_text())
                log = jobs_dir(slug) / f"{jid}.log"
                if log.exists():
                    meta["log_tail"] = log.read_text(errors="replace")[-4000:]
                return self._send(200, meta)
            return self._send(404, {"error": "not found"})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": str(e)})


if __name__ == "__main__":
    ROOT.mkdir(exist_ok=True)
    srv = ThreadingHTTPServer((os.environ.get("OPENDESIGN_BIND", "127.0.0.1"), PORT), Handler)
    print(f"OpenDesign Studio on 127.0.0.1:{PORT}")
    srv.serve_forever()
