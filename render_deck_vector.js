// Render a .slide deck to one VECTOR PDF page per slide, plus 2x PNGs.
// Usage: node render_deck_vector.js <url> <outdir>
const { chromium } = require('playwright-core');
const path = require('path');
const fs = require('fs');

const ALLOW = ['localhost', '127.0.0.1', 'fonts.googleapis.com', 'fonts.gstatic.com'];

(async () => {
  const [url, outdir] = process.argv.slice(2);
  fs.mkdirSync(outdir, { recursive: true });

  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_PATH || "/usr/bin/chromium", args: ["--no-sandbox"] });
  try {
    const page = await browser.newPage({
      viewport: { width: 1280, height: 720 },
      deviceScaleFactor: 2,
    });
    await page.route('**/*', r => {
      const h = new URL(r.request().url()).hostname;
      if (ALLOW.some(a => h === a || h.endsWith('.' + a))) return r.continue();
      r.abort();
    });
    await page.goto(url, { waitUntil: 'networkidle', timeout: 45000 })
      .catch(() => page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 }));
    await page.waitForTimeout(2500);

    // bevries animaties zodat elke slide stabiel is
    await page.addStyleTag({ content:
      '*,*::before,*::after{animation:none!important;transition:none!important;}' });

    const total = await page.evaluate(() => document.querySelectorAll('.slide').length);

    // 2x PNG per slide (voor pptx en als raster-fallback)
    const slides = await page.$$('.slide');
    for (let i = 0; i < slides.length; i++) {
      await slides[i].screenshot({
        path: path.join(outdir, `slide-${String(i).padStart(3, '0')}.png`),
      });
    }

    // vector PDF per slide: toon er telkens precies een, zonder marges of schaduw
    await page.addStyleTag({ content:
      '@page{size:1280px 720px;margin:0}' +
      'html,body{margin:0!important;padding:0!important;background:#080d18!important}' +
      '.deck{padding:0!important;background:none!important}' +
      '.slide{margin:0!important;border-radius:0!important;box-shadow:none!important;' +
      'break-inside:avoid;page-break-inside:avoid}' });

    for (let i = 0; i < total; i++) {
      await page.evaluate((idx) => {
        document.querySelectorAll('.slide').forEach((el, j) => {
          el.style.display = j === idx ? 'flex' : 'none';
        });
      }, i);
      await page.waitForTimeout(120);
      await page.pdf({
        path: path.join(outdir, `page-${String(i).padStart(3, '0')}.pdf`),
        width: '1280px', height: '720px',
        printBackground: true,
        margin: { top: '0', right: '0', bottom: '0', left: '0' },
        pageRanges: '1',
      });
    }
    console.log(JSON.stringify({ slides: total }));
  } finally {
    await browser.close();
  }
})();
