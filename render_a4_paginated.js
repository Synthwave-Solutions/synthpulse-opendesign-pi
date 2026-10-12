/*
 * render_a4_paginated.js — render a brand `.page`-based HTML document to a proper
 * MULTI-PAGE A4 PDF (not OpenDesign's single tall-page export).
 *
 * Gives, in one shot:
 *   - content fills the full A4 width (small 30px gutters, cover/colofon bleed full)
 *   - full-bleed cover band to the TOP edge of page 1 (@page :first margin 0)
 *   - clean page breaks: whole articles (.section) stay together; only a section
 *     taller than a page breaks, and then only between clauses (never mid-clause)
 *   - the navy colofon banner pinned to the BOTTOM edge of the last page
 *
 * The print CSS is INJECTED at render time, so source templates stay clean — any
 * document built from the Synthwave `_brand` kit (`.page`, `.cover-band`, `.section`,
 * `.clause`, `.colofon`, `.sign-grid`, ...) renders correctly without edits.
 *
 * Usage:  NODE_PATH=~/.config/playwright/helper/node_modules \
 *           node render_a4_paginated.js <input.html> <output.pdf>
 * (the `od-pdf` CLI wraps this with NODE_PATH set.)
 *
 * DocuSeal signing render: set env OD_DOCUSEAL=1 (od-pdf --docuseal) to reveal the hidden
 * DocuSeal field text-tags so DocuSeal auto-detects + places signature/date fields on upload,
 * while the normal render stays clean. See the `docuseal` skill.
 */
const { chromium } = require('playwright-core');
const path = require('path');

const PRINT_CSS = `
  @page { size: A4; margin: 7mm 0; }
  @page :first { margin: 0; }
  body { background: #fff; }
  .page { width: auto; min-height: 0; box-shadow: none; padding: 0 30px; margin: 0; }
  .cover-band { margin: 0 -30px 24px; }
  .colofon { margin: 0 -30px -12mm; break-inside: avoid; }
  .doc-foot { display: none; }            /* per-page absolute footers don't survive flow pagination */
  /* cover page: a hero/cover band must bleed to the TOP edge, so hide a running header strip placed above it */
  .doc-head:has(+ .hero-band), .doc-head:has(+ .cover-band) { display: none; }
  .section, .clause, .party-grid .kv, .sign-grid, .callout, .considerans,
  .party-grid, table.tbl tr, .kpi, .stat, .viz { break-inside: avoid; }
  /* bespoke card/block classes (brochure, CV, pitch, etc.) so they never split mid-card */
  .card, .svc, .svc-step, .viz-card, .flow-card, .reason, .team-card,
  .kpi-band, .roadmap .step, .cta, .exp-card, .sb-section, .skill-item,
  .lang-row, .edu-card, .proof-kpi, .case, .pillar, figure, img { break-inside: avoid; }
  h2, .section > h2, .clause h4, .doc-title, .s-head { break-after: avoid; }
  p { orphans: 3; widows: 3; }
  .section { margin-bottom: 16px; }
`;

// --docuseal: reveal the hidden DocuSeal field text-tags ({{...;type=signature/date;role=...}})
// as TRANSPARENT text on the signature line, so DocuSeal auto-detects + places the fields on
// upload while the visible document stays clean. Signing render only.
const DOCUSEAL_CSS = `
  .ds-field { display: inline-block !important; color: transparent !important; font-size: 13px; line-height: 1; white-space: nowrap; user-select: none; pointer-events: none; }
  .sign .line { position: relative; overflow: visible; }
  .sign .line .ds-field { position: absolute; left: 0; bottom: 3px; }
`;

