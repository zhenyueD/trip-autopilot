'use strict';

// Real place photos are a presentation layer, not a modification of the trip.
// Google results belong only to mounted image figures, never a completed cache.
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.TravelPlaceMedia = api;
})(typeof window === 'undefined' ? null : window, function () {
  const PAGE_SIZE = 6; const BATCH_SIZE = 8;
  const text = value => typeof value === 'string' || typeof value === 'number' ? String(value).trim() : '';
  const list = value => Array.isArray(value) ? value : [];
  const rows = value => list(value).filter(item => item && typeof item === 'object' && !Array.isArray(item));
  const strings = value => list(value).map(text).filter(Boolean);
  const kinds = { city: '城市', station: '车站', attraction: '景点', landmark: '地标', hotel: '住宿', restaurant: '餐饮', airport: '机场' };
  const priority = { city: 0, landmark: 1, attraction: 2, station: 3 };
  const $ = id => document.getElementById(id);
  let current = null; let activeJobs = 0; const queue = [];

  // Lazily resolve: this script loads before preparation in the existing HTML.
  const policy = () => typeof module === 'object' && module.exports ? require('./studio-preparation.js') : window.TravelPreparation;
  const safeMediaURL = value => policy()?.safeURL(value) || '';
  const policyVersion = ctx => JSON.stringify([[...(ctx.helpers.unavailableSources || [])].sort(),
    [...(ctx.helpers.validatedSources || [])].sort()]);

  function projectEntities(value) {
    const seen = new Set();
    return rows(value).filter(item => {
      const id = text(item.id); if (!id || seen.has(id)) return false; seen.add(id); return true;
    }).map(item => ({
      ...Object.fromEntries(['id', 'name', 'search_name', 'kind', 'city', 'image_author', 'image_license', 'place_id'].map(key => [key, text(item[key])])),
      ...Object.fromEntries(['image_url', 'image_source_url', 'image_license_url'].map(key => [key, safeMediaURL(item[key])])),
      supply_ids: strings(item.supply_ids), related_item_ids: strings(item.related_item_ids),
      lat: Number.isFinite(item.lat) ? item.lat : null, lng: Number.isFinite(item.lng) ? item.lng : null
    })).sort((a, b) => (priority[a.kind] ?? 4) - (priority[b.kind] ?? 4));
  }

  function entitiesForItem(entities, item) {
    const id = text(item?.id); const supply = text(item?.supply_id);
    return entities.filter(entity => {
      // A city relation is not evidence that its skyline depicts a museum visit.
      if (entity.kind === 'city' && ![entity.name, entity.search_name].includes(text(item?.title))) return false;
      return Boolean((id && entity.related_item_ids.includes(id)) || (supply && entity.supply_ids.includes(supply)));
    }).sort((a, b) => Number(b.related_item_ids.includes(id)) - Number(a.related_item_ids.includes(id)));
  }

  function photoQuery(entity) {
    return { name: text(entity.search_name) || text(entity.name), city: text(entity.city),
      kind: text(entity.kind), ...(text(entity.place_id) ? { place_id: text(entity.place_id) } : {}) };
  }

  function node(tag, className, value) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value !== undefined) element.textContent = text(value);
    return element;
  }
  function urlFor(ctx, value) {
    const url = safeMediaURL(value);
    if (!url) return '';
    try { return ctx.helpers.safeURL ? safeMediaURL(ctx.helpers.safeURL(url)) : url; } catch { return ''; }
  }
  function sourceLink(ctx, label, value) {
    const safe = safeMediaURL(value);
    const url = policy().sourceURL({ source_url: safe }, ctx.helpers.unavailableSources, 'source_url', ctx.helpers.validatedSources);
    const anchor = node(url ? 'a' : 'span', '', url ? label : `${label}${safe ? ` · 来源未验证或不可用（不可点击）：${safe}` : ''}`);
    if (url) { anchor.href = url; anchor.target = '_blank'; anchor.rel = 'noopener noreferrer'; anchor.referrerPolicy = 'no-referrer'; }
    return anchor;
  }

  function pump() {
    while (activeJobs < 2 && queue.length) {
      const job = queue.shift();
      if (current !== job.ctx || (job.wanted && !job.wanted())) { job.resolve(null); continue; }
      activeJobs += 1;
      Promise.resolve().then(() => current === job.ctx && (!job.wanted || job.wanted()) ? job.run() : null)
        .then(job.resolve, () => job.resolve(null)).finally(() => { activeJobs -= 1; pump(); });
    }
  }
  function schedule(ctx, run, wanted) {
    return new Promise(resolve => { queue.push({ ctx, run, wanted, resolve }); pump(); });
  }

  function publicPhoto(ctx, entity) {
    const url = urlFor(ctx, entity?.image_url); const sourceURL = safeMediaURL(entity?.image_source_url);
    if (!url || !sourceURL || ctx.failedURLs.has(url)) return null;
    return { url, source: 'public_source', sourceURL, author: entity.image_author, license: entity.image_license,
      licenseURL: urlFor(ctx, entity.image_license_url) };
  }
  // Saved-handbook media: images already chosen and vetted by the presentation
  // pipeline, embedded in the served document. Reused as-is (no new selection).
  function savedPhoto(ctx, entity) {
    const url = text(entity?.image_url);
    if (!url.startsWith('data:image/') || ctx.failedURLs.has(url)) return null;
    return { url, source: 'saved_handbook', sourceURL: text(entity?.image_source_url),
      author: text(entity?.image_author), license: text(entity?.image_license), licenseURL: '' };
  }
  function photoFor(ctx, entity) {
    return ctx.googleAssets.get(entity.id)?.photo || publicPhoto(ctx, entity) || savedPhoto(ctx, entity);
  }
  function pageEntities(ctx) { return ctx.entities.slice(ctx.page * PAGE_SIZE, (ctx.page + 1) * PAGE_SIZE); }
  function wantedEntities(ctx) {
    const byId = new Map(pageEntities(ctx).map(entity => [entity.id, entity]));
    rows(ctx.day?.items).forEach(item => entitiesForItem(ctx.entities, item).forEach(entity => byId.set(entity.id, entity)));
    return [...byId.values()];
  }
  const wanted = (ctx, id) => current === ctx && wantedEntities(ctx).some(entity => entity.id === id);

  function clearGoogleFigure(ctx, figure) {
    figure.remove(); figure.querySelector('img')?.removeAttribute('src');
    figure.querySelector('figcaption')?.replaceChildren(); ctx.drawn.delete(figure);
  }
  function releaseGoogle(ctx, id, retry = true) {
    const asset = ctx.googleAssets.get(id);
    asset?.figures.forEach(figure => clearGoogleFigure(ctx, figure)); asset?.figures.clear();
    ctx.googleAssets.delete(id); ctx.cards.delete(id);
    if (retry) { ctx.googleTried.delete(id); ctx.done.delete(id); ctx.requested.delete(id); }
  }
  function pruneGoogle(ctx, installingId) {
    for (const [id, asset] of ctx.googleAssets) {
      for (const figure of asset.figures) if (!figure.isConnected) {
        clearGoogleFigure(ctx, figure); asset.figures.delete(figure);
      }
      if (!asset.figures.size && id !== installingId) releaseGoogle(ctx, id);
    }
  }
  function changeView(ctx) {
    ctx.generation += 1;
    for (const id of ctx.googleAssets.keys()) releaseGoogle(ctx, id);
    ctx.googleTried.forEach(id => ctx.done.delete(id)); ctx.googleTried.clear(); ctx.requested.clear();
  }
  const attemptCurrent = (ctx, attempt) => current === ctx && ctx.generation === attempt.generation
    && ctx.requested.get(attempt.entity.id) === attempt.token && wanted(ctx, attempt.entity.id);

  function mount(ctx) {
    const story = $('story'); if (!story) return false;
    let section = $('trip-places');
    if (!section) { section = node('section', 'trip-places'); section.id = 'trip-places'; }
    section.setAttribute('aria-labelledby', 'trip-places-title');
    let gallery = $('delivery-gallery');
    if (!gallery) {
      gallery = node('wa-details', 'delivery-gallery'); gallery.id = 'delivery-gallery';
      gallery.append(node('summary', '', '地点相册与图片来源'));
      const after = $('handbook-daily') || $('handbook-nav') || story; after.after(gallery);
    }
    gallery.append(section);
    if (ctx.section === section) return true;
    ctx.section = section;
    const header = node('div', 'trip-places-heading'); const title = node('h2', '', '先看看会去的地方'); title.id = 'trip-places-title';
    const controls = node('div', 'trip-places-controls');
    const makeButton = (id, label, step) => {
      const button = node('button', 'trip-places-button', label); button.id = id; button.type = 'button';
      button.setAttribute('aria-controls', 'trip-places-strip');
      button.addEventListener('click', () => {
        if (current !== ctx) return;
        const page = Math.max(0, Math.min(Math.ceil(ctx.entities.length / PAGE_SIZE) - 1, ctx.page + step));
        if (page !== ctx.page) changeView(ctx); ctx.page = page;
        paint(ctx); void enrich(ctx);
      }); return button;
    };
    ctx.previous = makeButton('trip-places-previous', '上一组', -1); ctx.next = makeButton('trip-places-next', '下一组', 1);
    ctx.count = node('span', 'trip-places-count'); controls.append(ctx.count, ctx.previous, ctx.next); header.append(title, controls);
    ctx.strip = node('div', 'trip-places-strip'); ctx.strip.id = 'trip-places-strip'; ctx.strip.setAttribute('aria-label', '来源地点照片');
    ctx.status = node('p', 'trip-places-status'); ctx.status.setAttribute('role', 'status'); ctx.status.setAttribute('aria-live', 'polite');
    section.replaceChildren(header, ctx.strip, ctx.status,
      node('p', 'trip-places-caveat', '来源实景照片，非实时画面；只帮助认识地点，不代表入口、营业状态或导航核验。'));
    ctx.cards.clear(); return true;
  }

  function captionFor(ctx, entity, photo) {
    const caption = node('figcaption', 'place-media-credit');
    if (photo.source === 'google_places') {
      caption.append(sourceLink(ctx, 'Google Maps', photo.googleMapsURI));
      photo.authors.forEach(author => caption.append(sourceLink(ctx, author.displayName || '摄影者（见来源）', author.uri)));
      if (photo.flagContentURI) caption.append(sourceLink(ctx, '报告照片问题', photo.flagContentURI));
    } else {
      caption.append(node('span', '', photo.author || '作者见来源页面'),
        sourceLink(ctx, photo.license || '许可见来源页面', photo.licenseURL), sourceLink(ctx, '图片来源 ↗', photo.sourceURL));
    }
    return caption;
  }

  function makePhoto(ctx, entity, photo, daily = false) {
    const figure = node('figure', `place-photo place-media-photo${daily ? ' handbook-item-photo' : ''}`);
    figure.dataset.placeMediaId = entity.id;
    const image = node('img'); image.src = photo.url; image.alt = `${entity.name || entity.search_name} · 来源照片`;
    image.loading = 'lazy'; image.decoding = 'async'; image.referrerPolicy = 'no-referrer';
    image.width = 640; image.height = 400;
    image.addEventListener('error', () => {
      if (current !== ctx || !image.isConnected) return;
      if (photo.source === 'google_places') releaseGoogle(ctx, entity.id, false);
      else ctx.failedURLs.add(photo.url);
      ctx.requested.delete(entity.id); ctx.done.delete(entity.id);
      paint(ctx); void enrich(ctx);
    }, { once: true });
    figure.append(image, captionFor(ctx, entity, photo)); ctx.drawn.set(figure, { id: entity.id, url: photo.url, policyVersion: policyVersion(ctx) });
    if (photo.source === 'google_places') ctx.googleAssets.get(entity.id)?.figures.add(figure);
    return figure;
  }

  function availability(ctx, entity) {
    if (ctx.requested.has(entity.id) && !ctx.done.has(entity.id)) return '正在查找来源照片…';
    return '尚未取得可用来源照片';
  }
  function placeCard(ctx, entity) {
    const photo = photoFor(ctx, entity); const status = photo ? photo.url : availability(ctx, entity);
    const signature = JSON.stringify([entity.name, entity.city, entity.kind, photo || status, policyVersion(ctx)]);
    const cached = ctx.cards.get(entity.id); if (cached?.signature === signature) return cached.node;
    const card = node('article', 'trip-place-card');
    const title = node('div', 'trip-place-title'); title.append(node('h3', '', entity.name || entity.search_name),
      node('span', '', [kinds[entity.kind] || '地点', entity.kind !== 'city' ? entity.city : ''].filter(Boolean).join(' · ')));
    if (photo) card.append(makePhoto(ctx, entity, photo));
    card.append(title);
    if (!photo) card.append(node('p', 'place-media-missing', status));
    ctx.cards.set(entity.id, { signature, node: card }); return card;
  }

  function paintGallery(ctx) {
    const entities = pageEntities(ctx); const pages = Math.ceil(ctx.entities.length / PAGE_SIZE);
    ctx.previous.disabled = ctx.page === 0; ctx.next.disabled = ctx.page >= pages - 1;
    ctx.previous.hidden = pages <= 1; ctx.next.hidden = pages <= 1;
    ctx.count.textContent = entities.length ? `${ctx.page * PAGE_SIZE + 1} 至 ${ctx.page * PAGE_SIZE + entities.length} / ${ctx.entities.length}` : '';
    ctx.strip.replaceChildren(...entities.map(entity => placeCard(ctx, entity)));
    for (const id of ctx.cards.keys()) if (!entities.some(entity => entity.id === id)) ctx.cards.delete(id);
    const havePhoto = entities.filter(entity => photoFor(ctx, entity)).length;
    ctx.status.textContent = !ctx.loaded ? '正在读取这趟旅行的地点…' : !ctx.entities.length
      ? '暂未取得地点图片清单；原行程与行动指南仍可使用。' : havePhoto ? ''
        : entities.some(entity => ctx.requested.has(entity.id) && !ctx.done.has(entity.id))
          ? '正在查找真实来源照片，行程内容可先阅读。' : '本组暂未取得可用照片，不以其他地点图片代替。';
    ctx.status.hidden = !ctx.status.textContent;
  }

  function paintDay(ctx) {
    const items = new Map(rows(ctx.day?.items).map(item => [text(item.id), item]));
    document.querySelectorAll('.timeline-item[data-item-id]').forEach(row => {
      const item = items.get(row.dataset.itemId); const body = row.querySelector('.item-body');
      if (!body) return;
      const previous = body.querySelector('.place-media-photo'); const note = body.querySelector('.place-media-note');
      if (!item) { previous?.remove(); note?.remove(); return; }
      const existing = body.querySelector('.place-photo'); if (existing && !previous) { note?.remove(); return; }
      const candidates = entitiesForItem(ctx.entities, item);
      const entity = candidates.find(candidate => photoFor(ctx, candidate)); const photo = entity && photoFor(ctx, entity);
      const drawn = previous && ctx.drawn.get(previous);
      if (photo && drawn?.id === entity.id && drawn.url === photo.url && drawn.policyVersion === policyVersion(ctx)) { note?.remove(); return; }
      previous?.remove(); note?.remove();
      if (photo) body.append(makePhoto(ctx, entity, photo, true));
      else if (candidates.length) body.append(node('p', 'place-media-note', `${candidates[0].name} · ${availability(ctx, candidates[0])}`));
    });
  }
  function paint(ctx, installingId) {
    if (current !== ctx) return;
    pruneGoogle(ctx, installingId);
    if (mount(ctx)) { paintGallery(ctx); paintDay(ctx); }
    pruneGoogle(ctx);
  }

  async function resolveGoogle(ctx, attempt) {
    const { entity } = attempt;
    if (photoFor(ctx, entity) || ctx.googleTried.has(entity.id) || !ctx.helpers.google?.findPhoto) return;
    await schedule(ctx, async () => {
      ctx.googleTried.add(entity.id);
      const result = await ctx.helpers.google.findPhoto(photoQuery(entity)); const url = urlFor(ctx, result?.url);
      if (!attemptCurrent(ctx, attempt) || !url) return;
      ctx.googleAssets.set(entity.id, { figures: new Set(), photo: { url, source: 'google_places', cacheable: false,
        googleMapsURI: urlFor(ctx, result.googleMapsURI), flagContentURI: urlFor(ctx, result.flagContentURI),
        authors: rows(result.authorAttributions).map(author => ({ displayName: text(author.displayName), uri: urlFor(ctx, author.uri) })) } });
      paint(ctx, entity.id);
    }, () => attemptCurrent(ctx, attempt));
  }

  async function resolveBatch(ctx, batch) {
    await schedule(ctx, async () => {
      const needed = batch.filter(attempt => attemptCurrent(ctx, attempt)).map(attempt => attempt.entity)
        .filter(entity => !photoFor(ctx, entity) && !ctx.backendTried.has(entity.id));
      if (!needed.length) return;
      needed.forEach(entity => ctx.backendTried.add(entity.id));
      const result = await ctx.helpers.request('POST', { entity_ids: needed.map(entity => entity.id) });
      if (current !== ctx || (result?.revision !== undefined && text(result.revision) !== ctx.mediaRevision)) return;
      const incoming = new Map(projectEntities(result?.entities).map(entity => [entity.id, entity]));
      ctx.entities = ctx.entities.map(entity => {
        if (!needed.some(item => item.id === entity.id) || !incoming.has(entity.id)) return entity;
        const next = incoming.get(entity.id);
        return { ...entity, ...next, name: next.name || entity.name, search_name: next.search_name || entity.search_name,
          kind: next.kind || entity.kind, city: next.city || entity.city,
          related_item_ids: next.related_item_ids.length ? next.related_item_ids : entity.related_item_ids,
          supply_ids: next.supply_ids.length ? next.supply_ids : entity.supply_ids };
      }); paint(ctx);
    });
  }

  async function resolveTargets(ctx, targets) {
    await Promise.all(targets.map(entity => resolveGoogle(ctx, entity)));
    if (current !== ctx) return;
    const missing = targets.filter(attempt => attemptCurrent(ctx, attempt) && !photoFor(ctx, attempt.entity) && !ctx.backendTried.has(attempt.entity.id));
    const batches = [];
    for (let index = 0; index < missing.length; index += BATCH_SIZE) batches.push(resolveBatch(ctx, missing.slice(index, index + BATCH_SIZE)));
    await Promise.all(batches);
    if (current !== ctx) return;
    targets.forEach(attempt => {
      if (ctx.requested.get(attempt.entity.id) !== attempt.token) return;
      if (attemptCurrent(ctx, attempt)) ctx.done.add(attempt.entity.id); ctx.requested.delete(attempt.entity.id);
    });
    paint(ctx);
  }

  function enrich(ctx) {
    if (current !== ctx || !ctx.loaded || typeof ctx.helpers.request !== 'function') return Promise.resolve();
    const targets = wantedEntities(ctx).filter(entity => !photoFor(ctx, entity) && !ctx.requested.has(entity.id) && !ctx.done.has(entity.id));
    if (targets.length) {
      const attempts = targets.map(entity => ({ entity, token: Symbol(), generation: ctx.generation }));
      attempts.forEach(attempt => ctx.requested.set(attempt.entity.id, attempt.token)); paint(ctx);
      const task = resolveTargets(ctx, attempts); ctx.work.add(task); task.finally(() => ctx.work.delete(task));
    }
    return Promise.all([...ctx.work]);
  }

  function planFor(trip) { return trip?.plan || trip?.draft_plan || trip || {}; }
  function fingerprint(trip) {
    const plan = planFor(trip);
    return JSON.stringify([text(trip?.id), text(trip?.revision), rows(plan.days).map(day => [day.day, day.city,
      rows(day.items).map(item => [item.id, item.title, item.supply_id])]), rows(plan.supplies).map(item => [item.id, item.image_url])]);
  }
  function reset() {
    if (current) for (const id of current.googleAssets.keys()) releaseGoogle(current, id);
    current = null;
    if (typeof document !== 'undefined') {
      $('trip-places')?.remove();
      document.querySelectorAll('.place-media-photo').forEach(element => element.remove());
      document.querySelectorAll('.place-media-note').forEach(element => element.remove());
    }
    pump();
  }

  async function loadManifest(ctx) {
    const result = typeof ctx.helpers.request === 'function' ? await schedule(ctx, () => ctx.helpers.request('GET')) : null;
    if (current !== ctx) return;
    ctx.loaded = true; ctx.mediaRevision = text(result?.revision ?? ctx.trip.revision);
    // A manifest returned for a superseded trip version must not be applied.
    if (ctx.trip.revision !== undefined && result?.revision !== undefined && text(ctx.trip.revision) !== text(result.revision)) {
      paint(ctx); return;
    }
    ctx.entities = projectEntities(result?.entities);
    // The single-session service has no /media manifest endpoint. Reuse the
    // media already saved in the presented document instead: same photos the
    // handbook and PDFs ship, already vetted with source attribution.
    if (!ctx.entities.some(entity => publicPhoto(ctx, entity)) && typeof ctx.helpers.savedMediaRequest === 'function') {
      const html = await schedule(ctx, () => ctx.helpers.savedMediaRequest('GET'));
      if (current === ctx && typeof html === 'string' && html.includes('<img')) {
        const saved = savedEntitiesFromDocument(html, planFor(ctx.trip));
        if (saved.length) {
          ctx.entities = saved;
          for (const entity of saved) {
            const key = text(entity.image_source_url) ? policy().urlKey(text(entity.image_source_url)) : '';
            if (key && ctx.helpers.validatedSources) ctx.helpers.validatedSources.add(key);
          }
        }
      }
    }
    paint(ctx); await enrich(ctx);
  }

  // Parse saved media out of the served handbook document: real photos embedded
  // as data URIs with author/license/source caption, day cards and the first-eye
  // photo grid carrying the same set. Places are bound to plan items by name so
  // removed places never reappear and items without a saved photo stay imageless.
  function parseSavedCaption(value) {
    const parts = text(value).split('·').map(part => part.trim()).filter(Boolean);
    const author = parts.length > 1 ? parts[1].replace(/图片出处$/, '').trim() : '';
    const license = parts.find(part => /^(CC|CC0|Public domain|公有领域|PD)\b/i.test(part)) || '';
    return { author, license };
  }
  function savedEntitiesFromDocument(html, plan) {
    if (typeof DOMParser !== 'function') return [];
    const doc = new DOMParser().parseFromString(String(html), 'text/html');
    const items = rows(plan?.days).flatMap(day => rows(day.items).map(item => ({ item, day })));
    const byPlace = new Map();
    const figures = [...doc.querySelectorAll('section.itinerary-day .destination-card figure'),
      ...doc.querySelectorAll('.fe-photos figure')];
    for (const figure of figures) {
      const img = figure.querySelector('img'); const name = text(img?.getAttribute('alt'));
      const src = text(img?.getAttribute('src'));
      if (!name || !src.startsWith('data:image/') || byPlace.has(name)) continue;
      const caption = figure.querySelector('figcaption');
      const sourceAnchor = caption?.querySelector('a[href^="https://"]');
      const parsed = parseSavedCaption(caption?.textContent || '');
      const related_item_ids = items.filter(({ item }) => text(item.title).includes(name))
        .map(({ item }) => text(item.id)).filter(Boolean);
      byPlace.set(name, { id: `saved:${name}`, name, search_name: name, kind: 'attraction', city: '',
        image_url: src, image_source_url: text(sourceAnchor?.getAttribute('href')),
        image_author: parsed.author, image_license: parsed.license,
        related_item_ids, supply_ids: [] });
    }
    return [...byPlace.values()];
  }

  function render(trip, helpers = {}) {
    if (!trip || typeof document === 'undefined') { reset(); return Promise.resolve(); }
    const sources = policy().planSources(planFor(trip));
    helpers = { ...helpers,
      unavailableSources: policy().unavailableSourceURLs(sources, helpers.unavailableSources),
      validatedSources: new Set([...(helpers.validatedSources || []), ...policy().validatedSourceURLs(sources)]) };
    const key = fingerprint(trip);
    if (current?.key === key) { current.helpers = helpers; paint(current); return Promise.all([current.ready, enrich(current)]); }
    const oldDay = text(current?.trip.id) === text(trip.id) ? text(current?.day?.day) : '';
    reset();
    const days = rows(planFor(trip).days);
    const ctx = { trip, key, helpers, day: days.find(day => oldDay && text(day.day) === oldDay) || days[0], page: 0, entities: [],
      loaded: false, generation: 0, cards: new Map(), googleAssets: new Map(), drawn: new WeakMap(), failedURLs: new Set(),
      googleTried: new Set(), backendTried: new Set(), requested: new Map(), done: new Set(), work: new Set() };
    current = ctx; paint(ctx); ctx.ready = loadManifest(ctx); return ctx.ready;
  }

  function selectDay(day) {
    const ctx = current; if (!ctx) return Promise.resolve();
    const selected = day && typeof day === 'object' ? day : rows(planFor(ctx.trip).days).find(entry => text(entry.day) === text(day));
    const dayKey = value => JSON.stringify([value?.day, value?.city, rows(value?.items).map(item => item.id)]);
    if (dayKey(ctx.day) !== dayKey(selected)) changeView(ctx); ctx.day = selected;
    paint(ctx); return enrich(ctx);
  }

  return { render, selectDay, reset, projectEntities, entitiesForItem, photoQuery, safeMediaURL };
});
