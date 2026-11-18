'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const handbook = require('../../apps/v1/studio-saved-handbook.js');

const TRIP_ID = 'f'.repeat(32);
const HREF = `http://localhost:8765/api/agent/trips/${TRIP_ID}/handbook`;

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.ownText = ''; this.dataset = {}; this.attributes = {}; this.hidden = false; }
  get textContent() { return [this.ownText, ...this.children.map(child => typeof child === 'string' ? child : child.textContent)].join(''); }
  set textContent(value) { this.ownText = String(value ?? ''); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(name, value) { this.attributes[name] = value; }
  querySelector() { return this.children.find(child => child.tag === 'iframe') || null; }
}
function makeDocument() {
  const panel = new Element('section'); panel.id = 'saved-handbook';
  return { panel, document: { getElementById: id => id === 'saved-handbook' ? panel : null, createElement: tag => new Element(tag) } };
}
function trip(overrides = {}) {
  return { id: TRIP_ID, status: 'completed', created_at: '2026-11-10T08:00:00Z',
    capabilities: { single_session: true }, document: { version: 2 },
    handbook_url: `/api/agent/trips/${TRIP_ID}/handbook`,
    plan: { title: '福冈五日', days: [{}, {}, {}, {}, {}] }, ...overrides };
}

test('saved handbook renders in-page reading panel framed with trip name, dates and version', () => {
  const { panel, document } = makeDocument();
  const shown = handbook.update(trip(), { document, locationHref: HREF });
  assert.equal(shown, true);
  assert.equal(panel.hidden, false);
  assert.match(panel.textContent, /旅行手册 · 版本 v2/);
  assert.match(panel.textContent, /福冈五日 · 5 天安排 · 2026-11-10/);
  const frame = panel.querySelector('iframe');
  assert.equal(frame.src, HREF);
  assert.match(frame.title, /版本 v2/);
  const open = panel.children[0].children[1];
  assert.equal(open.tag, 'a'); assert.equal(open.target, '_blank');
});

test('handbook stays readable while a new round is running, partial or failed', () => {
  for (const status of ['running', 'partial', 'failed']) {
    const { panel, document } = makeDocument();
    handbook.update(trip({ status, delivery: { status, quality_gaps: [{ rule: 'image_decode_gap', artifact: 'handbook_pdf', expected: 20, actual: 18 }] } }), { document, locationHref: HREF });
    assert.equal(panel.hidden, false, `panel visible for status=${status}`);
    assert.match(panel.textContent, /仍是当前已保存版本|仍可阅读|已保存部分不受影响/);
  }
});

test('missing document or cross-origin handbook URL keeps the panel hidden', () => {
  for (const bad of [trip({ document: null }),
    trip({ handbook_url: 'https://evil.example/handbook' })]) {
    const { panel, document } = makeDocument();
    assert.equal(handbook.update(bad, { document, locationHref: HREF }), false);
    assert.equal(panel.hidden, true);
  }
});

test('switching trips re-keys the panel so content never bleeds across trips', () => {
  const { panel, document } = makeDocument();
  handbook.update(trip(), { document, locationHref: HREF });
  const firstKey = panel.dataset.handbookKey;
  const other = trip({ id: 'e'.repeat(32), handbook_url: '/api/agent/trips/' + 'e'.repeat(32) + '/handbook', document: { version: 3 } });
  handbook.update(other, { document, locationHref: HREF.replace(TRIP_ID, 'e'.repeat(32)) });
  assert.notEqual(panel.dataset.handbookKey, firstKey);
  assert.match(panel.textContent, /版本 v3/);
  handbook.update(trip({ document: null }), { document, locationHref: HREF });
  assert.equal(panel.hidden, true);
  assert.equal(panel.children.length, 0, 'panel cleared after switching away');
});

test('same trip and version update is idempotent and keeps the existing frame', () => {
  const { panel, document } = makeDocument();
  handbook.update(trip(), { document, locationHref: HREF });
  handbook.update(trip(), { document, locationHref: HREF });
  assert.equal(panel.children.length, 4);
  assert.equal(handbook.handbookURL(trip(), HREF), HREF);
});

test('PDF downloads come only from backend-issued same-origin URLs', () => {
  const { panel, document } = makeDocument();
  handbook.update(trip({ pdf_url: `/api/agent/trips/${TRIP_ID}/pdf?version=2`,
    first_eye_pdf_url: `/api/agent/trips/${TRIP_ID}/pdf?first_eye=1&version=2` }), { document, locationHref: HREF });
  const downloads = panel.children[2];
  assert.equal(downloads.className, 'saved-handbook-downloads');
  const links = downloads.children.filter(node => node.tag === 'a');
  assert.equal(links.length, 2);
  assert.match(links[0].textContent, /完整手册 PDF/);
  assert.equal(links[0].target, '_blank');
  assert.match(links[0].href, new RegExp(`/api/agent/trips/${TRIP_ID}/pdf\\?version=2$`));
  assert.match(links[1].textContent, /第一眼 PDF/);
});

test('missing first-eye URL shows an honest note, never a guessed route; unsafe URLs are dropped', () => {
  const { panel, document } = makeDocument();
  handbook.update(trip({ first_eye_pdf_required: true, first_eye_pdf_status: 'incomplete' }), { document, locationHref: HREF });
  const downloads = panel.children[2];
  assert.equal(downloads.children.filter(node => node.tag === 'a').length, 0);
  assert.match(downloads.textContent, /第一眼 PDF 尚未达标/);
  handbook.update(trip({ pdf_url: 'https://evil.example/fake.pdf' }), { document, locationHref: HREF });
  const next = panel.children[2];
  assert.equal(next.children.filter(node => node.tag === 'a').length, 0);
});

test('saved reader tabs scroll the same document to daily, preparation, fallback and overview', t => {
  const previous = global.document; t.after(() => { global.document = previous; });
  const calls = []; const found = new Set(['section.trip-days', 'section.predeparture-confirmation']);
  const frame = { contentWindow: { scrollY: 100, scrollTo: options => calls.push(options) },
    contentDocument: { querySelector: selector => found.has(selector) ? { getBoundingClientRect: () => ({ top: 240 }) } : null } };
  global.document = { getElementById: () => ({ hidden: false, querySelector: () => frame }) };
  assert.equal(handbook.syncView('daily'), true);
  assert.deepEqual(calls.pop(), { top: 328, behavior: 'smooth' });
  assert.equal(handbook.syncView('prepare'), true);
  assert.equal(calls.pop().top, 328);
  found.clear(); found.add('section.trip-todo');
  assert.equal(handbook.syncView('prepare'), true);
  assert.equal(calls.pop().top, 328);
  assert.equal(handbook.syncView('overview'), true);
  assert.equal(calls.pop().top, 0);
});

test('unavailable frame waits for load once and hidden panels do not scroll', t => {
  const previous = global.document; t.after(() => { global.document = previous; });
  let loaded; let count = 0;
  const frame = { contentDocument: null, contentWindow: null,
    addEventListener: (event, callback, options) => { assert.equal(event, 'load'); assert.equal(options.once, true); loaded = callback; } };
  const panel = { hidden: true, querySelector: () => frame };
  global.document = { getElementById: () => panel };
  assert.equal(handbook.syncView('daily'), false);
  assert.equal(loaded, undefined);
  panel.hidden = false;
  assert.equal(handbook.syncView('daily'), false);
  frame.contentDocument = { querySelector: () => null };
  frame.contentWindow = { scrollY: 0, scrollTo: () => { count++; } };
  loaded(); assert.equal(count, 1);
});