// Optional running footer (opt-in, default OFF so every existing document renders unchanged):
//   OD_FOOTER="Documentnaam"  -> "Documentnaam" left, "Pagina X van Y" right, in the bottom margin
// of every page except the cover (@page :first keeps margin 0, so the footer has no room there).
const FOOTER_TEXT = process.env.OD_FOOTER || '';
const FOOTER_CSS = `
  @page { size: A4; margin: 7mm 0 13mm; }
  @page :first { margin: 0 0 13mm; }   /* cover still bleeds to the top edge, footer keeps its strip */
  .colofon { margin-bottom: 0 !important; }   /* keep the navy band clear of the footer strip */
`;
// Optional colour logo in the running footer (opt-in): OD_FOOTER_LOGO=/path/to/logo.png
const FOOTER_LOGO = (() => {
  const f = process.env.OD_FOOTER_LOGO;
  if (!f) return '';
  try { return `<img src="data:image/png;base64,${require('fs').readFileSync(f).toString('base64')}" style="height:4.2mm;width:auto;margin-right:3mm;vertical-align:middle;">`; }
  catch (e) { console.error('OD_FOOTER_LOGO unreadable:', e.message); return ''; }
})();
const footerTemplate = (txt) => `
  <div style="width:100%;font-family:Roboto,Helvetica,Arial,sans-serif;font-size:7.5pt;color:#64748b;
              padding:0 11mm;display:flex;justify-content:space-between;align-items:center;">
    <span style="display:flex;align-items:center;">${FOOTER_LOGO}${txt.replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))}</span>
    <span>Pagina <span class="pageNumber"></span> van <span class="totalPages"></span></span>
  </div>`;

const countPages = (buf) => (buf.toString('latin1').match(/\/Type\s*\/Page[^s]/g) || []).length || 1;

(async () => {
  const [,, infile, outfile] = process.argv;
  if (!infile || !outfile) { console.error('usage: render_a4_paginated.js <in.html> <out.pdf>'); process.exit(2); }
  const url = 'file://' + path.resolve(infile);
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_PATH || "/usr/bin/chromium", args: ["--no-sandbox"] });
  try {
    const page = await browser.newPage();
    await page.emulateMedia({ media: 'print' });
    await page.goto(url, { waitUntil: 'load', timeout: 30000 });
    // inject the standard A4 print rules (cascade-last so it wins)
    await page.addStyleTag({ content: PRINT_CSS });
    if (process.env.OD_DOCUSEAL === '1') await page.addStyleTag({ content: DOCUSEAL_CSS });
    if (FOOTER_TEXT) await page.addStyleTag({ content: FOOTER_CSS });
    try { await page.evaluate(() => document.fonts.ready); } catch (e) {}
    await page.waitForTimeout(500);

    const hasColofon = await page.evaluate(() => !!document.querySelector('.colofon'));
    const renderBuf = async (spacer) => {
      await page.evaluate((px) => {
        document.querySelectorAll('.od-footer-spacer').forEach(n => n.remove());
        if (px > 0) {
          const colos = document.querySelectorAll('.colofon');
          const last = colos[colos.length - 1];
          if (last) {
            const sp = document.createElement('div');
            sp.className = 'od-footer-spacer'; sp.style.height = px + 'px';
            last.parentNode.insertBefore(sp, last);
          }
        }
      }, spacer);
      const opts = { format: 'A4', printBackground: true, preferCSSPageSize: true };
      if (FOOTER_TEXT) {
        opts.displayHeaderFooter = true;
        opts.headerTemplate = '<div></div>';
        opts.footerTemplate = footerTemplate(FOOTER_TEXT);
      }
      return page.pdf(opts);
    };

    let finalBuf = await renderBuf(0);
    if (hasColofon) {
      const N0 = countPages(finalBuf);
      // binary-search the largest spacer that keeps the page count == N0:
      // that pushes the colofon to the bottom of the last page without spilling.
      let lo = 0, hi = 1100, best = 0;
      for (let i = 0; i < 10; i++) {
        const mid = (lo + hi) >> 1;
        const buf = await renderBuf(mid);
        if (countPages(buf) === N0) { best = mid; lo = mid + 1; } else { hi = mid - 1; }
      }
      finalBuf = await renderBuf(best);
      console.log(`OK ${outfile}  pages=${N0}  footer-pin=${best}px`);
    } else {
      console.log(`OK ${outfile}  pages=${countPages(finalBuf)}  (no colofon to pin)`);
    }
    require('fs').writeFileSync(outfile, finalBuf);
  } finally {
    await browser.close();
  }
})().catch(e => { console.error('ERR', e.message); process.exit(1); });
