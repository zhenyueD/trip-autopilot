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
<h1>合成四景点手册</h1><section class="trip-decision" style="min-height:900px">总览</section>
<section class="trip-todo"><section class="predeparture-confirmation" style="min-height:900px">出发前确认</section></section>
<section class="trip-days"><h2>逐日安排</h2><section class="itinerary-day">${[1, 2, 3, 4].map(number =>
  `<section class="destination-card"><figure><img src="${pixel}" alt="合成景点${number}"><figcaption>离线合成图片 · Fixture author · CC0</figcaption></figure></section>`).join('')}</section></section></html>`;
const onViewBody = fs.readFileSync(path.join(app, 'studio.js'), 'utf8').match(/onView: view => \{([^\n]+)\},/)?.[1];
assert.match(onViewBody || '', /TravelSavedHandbook\?\.syncView\?\.\(view\)/, 'Use the actual Studio callback, not a substitute integration');
const assets = ['studio-saved-handbook.js', 'studio-saved-handbook.css', 'studio-handbook.js', 'studio-handbook.css', 'studio-preparation.js', 'studio-presentation.js'];

test('saved-handbook current path loads all embedded photos and fits desktop/mobile viewports', async () => {
  const browser = await chromium.launch({ headless: true });
  const results = []; const unexpected = []; let handbookReads = 0;
  try {
    const page = await browser.newPage();
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== origin) { unexpected.push(url.href); return route.abort(); }
      if (url.pathname === '/studio.html') return route.fulfill({ contentType: 'text/html', body:
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>*{box-sizing:border-box}body{margin:0;padding:12px}main{min-width:0}.handbook-actions{display:none}</style><link rel="stylesheet" href="/studio-saved-handbook.css"><link rel="stylesheet" href="/studio-handbook.css"><main><div id="plan-content"><section id="story"></section><section class="itinerary-section"></section></div><section id="saved-handbook" class="saved-handbook"></section></main><script src="/studio-preparation.js"></script><script src="/studio-presentation.js"></script><script src="/studio-handbook.js"></script><script src="/studio-saved-handbook.js"></script>' });
      if (assets.includes(url.pathname.slice(1))) return route.fulfill({
        contentType: url.pathname.endsWith('.js') ? 'text/javascript' : 'text/css', body: fs.readFileSync(path.join(app, url.pathname.slice(1)), 'utf8') });
      if (url.pathname === `/api/agent/trips/${id}/handbook`) { handbookReads++; return route.fulfill({ contentType: 'text/html', body: handbook }); }
      unexpected.push(url.href); return route.abort();
    });
    await page.goto(`${origin}/studio.html`);
    await page.evaluate(body => {
      window.refreshMapLayout = () => {};
      window.TravelHandbook.mount({ onView: new Function('view', body) });
    }, onViewBody);
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
      const positions = {};
      for (const [view, selector] of [['daily', 'section.trip-days'], ['prepare', 'section.predeparture-confirmation'], ['overview', '']]) {
        await page.locator(`[data-handbook-view="${view}"]`).click();
        await frame.waitForFunction(selector => selector ? Math.abs(document.querySelector(selector).getBoundingClientRect().top - 12) < 2 : scrollY < 2, selector);
        positions[view] = await frame.evaluate(() => scrollY);
        assert.equal(await frameElement.evaluate(element => element === document.querySelector('iframe.saved-handbook-frame')), true);
      }
      assert.ok(positions.daily > positions.prepare && positions.prepare > 0);
      const outer = await page.evaluate(() => ({ client: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
      const inner = await frame.evaluate(() => ({ client: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth,
        images: document.images.length, decoded: [...document.images].filter(image => image.naturalWidth > 0).length }));
      assert.ok(outer.scroll <= outer.client, `${width}px outer page overflow: ${JSON.stringify(outer)}`);
      assert.ok(inner.scroll <= inner.client, `${width}px iframe overflow: ${JSON.stringify(inner)}`);
      results.push({ viewport: width, outer, inner, positions });
      if (process.env.FRONTEND_EVIDENCE_DIR) {
        fs.mkdirSync(process.env.FRONTEND_EVIDENCE_DIR, { recursive: true });
        await page.locator('[data-handbook-view="daily"]').click();
        await frame.waitForFunction(() => Math.abs(document.querySelector('section.trip-days').getBoundingClientRect().top - 12) < 2);
        await page.screenshot({ path: path.join(process.env.FRONTEND_EVIDENCE_DIR, `saved-handbook-${width}.png`), fullPage: true });
      }
    }
    assert.deepEqual(unexpected, [], 'No external or real-service requests');
    assert.equal(handbookReads, 1, 'Tab switching and same-version updates reuse the same iframe document');
    console.log(JSON.stringify({ savedHandbookBrowser: results }));
  } finally { await browser.close(); }
});
