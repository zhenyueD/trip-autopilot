'use strict';

// Sourced places become the same visual folio as the day's experiences.
// This module presents returned data only; it never invents images or routes.
window.TravelExperience = (() => {
  const $ = id => document.getElementById(id);
  const list = value => Array.isArray(value) ? value : [];
  const rows = value => list(value).filter(item => item && typeof item === 'object' && !Array.isArray(item));
  const text = value => typeof value === 'string' || typeof value === 'number' ? String(value) : '';
  const node = (tag, className, value) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value !== undefined) element.textContent = text(value);
    return element;
  };
  let discoveryFingerprint = '';

  function guardedHelpers(helpers, sources = []) {
    const policy = window.TravelPreparation;
    const unavailableSources = policy.unavailableSourceURLs(sources, helpers.unavailableSources);
    const validatedSources = new Set([...(helpers.validatedSources || []), ...policy.validatedSourceURLs(sources)]);
    const safeURL = value => {
      const safe = policy.safeURL(value);
      return safe && !unavailableSources.has(policy.urlKey(safe)) ? safe : '';
    };
    const link = (title, value, className) => {
      const safe = policy.safeURL(value);
      const url = policy.sourceURL({ source_url: safe }, unavailableSources, 'source_url', validatedSources);
      return url ? helpers.link(title, url, className)
        : node('span', className, `${title}${safe ? ` · 来源未验证或不可用（不可点击）：${safe}` : ''}`);
    };
    return { ...helpers, safeURL, link, unavailableSources, validatedSources };
  }

  // A conservative reading-language check, never a translation or place lookup.
  // A CJK alias inside a long English source paragraph is not Chinese prose.
  function chineseText(value) {
    const content = text(value).trim();
    const han = content.match(/\p{Script=Han}/gu)?.length || 0;
    const latin = content.match(/[A-Za-z]/g)?.length || 0;
    if (!han || /[\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u.test(content)) return '';
    return han / (han + latin) >= 0.25 ? content : '';
  }

  function placeReading(supply = {}) {
    supply ||= {};
    const local = text(supply.name).trim();
    const name = chineseText(supply.name_zh) || chineseText(local);
    const description = chineseText(supply.description_zh) || chineseText(supply.description);
    const original = text(supply.description).trim();
    return { name: name || '地点资料', localName: local !== name ? local : '', description,
      originalDescription: original !== description ? original : '' };
  }

  function appendOriginal(parent, label, value) {
    const entries = (Array.isArray(value) ? value : [value]).map(text).map(item => item.trim()).filter(Boolean);
    if (!entries.length) return;
    const source = node('wa-details', 'experience-more source-description');
    source.append(node('summary', '', `查看原文 · ${label}`));
    entries.forEach(entry => source.append(node('p', '', entry))); parent.append(source);
  }

  function appendReading(parent, value, className, label, limit = 180) {
    const chinese = chineseText(value);
    if (chinese) parent.append(copy(chinese, className, limit));
    else appendOriginal(parent, `${label}（未翻译）`, value);
  }

  function copy(value, className = '', limit = 180) {
    const content = text(value).trim();
    const block = node('div', className);
    if (!content) return block;
    if (content.length <= limit) { block.append(node('p', '', content)); return block; }
    const boundary = content.slice(0, limit).lastIndexOf('。');
    const cutoff = boundary > limit / 2 ? boundary + 1 : limit;
    block.append(node('p', '', `${content.slice(0, cutoff)}${cutoff === limit ? '…' : ''}`));
    const full = node('wa-details', 'experience-more');
    full.append(node('summary', '', '继续读'), node('p', '', content.slice(cutoff)));
    block.append(full); return block;
  }

  function photo(supply, helpers, className = '') {
    helpers = guardedHelpers(helpers, [supply]);
    if (window.TravelPreparation?.projectTraveler(supply)) return null;
    const url = helpers.safeURL(supply?.image_url);
    if (!url) return null;
    const figure = node('figure', `place-photo ${className}`);
    const reading = placeReading(supply);
    const image = node('img'); image.src = url; image.alt = chineseText(supply.name_zh) || text(supply.name);
    image.loading = 'lazy'; image.decoding = 'async'; image.referrerPolicy = 'no-referrer';
    const caption = node('figcaption');
    caption.append(helpers.link(`${reading.name} · 来源照片 ↗`, supply.image_source_url || supply.source_url));
    const credit = node('wa-details', 'photo-credit');
    credit.append(node('summary', '', '图片说明'), node('p', '', helpers.imageCredit(supply)));
    if (helpers.safeURL(supply.image_license_url)) credit.append(helpers.link('查看许可 ↗', supply.image_license_url));
    caption.append(credit); image.addEventListener('error', () => {
      image.hidden = true; figure.className += ' image-unavailable';
      caption.prepend(node('span', 'photo-unavailable', '来源图片暂未加载，可打开原页面查看。'));
    }, { once: true });
    figure.append(image, caption); return figure;
  }

  function appendTraveler(parent, supply, helpers) {
    helpers = guardedHelpers(helpers, [supply]);
    const traveler = window.TravelPreparation?.projectTraveler(supply, helpers.unavailableSources);
    if (!traveler) return false;
    const attribution = node('div', 'traveler-attribution');
    attribution.append(node('p', 'field-hint', `来源作者：${traveler.author_hint}`),
      node('p', 'field-hint', traveler.notice));
    if (traveler.citation_url) attribution.append(helpers.link('查看原帖 ↗', traveler.citation_url, 'place-source'));
    else if (traveler.source_ref) attribution.append(node('p', 'field-hint', `原帖记录（不可点击）：${traveler.source_ref}`));
    parent.append(attribution); return true;
  }

  function discoveryCard(supply, helpers) {
    const reading = placeReading(supply); const card = node('article', 'discovery-place'); const media = photo(supply, helpers);
    if (media) card.append(media);
    card.append(node('h3', '', reading.name));
    if (reading.localName) card.append(node('small', 'field-hint', `地图检索名称 · ${reading.localName}`));
    if (reading.description) card.append(copy(reading.description, 'discovery-copy', 110));
    else card.append(node('p', 'field-hint', '中文介绍尚未提供；可查看未翻译的来源原文。'));
    appendOriginal(card, '来源资料', reading.originalDescription);
    if (!appendTraveler(card, supply, helpers)) {
      const sourceURL = window.TravelPreparation?.sourceURL(supply, helpers.unavailableSources);
      card.append(sourceURL ? helpers.link(`查看地点来源${supply.provider ? ` · ${text(supply.provider)}` : ''} ↗`, sourceURL, 'place-source')
        : node('p', 'field-hint', `来源未验证（不可点击）：${window.TravelPreparation?.safeURL(supply.source_url || supply.source_provenance?.source_ref) || ''}`));
    }
    return card;
  }

  function renderDiscovery(trip, helpers) {
    helpers = guardedHelpers(helpers, [...rows(trip.discovery?.supplies), ...window.TravelPreparation.planSources(trip.plan), ...window.TravelPreparation.planSources(trip.draft_plan)]);
    const supplies = rows(trip.discovery?.supplies);
    const container = $('discovery');
    container.hidden = Boolean(trip.plan || trip.draft_plan) || !supplies.length;
    if (container.hidden) { discoveryFingerprint = ''; return; }
    const fingerprint = JSON.stringify([supplies, trip.status, [...(helpers.unavailableSources || [])].sort(), [...helpers.validatedSources].sort()]);
    if (fingerprint === discoveryFingerprint) return;
    discoveryFingerprint = fingerprint;
    const heading = node('div', 'discovery-heading');
    heading.append(node('h2', '', '已经找到，先看看这些地方。'), node('p', '',
      `${supplies.length} 个来源地点 · ${trip.status === 'running' ? '行程仍在编排，找到的内容会继续补进来。' : '这些是研究所得，尚不等于已排入行程。'}`));
    const gallery = node('div', 'discovery-gallery'); gallery.setAttribute('aria-label', '已找到的真实地点');
    const ordered = [...supplies].sort((a, b) => Number(Boolean(b.image_url)) - Number(Boolean(a.image_url)));
    ordered.slice(0, 6).forEach(supply => gallery.append(discoveryCard(supply, helpers)));
    container.replaceChildren(heading, gallery);
    if (ordered.length > 6) {
      const more = node('wa-details', 'experience-more discovery-more');
      more.append(node('summary', '', `查看其余 ${ordered.length - 6} 个候选地点`));
      const remaining = node('div', 'discovery-gallery'); remaining.setAttribute('aria-label', '其余候选地点');
      ordered.slice(6).forEach(supply => remaining.append(discoveryCard(supply, helpers))); more.append(remaining); container.append(more);
    }
    container.append(node('p', 'discovery-caveat', '来源图片与地点资料不是实时库存；候选会根据你的需求取舍，不代表已预订。'));
  }

  function appendExperience(body, item, supply, helpers) {
    const experience = item.experience && typeof item.experience === 'object' ? item.experience : {};
    const media = photo(supply, helpers, 'experience-photo');
    if (media) body.append(media);
    const reading = placeReading(supply);
    const background = chineseText(experience.background) || reading.description;
    if (background) body.append(copy(background, 'experience-background', 180));
    if (experience.background && !chineseText(experience.background)) appendOriginal(body, '体验背景（未翻译）', experience.background);
    if (reading.originalDescription !== text(experience.background).trim()) appendOriginal(body, '地点来源', reading.originalDescription);
    const activities = list(experience.what_to_do).map(text).filter(Boolean);
    const chineseActivities = activities.map(chineseText).filter(Boolean);
    if (chineseActivities.length) {
      const actions = node('div', 'experience-actions'); actions.append(node('h5', '', '在这里，怎么度过'));
      const steps = node('ul'); chineseActivities.forEach(activity => steps.append(node('li', '', activity)));
      actions.append(steps); body.append(actions);
    }
    appendOriginal(body, '玩法说明（未翻译）', activities.filter(activity => !chineseText(activity)));
    if (experience.personal_fit) appendReading(body, experience.personal_fit, 'experience-fit', '适合你的原因', 150);
    const tips = node('div', 'experience-tips');
    [['photo_tip', '拍照'], ['practical_tip', '到现场']].forEach(([key, label]) => {
      if (!text(experience[key]).trim()) return;
      if (!chineseText(experience[key])) { appendOriginal(body, `${label}提示（未翻译）`, experience[key]); return; }
      const tip = node('p'); tip.append(node('span', '', label), node('span', '', experience[key])); tips.append(tip);
    });
    if (tips.children.length) body.append(tips);
    if (reading.localName) body.append(node('small', 'field-hint', `地图检索名称 · ${reading.localName}`));
    if (!appendTraveler(body, supply, helpers) && supply?.source_url) {
      body.append(helpers.link(`${reading.name} · 地点来源 ↗`, supply.source_url, 'place-source'));
    }
  }

  function routeFor(plan, day) {
    return rows(plan?.routes).find(route => String(route.day) === String(day?.day)) || null;
  }

  function heroSupply(plan, safeURL) {
    const supplies = rows(plan?.supplies); const byId = new Map(supplies.map(supply => [supply.id, supply]));
    const eligible = supply => !window.TravelPreparation?.projectTraveler(supply) && safeURL(supply?.image_url);
    const activity = rows(plan?.days).flatMap(day => rows(day.items)).filter(item => item.activity_kind === 'activity')
      .map(item => byId.get(item.supply_id)).find(eligible);
    return activity || supplies.find(eligible);
  }

  function routeCoordinates(route) {
    const geometry = route?.geometry;
    if (!['available', 'partial'].includes(route?.status) || !text(route.provider).trim() || geometry?.type !== 'LineString') return [];
    const coordinates = list(geometry.coordinates);
    if (coordinates.length < 2 || coordinates.some(point => !Array.isArray(point) || point.length < 2
      || !Number.isFinite(point[0]) || !Number.isFinite(point[1]) || Math.abs(point[0]) > 180 || Math.abs(point[1]) > 90)) return [];
    return coordinates.map(point => [point[1], point[0]]);
  }

  function routeNote(route) {
    if (!routeCoordinates(route).length) return '尚未取得可显示的路网路线；仅标记已知地点，不用直线替代导航。';
    const coverage = route.status === 'partial' ? '部分路线 · 仅已定位站点，仍有未定位安排。' : '';
    return route.mode === 'driving' ? `${coverage}驾车路网参考 · 非实时路况，也不表示建议一定开车；不代表步行或公交路线。`
      : '来源路网路线参考 · 非实时路况；具体交通方式请以来源说明为准。';
  }

  function legMeasure(leg) {
    if (leg.status !== 'available') return '路线未取得 · 距离与用时未知';
    const distance = Number.isFinite(leg.distance_m) && leg.distance_m >= 0
      ? leg.distance_m >= 1000 ? `${(leg.distance_m / 1000).toFixed(1)} 公里` : `${Math.round(leg.distance_m)} 米` : '距离未知';
    const duration = Number.isFinite(leg.duration_s) && leg.duration_s >= 0
      ? `约 ${Math.max(1, Math.round(leg.duration_s / 60))} 分钟` : '用时未知';
    return `${distance} · ${duration}`;
  }

  function renderRoute(route, items, helpers) {
    helpers = guardedHelpers(helpers, [route, ...rows(route?.legs)]);
    const summary = $('route-summary'); const legs = $('route-legs');
    summary.replaceChildren(node('h3', '', '这一天，怎样串起来'), node('p', 'route-status', routeNote(route)));
    const titles = new Map(rows(items).map(item => [item.id, item.title]));
    const missing = list(route?.missing_item_ids).map(id => text(titles.get(id))).filter(Boolean);
    if (missing.length) summary.append(node('p', 'route-missing', `尚未定位：${missing.join('、')}。图上路线不包含这些安排。`));
    legs.replaceChildren(...rows(route?.legs).map(leg => {
      const row = node('li');
      row.append(node('p', 'route-leg-title', `${text(titles.get(leg.from_item_id)) || '起点'} → ${text(titles.get(leg.to_item_id)) || '下一站'}`),
        node('p', 'route-measure', legMeasure(leg)));
      if (leg.note || helpers.safeURL(leg.source_url)) {
        const evidence = node('wa-details', 'route-evidence'); evidence.append(node('summary', '', '分段来源与说明'));
        if (leg.note) evidence.append(node('p', 'route-note', leg.note));
        if (helpers.safeURL(leg.source_url)) evidence.append(helpers.link('路网来源 ↗', leg.source_url));
        row.append(evidence);
      }
      return row;
    }));
    if (route?.provider || route?.note || helpers.safeURL(route?.source_url)) {
      const evidence = node('wa-details', 'route-evidence'); evidence.append(node('summary', '', '路线来源与使用限制'));
      if (route.note) evidence.append(node('p', 'route-note', route.note));
      if (route.provider) evidence.append(node('p', 'route-provider', `路线来源：${text(route.provider)}`));
      if (helpers.safeURL(route.source_url)) evidence.append(helpers.link('查看路网查询结果 ↗', route.source_url));
      summary.append(evidence);
    }
    legs.hidden = !legs.children.length;
  }

  return { renderDiscovery, appendExperience, heroSupply, routeFor, routeCoordinates, renderRoute, routeNote,
    chineseText, placeReading, appendOriginal, appendTraveler };
})();
