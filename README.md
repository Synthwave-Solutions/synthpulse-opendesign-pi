# OpenDesign Studio (app)

Self-hosted OpenDesign studio wrapper running on the Pi (`opendesign-app.service`,
127.0.0.1:8289, behind Tailscale Funnel `/design`). Companion content repo:
**Synthwave-Solutions/opendesign-projects**.

## Files
- `server.py` — studio HTTP server: projects, jobs, viewer, and `/api/export/<slug>/<fmt>`.
  PDF export **auto-routes** by artifact type:
  - legal `_brand` docs (`.clause`/`.cover-band`/`.colofon`) → `render_a4_paginated.js` (print A4)
  - decks (`.slide`) → per-slide screenshot → Pillow PDF
  - everything else → `export_render.js` (screen, paginated to A4, sized-to-content when single page)
- `export_render.js` — Playwright/Chromium renderer (screen media): decks, web-style docs, office PNGs.
- `render_a4_paginated.js` — print-media A4 paginator for `.page` brand documents (also the `od-pdf` CLI backend).
- `export_office.py` — pptx/docx (and deck PDF) builder via Pillow + python-pptx/docx.
- `shell.html` / `viewer.html` — studio UI + per-project viewer.

## Authoring + export rules
See the `opendesign-studio` skill (`~/.claude/skills/opendesign-studio/SKILL.md`),
section "Authoring rules for clean export".

## Git sync
The Pi is the source of truth. `opendesign-git-sync.timer` (systemd user timer, every 30 min,
Persistent) commits + pushes changes here and in `~/opendesign-projects` as Michael Ramirez.
The `github_workspace_sync` pull-cron then mirrors both repos into the workspace.
