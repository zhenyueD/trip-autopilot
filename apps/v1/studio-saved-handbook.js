'use strict';

// Saved-handbook reading panel: the server-rendered handbook HTML is the single
// source of truth. This module only frames it (same-origin iframe), shows trip
// name / dates / handbook version, and stays readable while a new round is
// running, partial or failed. It never re-generates itinerary content.
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.TravelSavedHandbook = api;
})(typeof window === 'undefined' ? null : window, function () {
  const text = value => typeof value === 'string' || typeof value === 'number' ? String(value).trim() : '';
  const records = value => Array.isArray(value) ? value.filter(item => item && typeof item === 'object' && !Array.isArray(item)) : [];

  function handbookURL(trip, locationHref) {
    if (!trip || !trip.capabilities?.single_session || !trip.document || !trip.handbook_url) return '';
    try {
      const reader = new URL(trip.handbook_url, locationHref);
      const page = new URL(locationHref);
      if (reader.origin !== page.origin || reader.pathname !== `/api/agent/trips/${trip.id}/handbook`) return '';
      return reader.href;
    } catch (_) { return ''; }
  }

  // Backend-issued artifact URLs only — never local cache paths or guessed routes.
  function artifactURL(trip, field, locationHref) {
    const value = trip?.[field];
    if (!trip || !trip.capabilities?.single_session || !trip.document || typeof value !== 'string' || !value) return '';
    const statusField = field === 'first_eye_pdf_url' ? 'first_eye_pdf_status' : 'pdf_status';
    const status = trip.document[statusField] || trip[statusField];
    // A stored first-eye file is not a qualified download until explicitly ready.
    if (field === 'first_eye_pdf_url' ? status !== 'ready' : status && !['ready', 'below_standard'].includes(status)) return '';
    try {
      const target = new URL(value, locationHref);
      const page = new URL(locationHref);
      if (target.origin !== page.origin || target.pathname !== `/api/agent/trips/${trip.id}/pdf`) return '';
      if (target.searchParams.has('version') && target.searchParams.get('version') !== String(trip.document.version)) return '';
      return target.href;
    } catch (_) { return ''; }
  }

  const statusCopy = (trip, running) => running ? '新一轮正在准备，以下仍是当前已保存版本。'
    : trip.status === 'failed' ? '本轮已停止，已保存的手册仍可阅读。'
      : trip.delivery?.status === 'partial' ? '手册已保存，仍有几处交付缺口，已保存部分不受影响。'
        : '当前已保存版本，可直接阅读。';

  function update(trip, options = {}) {
    const doc = options.document || globalThis.document;
    const panel = doc?.getElementById?.('saved-handbook');
    if (!panel) return false;
    const locationHref = options.locationHref || globalThis.location?.href || 'http://localhost/studio.html';
    const href = handbookURL(trip, locationHref);
    if (!href) { panel.hidden = true; panel.replaceChildren(); delete panel.dataset.handbookKey; return false; }
    const version = trip.document.version;
    const key = `${trip.id}@${version}@${href}`;
    const existingFrame = panel.dataset.handbookKey === key ? panel.querySelector?.('iframe') : null;

    const create = (tag, className, content) => {
      const node = doc.createElement(tag);
      if (className) node.className = className;
      if (content !== undefined) node.textContent = content;
      return node;
    };
    const running = trip.status === 'running';
    const title = text(trip.plan?.title) || text(trip.draft_plan?.title) || text(options.tripTitle) || '这趟旅行';
    const days = records(trip.plan?.days || trip.draft_plan?.days).length;

    const head = create('div', 'saved-handbook-head');
    const heading = create('div', 'saved-handbook-heading');
    heading.append(create('p', 'folio', '已保存手册 · 旅行工作台内阅读'),
      create('h2', '', `旅行手册 · 版本 v${version}`),
      create('p', 'saved-handbook-meta', [title, days ? `${days} 天安排` : '', text(trip.created_at)?.slice(0, 10)].filter(Boolean).join(' · ')));
    const open = create('a', 'text-button', '独立打开 ↗');
    open.href = href; open.target = '_blank'; open.rel = 'noopener noreferrer';
    head.append(heading, open);

    const status = create('p', 'saved-handbook-status', statusCopy(trip, running));

    const downloads = create('p', 'saved-handbook-downloads');
    const fullPDF = artifactURL(trip, 'pdf_url', locationHref);
    if (fullPDF) {
      const full = create('a', 'secondary-button', '下载完整手册 PDF ↗');
      full.href = fullPDF; full.target = '_blank'; full.rel = 'noopener noreferrer';
      downloads.append(full);
    }
    const firstEye = artifactURL(trip, 'first_eye_pdf_url', locationHref);
    if (firstEye) {
      const eye = create('a', 'secondary-button', '下载第一眼 PDF ↗');
      eye.href = firstEye; eye.target = '_blank'; eye.rel = 'noopener noreferrer';
      downloads.append(eye);
    } else if (trip.document.first_eye_pdf_required || trip.first_eye_pdf_required) {
      downloads.append(create('span', 'saved-handbook-note',
        '第一眼 PDF 尚未达标或尚未确认可下载，不会用旧文件替代。'));
    }

    const frame = existingFrame || doc.createElement('iframe');
    frame.className = 'saved-handbook-frame';
    if (!existingFrame) frame.src = href;
    frame.title = `已保存的旅行手册（版本 v${version}）`;
    frame.setAttribute('loading', 'lazy');

    if (existingFrame) {
      // Keep the iframe attached: moving it through replaceChildren reloads it
      // in browsers even when the DOM node is the same object.
      panel.children[0].replaceWith(head);
      panel.children[1].replaceWith(status);
      panel.children[2].replaceWith(downloads);
    } else panel.replaceChildren(head, status, downloads, frame);
    panel.dataset.handbookKey = key;
    panel.hidden = false;
    return true;
  }

  // Reading-view tabs drive the saved document: the presented handbook already
  // carries the vetted photos with source captions, so a tab switches the
  // reading position instead of re-rendering anything. Sections are looked up
  // by class; missing sections fall back gracefully.
  const SECTION_FOR_VIEW = { daily: 'section.trip-days', prepare: 'section.predeparture-confirmation' };
  function syncView(view) {
    const panel = globalThis.document?.getElementById?.('saved-handbook');
    const frame = panel && !panel.hidden ? panel.querySelector?.('iframe') : null;
    if (!frame) return false;
    const scroll = () => {
      try {
        const doc = frame.contentDocument; const win = frame.contentWindow;
        if (!doc || !win) return false;
        const selector = SECTION_FOR_VIEW[view];
        const target = selector ? doc.querySelector(selector) || doc.querySelector('section.trip-todo') : null;
        const top = target ? target.getBoundingClientRect().top + win.scrollY - 12 : 0;
        win.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
        return true;
      } catch { return false; }
    };
    if (scroll()) return true;
    frame.addEventListener('load', () => scroll(), { once: true });
    return false;
  }

  return { update, handbookURL, artifactURL, syncView };
});
