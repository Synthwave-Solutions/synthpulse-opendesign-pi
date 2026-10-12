// Render an OpenDesign artifact URL to PDF, full-page PNG, or per-slide PNGs.
// Usage: node export_render.js <url> [--pdf out.pdf] [--png out.png] [--slidedir dir]
//
// Deck-aware: if the page contains .slide elements it is treated as a deck.
//   --pdf      -> one vector page PER slide (deck) | one tall page (document)
//   --slidedir -> one PNG per .slide element (deck only); prints slide count
//   --png      -> single full-page tall screenshot (document office export)
//
// Pi gotcha: abort external requests except the CDNs designs actually use,
// otherwise headless chromium hangs on network (see pi-headless-browser memory).
const { chromium } = require('playwright-core');
const path = require('path');

const ALLOW = [
  'localhost', '127.0.0.1', 'tailbdab77.ts.net',
  'fonts.googleapis.com', 'fonts.gstatic.com',
  'cdn.tailwindcss.com', 'unpkg.com', 'cdn.jsdelivr.net', 'cdnjs.cloudflare.com',
];

(async () => {
  const [url, ...rest] = process.argv.slice(2);
  const opt = {};
  for (let i = 0; i < rest.length; i += 2) opt[rest[i].replace('--', '')] = rest[i + 1];
  if (!url || (!opt.pdf && !opt.png && !opt.slidedir)) {
    console.error('usage: export_render.js <url> [--pdf f] [--png f] [--slidedir d]');
    process.exit(2);
  }

  const browser = await chromium.launch({ headless: true });
  const result = { mode: 'document', slides: 0 };
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    await page.route('**/*', r => {
      const h = new URL(r.request().url()).hostname;
      if (ALLOW.some(a => h === a || h.endsWith('.' + a))) return r.continue();
      r.abort();
    });

    // WYSIWYG: render exactly as the page looks on screen, NOT in print media.
    // Decks/reports use @media print rules that change layout (collapse padding,
    // inject page-breaks) which previously truncated multi-slide decks. Forcing
    // screen media makes measurement match output.
    await page.emulateMedia({ media: 'screen' });

    await page.goto(url, { waitUntil: 'networkidle', timeout: 45000 }).catch(async () => {
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
    });
    await page.waitForSelector('body', { timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(2500);

    // Prepare the page for clean export:
    // 1. Freeze animations/transitions. A slide with a running CSS animation never
    //    reaches "stable", so element.screenshot() would time out.
    // 2. Hide presentation chrome (nav toolbars, pagers, fullscreen + slide-nav
    //    buttons) so it does not show up as "fluff" around each exported slide.
    await page.addStyleTag({ content:
      '*,*::before,*::after{animation:none!important;-webkit-animation:none!important;' +
      'transition:none!important;animation-play-state:paused!important;}' +
      '.nav-toolbar,.progress-dots,.btn-fullscreen,.nav-btn,.nav-indicator,.dots,' +
      '.slide-nav,.deck-nav,.presentation-nav,.slide-controls,.deck-controls,.pager,' +
      '[class*="fullscreen"],[onclick*="Slide"],[onclick*="slide("],[onclick*="ullscreen"]' +
      '{display:none!important;visibility:hidden!important;}' }).catch(() => {});

    // Per-element export is for true SLIDE decks only (.slide) - JS slideshows and
    // 16:9 stacks where each slide is its own image. Document templates that use
    // .page divs are handled by the paginating document branch instead (so variable
    // -height .page divs flow into uniform A4 pages with forced breaks).
    const UNIT = '.slide';
    const info = await page.evaluate((sel) => ({ total: document.querySelectorAll(sel).length }), UNIT);
    const isDeck = info.total >= 2;
    result.mode = isDeck ? 'deck' : 'document';
    result.slides = info.total;

    // ---- per-unit PNGs (paged office/PDF export: one image per slide / .page) ----
    if (opt.slidedir && isDeck) {
      const handles = await page.$$(UNIT);
      let captured = 0, skipped = 0;
      for (let i = 0; i < handles.length; i++) {
        // Reveal ONLY this unit (handles JS slideshows where units are absolutely
        // stacked and toggled). For normal-flow .page docs everything is already
        // visible, so this is harmless. Do NOT force display:flex (would break
        // block/grid .page layouts) - only un-hide display:none units.
        await page.evaluate((args) => {
          const [sel, idx] = args;
          document.querySelectorAll(sel).forEach((s, j) => {
            const on = j === idx;
            s.style.setProperty('opacity', on ? '1' : '0', 'important');
            s.style.setProperty('visibility', on ? 'visible' : 'hidden', 'important');
            if (on) {
              if (getComputedStyle(s).display === 'none') s.style.setProperty('display', 'block', 'important');
              s.style.setProperty('z-index', '99999', 'important');
            }
          });
        }, [UNIT, i]).catch(() => {});
        await page.waitForTimeout(120);
        // skip zero-size units (so output stays contiguous: slide-000..N)
        const box = await handles[i].boundingBox().catch(() => null);
        if (!box || box.width < 2 || box.height < 2) { skipped++; continue; }
        const p = path.join(opt.slidedir, `slide-${String(captured).padStart(3, '0')}.png`);
        await handles[i].scrollIntoViewIfNeeded({ timeout: 3000 }).catch(() => {});
        try {
          await handles[i].screenshot({ path: p, timeout: 12000, animations: 'disabled' });
          captured++;
        } catch (e) {
          try { // fallback: clip the region from a page screenshot (no stability wait)
            const b2 = await handles[i].boundingBox();
            if (b2) { await page.screenshot({ path: p, clip: b2, animations: 'disabled' }); captured++; }
            else skipped++;
          } catch (e2) { skipped++; }
        }
      }
      result.slidePngs = captured;
      result.skipped = skipped;
    }

    // ---- full-page PNG (document office export) ----
    if (opt.png) {
      const box = await page.evaluate(() => {
        const b = document.body, h = document.documentElement;
        return {
          w: Math.min(Math.max(b ? b.scrollWidth : 0, h.scrollWidth, h.clientWidth, 794), 4000),
          h: Math.min(Math.max(b ? b.scrollHeight : 0, h.scrollHeight, h.offsetHeight, 900), 60000),
        };
      });
      await page.setViewportSize({ width: box.w, height: Math.min(box.h, 8000) });
      await page.screenshot({ path: opt.png, fullPage: true, animations: 'disabled' });
    }

    // ---- PDF (document only; decks are assembled from per-slide PNGs in Python
    // for exact one-page-per-slide fidelity) ----
    if (opt.pdf) {
      // SHEET MODE: documents authored as discrete A4 `.page` sheets (white card on a
      // grey gutter, with "Pagina N van M" footers) must map ONE `.page` -> ONE PDF page.
      // Otherwise content-flow pagination drops PDF breaks mid-sheet, leaving grey gaps
      // and whitespace at the top of pages. Detect >=2 reasonably-sized sheets and force
      // one-per-page. Web-style docs whose few sheets overflow (e.g. brochure) skip this
      // and use the content-flow path below.
      const sheetHs = await page.evaluate(() =>
        [...document.querySelectorAll('.page')].map(e => Math.round(e.getBoundingClientRect().height)));
      const overflowing = sheetHs.filter(h => h > 1320).length;
      if (sheetHs.length >= 2 && overflowing === 0) {
        await page.addStyleTag({ content:
          'html,body{background:#fff!important;margin:0!important;padding:0!important;}' +
          '.page{break-after:page!important;break-before:auto!important;break-inside:avoid!important;' +
          'min-height:1123px!important;margin:0!important;box-shadow:none!important;}' +
          '.page:last-child{break-after:auto!important;}' });
        await page.waitForTimeout(150);
        await page.pdf({ path: opt.pdf, printBackground: true,
          width: '794px', height: '1123px',
          margin: { top: '0', right: '0', bottom: '0', left: '0' }, pageRanges: '1-60' });
        console.log(JSON.stringify({ ...result, mode: 'document-sheets', sheets: sheetHs.length }));
        return;
      }
      // Single-flow document: paginate into A4-proportioned pages at the CONTENT
      // column width (not the body) so there is NO dead left/right whitespace and
      // real PAGE BREAKS. Keep vector text; avoid breaking through cards/sections.
      // Find the dominant content container (the centered "sheet") and make it fill
      // the page: strip the body's background gutter and center the column edge-to-edge.
      const box = await page.evaluate(() => {
        const cands = [...document.querySelectorAll('.page, .doc, .sheet, main, .container, .wrap, article, body > div')];
        let best = null;
        for (const el of cands) {
          const r = el.getBoundingClientRect();
          if (r.width > 200 && r.height > 200) {
            if (!best || r.width * r.height > best.area) best = { w: Math.round(r.width), area: r.width * r.height };
          }
        }
        const bodyW = Math.max(document.body.scrollWidth, document.documentElement.clientWidth);
        const w = best ? best.w : bodyW;
        // collapse body gutter so the content sits flush and fills the page width
        document.documentElement.style.setProperty('background', '#ffffff', 'important');
        const b = document.body;
        b.style.setProperty('margin', '0', 'important');
        b.style.setProperty('padding', '0', 'important');
        b.style.setProperty('width', w + 'px', 'important');
        b.style.setProperty('display', 'block', 'important');
        const h = Math.min(Math.max(b.scrollHeight, document.documentElement.scrollHeight, 900), 60000);
        return { w: Math.min(Math.max(w, 320), 2400), h };
      });
      await page.addStyleTag({ content:
        // 1) never force breaks (forced breaks on tall sections waste half-empty pages)
        '*{break-before:auto!important;page-break-before:auto!important;' +
        'break-after:auto!important;page-break-after:auto!important;}' +
        // 2) keep a heading WELDED to the content that follows it (kills orphaned
        //    headings stranded at the bottom of a page)
        'h1,h2,h3,h4,h5,.section-head,.sec-head,.s-head,[class*="eyebrow"],[class*="kicker"],' +
        '[class*="-head"],[class*="head-"],[class*="section-title"],[class*="sec-title"],' +
        '[class*="running"],[class*="run-head"],thead,caption' +
        '{break-after:avoid!important;page-break-after:avoid!important;break-inside:avoid;}' +
        // 3) atomic blocks must not be sliced by a page boundary
        'tr,li,figure,img,svg,.fig,.card,.panel,.kpi,.swatch,.metric-card,.metric-cell,' +
        '.command,.sw-card,.prob-card,.prod-card,.step,.scale-row,.agent-row,.feature,' +
        '.benefit,.callout,.reg-row,.finding,blockquote,.arch,.flow,.timeline-item,' +
        '.network,.rings,[class*="card"],[class*="diagram"],[class*="architecture"],' +
        '[class*="figure"],[class*="-fig"],[class*="chart"],[class*="viz"],[class*="bar"],' +
        '[class*="testimonial"],[class*="quote"],[class*="cta"],[class*="module"],[class*="reason"],' +
        '[class*="svc"],[class*="team"],[class*="pricing"],[class*="kost"],[class*="stat"]' +
        '{break-inside:avoid;page-break-inside:avoid;}' +
        // 3b) drop the brand .page A4 min-height so a short trailing sheet (just a
        //     footer/CTA) does not force a near-blank full page in continuous flow
        '.page,.sheet,.doc-page{min-height:0!important;}' +
        // 4) large containers MUST be allowed to flow across pages (else they jump
        //    whole and leave big whitespace)
        'section,.section,main,.page,.doc,.wrap,.container,article,ul,ol,tbody,' +
        '[class*="grid"],[class*="cols"],[class*="list"]{break-inside:auto!important;page-break-inside:auto!important;}' +
        // 5) horizontal fit: let grid/flex children shrink instead of overflowing the
        //    page width, and never let media/tables exceed the column
        '[class*="grid"]>*,[class*="cols"]>*,.kpi,.metric-cell,.mc-body,.finding-body,' +
        '.card>*,td,th{min-width:0!important;}' +
        'table{max-width:100%!important;table-layout:auto!important;}' +
        'img,svg,canvas,video{max-width:100%!important;}' }).catch(() => {});
      await page.setViewportSize({ width: box.w, height: 900 }).catch(() => {});
      await page.waitForTimeout(150);
      // re-measure height after the width collapse + pagination styles settled
      const realH = await page.evaluate(() => Math.min(Math.max(document.body.scrollHeight,
        document.documentElement.scrollHeight, 200), 60000)).catch(() => box.h);
      const a4 = Math.max(Math.round(box.w * 1.414), 600); // A4 portrait at content width
      // If everything fits on ONE page, size the page to the content (no trailing
      // blank band, e.g. single square social posts). Else paginate at A4 height.
      const pageH = realH <= a4 + 24 ? Math.max(realH, 200) : a4;
      await page.pdf({
        path: opt.pdf,
        printBackground: true,
        width: `${box.w}px`,
        height: `${pageH}px`,
        margin: { top: '0', right: '0', bottom: '0', left: '0' },
        pageRanges: '1-50',
      });
    }
    console.log(JSON.stringify(result));
  } finally {
    await browser.close();
  }
})().catch(e => { console.error(e.message); process.exit(1); });
