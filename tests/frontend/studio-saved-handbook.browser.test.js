'use strict';

// Offline browser check: every request is fulfilled or rejected locally.
// Run with playwright installed (or NODE_PATH pointing at its installation).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const app = path.resolve(__dirname, '../../apps/v1');
const origin = 'http://localhost:8765';
const id = 'a'.repeat(32); // Synthetic, never a persisted trip or capability.
const pixel = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAKAAAABaCAIAAACwpMoFAAAA9klEQVR4nO3RQQmAQAAAwevphTGBT5uIISxlChH2BibAwo55XoSN3wv4lMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHDfmvi3luY+lGBxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHvdHhlLXxg/+qAAAAAElFTkSuQmCC';
const handbook = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>*{box-sizing:border-box}body{margin:0;padding:16px;overflow-wrap:anywhere}figure{margin:0;min-width:0}img{display:block;width:100%;height:auto;max-width:100%}</style>
<h1>合成四景点手册</h1><section class="itinerary-day">${[1, 2, 3, 4].map(number =>
  `<section class="destination-card"><figure><img src="${pixel}" alt="合成景点${number}"><figcaption>离线合成图片 · Fixture author · CC0</figcaption></figure></section>`).join('')}</section></html>`;

test('saved-handbook current path loads all embedded photos and fits desktop/mobile viewports', async () => {
  const browser = await chromium.launch({ headless: true });
  const results = []; const unexpected = [];
  try {
    const page = await browser.newPage();
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== origin) { unexpected.push(url.href); return route.abort(); }
      if (url.pathname === '/studio.html') return route.fulfill({ contentType: 'text/html', body:
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>*{box-sizing:border-box}body{margin:0;padding:12px}main{min-width:0}</style><link rel="stylesheet" href="/studio-saved-handbook.css"><main><section id="saved-handbook" class="saved-handbook"></section></main><script src="/studio-saved-handbook.js"></script>' });
      if (['/studio-saved-handbook.js', '/studio-saved-handbook.css'].includes(url.pathname)) return route.fulfill({
        contentType: url.pathname.endsWith('.js') ? 'text/javascript' : 'text/css', body: fs.readFileSync(path.join(app, url.pathname.slice(1)), 'utf8') });
      if (url.pathname === `/api/agent/trips/${id}/handbook`) return route.fulfill({ contentType: 'text/html', body: handbook });
      unexpected.push(url.href); return route.abort();
    });
    await page.goto(`${origin}/studio.html`);
    for (const width of [1280, 390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      const shown = await page.evaluate(({ id }) => window.TravelSavedHandbook.update({
        id, capabilities: { single_session: true }, status: 'completed', document: { version: 2 },
        plan: null, handbook_url: `/api/agent/trips/${id}/handbook`,
        pdf_url: `/api/agent/trips/${id}/pdf?version=2`, first_eye_pdf_url: `/api/agent/trips/${id}/pdf?version=2&first_eye=1`
      }), { id });
      assert.equal(shown, true, 'The real single-session plan:null path remains readable');
      const frameElement = await page.locator('iframe.saved-handbook-frame').elementHandle();
      const frame = await frameElement.contentFrame();
      await frame.waitForFunction(() => document.images.length === 4 && [...document.images].every(image => image.complete && image.naturalWidth > 0));
      const outer = await page.evaluate(() => ({ client: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
      const inner = await frame.evaluate(() => ({ client: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth,
        images: document.images.length, decoded: [...document.images].filter(image => image.naturalWidth > 0).length }));
      assert.ok(outer.scroll <= outer.client, `${width}px outer page overflow: ${JSON.stringify(outer)}`);
      assert.ok(inner.scroll <= inner.client, `${width}px iframe overflow: ${JSON.stringify(inner)}`);
      results.push({ viewport: width, outer, inner });
      if (process.env.FRONTEND_EVIDENCE_DIR) {
        fs.mkdirSync(process.env.FRONTEND_EVIDENCE_DIR, { recursive: true });
        await page.screenshot({ path: path.join(process.env.FRONTEND_EVIDENCE_DIR, `saved-handbook-${width}.png`), fullPage: true });
      }
    }
    assert.deepEqual(unexpected, [], 'No external or real-service requests');
    console.log(JSON.stringify({ savedHandbookBrowser: results }));
  } finally { await browser.close(); }
});
