'use strict';

// Presentation of the existing public plan. Day switching never generates a plan.
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.TravelDelivery = api;
})(typeof window === 'undefined' ? null : window, function () {
  const preparationAPI = typeof module === 'object' && module.exports ? require('./studio-preparation.js') : window.TravelPreparation;
  const selections = new WeakMap();
  const draws = new WeakMap(); // container → 切天函数（selectDay 入口）
  const text = value => typeof value === 'string' || typeof value === 'number' ? String(value) : '';
  const list = value => Array.isArray(value) ? value : [];
  const brief = (value, length = 88) => text(value).length > length ? `${text(value).slice(0, length)}…` : text(value);
  function node(tag, className, value) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value !== undefined) element.textContent = text(value);
    return element;
  }
  function button(label, callback, className = 'delivery-button') {
    const element = node('button', className, label); element.type = 'button';
    element.addEventListener('click', callback); return element;
  }
  function dayPhoto(day, supplies) {
    const ids = new Set(list(day?.items).map(item => item.supply_id).filter(Boolean));
    return list(supplies).find(supply => ids.has(supply.id) && supply.image_url) || null;
  }
  function mapURL(stop) {
    const query = Number.isFinite(stop?.lat) && Number.isFinite(stop?.lng)
      ? `${stop.lat},${stop.lng}` : [stop?.meetingPoint, stop?.supplyName, stop?.city, stop?.title].filter(Boolean).join(' ');
    return query ? `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(query)}` : '';
  }
  function stops(day, supplies = []) {
    const supplyById = new Map(list(supplies).map(item => [item.id, item]));
    return list(day?.items).map((item, position) => ({
      number: position + 1, title: text(item.title) || '安排待补充', time: text(item.time), city: text(day?.city),
      details: list(item.details),
      preview: text(list(item.guide?.steps)[0]) || text(list(item.experience?.what_to_do)[0]) || text(item.why),
      transport: text(item.transport), meetingPoint: text(item.guide?.meeting_point), bring: list(item.guide?.bring).map(text).filter(Boolean),
      supplyName: text(supplyById.get(item.supply_id)?.name_zh) || text(supplyById.get(item.supply_id)?.name),
      lat: supplyById.get(item.supply_id)?.lat, lng: supplyById.get(item.supply_id)?.lng
    }));
  }
  function link(label, url) {
    const anchor = node('a', '', label); anchor.href = url; anchor.target = '_blank';
    anchor.rel = 'noopener noreferrer'; anchor.referrerPolicy = 'no-referrer'; return anchor;
  }
  // 「打开地图」+ 12px 描线箭头 icon（P0-10 #2：↗ 字符退役）。
  function mapLink(url) {
    const anchor = link('打开地图', url);
    const glyph = typeof window !== 'undefined' && window.TravelIcons?.svg?.('external', 12);
    if (glyph) anchor.append(glyph);
    return anchor;
  }
  function photo(day, plan) {
    const figure = node('figure', 'delivery-photo'); const supply = dayPhoto(day, plan.supplies);
    const fallback = node('div', 'delivery-photo-fallback');
    fallback.append(node('span', 'delivery-kicker', '每一天，都有期待'),
      node('strong', '', day.city || '这一天'), node('p', '', '按你的节奏，把每一站串起来。'));
    figure.append(fallback);
    if (!supply) return figure;
    const image = node('img'); image.alt = `${supply.name} · 来源实景照片`;
    fallback.setAttribute('aria-hidden', 'true');
    image.src = supply.image_url; image.decoding = 'async'; image.referrerPolicy = 'no-referrer';
    image.addEventListener('error', () => { image.hidden = true; credit.hidden = true; fallback.setAttribute('aria-hidden', 'false'); });
    const credit = node('wa-details', 'delivery-photo-credit');
    credit.append(node('summary', '', '实景照片 · 来源与许可'), node('p', '', supply.name));
    credit.append(node('p', '', supply.image_attribution || [supply.image_author, supply.image_license].filter(Boolean).join(' · ') || '署名信息未提供'));
    credit.append(node('p', '', '历史实景照片，非实时画面；按版式裁切。'));
    if (supply.image_source_url || supply.source_url) credit.append(link('查看原图来源 ↗', supply.image_source_url || supply.source_url));
    if (supply.image_license_url) credit.append(link('图片许可 ↗', supply.image_license_url));
    figure.append(image, credit); return figure;
  }
  function dayPreparation(day, plan, options) {
    const items = [...list(plan.actions), ...list(plan.gaps)].filter(item => Number(item.day) === Number(day.number));
    if (!items.length) return null;
    const aside = node('wa-callout', 'delivery-day-prep');
    aside.setAttribute('variant', 'brand');
    const body = node('div'); body.append(node('span', 'delivery-kicker', '这一天出发前'),
      node('strong', '', items.length === 1 ? items[0].task : `${items.length} 项关键准备`),
      node('p', '', brief(items.map(item => item.task).join('、'), 110)));
    const focus = items.find(item => item.owner === 'user') || items[0];
    aside.append(body, button('去处理 →', () => options.onPrepare?.(focus?.id), 'delivery-button')); return aside;
  }
  function route(day, plan, options) {
    const panel = node('div', 'delivery-route');
    panel.append(node('p', 'delivery-kicker', '行程顺序 · 非导航路线'));
    const sequence = node('ol', 'delivery-stops');
    stops(day, plan.supplies).forEach(stop => {
      const row = node('li', 'delivery-stop');
      const marker = node('span', 'delivery-marker', String(stop.number).padStart(2, '0'));
      marker.setAttribute('aria-hidden', 'true');
      const body = node('div', 'delivery-stop-body');
      const heading = node('div', 'delivery-stop-heading');
      heading.append(node('time', '', stop.time || '时间待定'), node('h4', '', stop.title));
      body.append(heading);
      preparationAPI.renderDetailSummary(body, stop.details);
      if (stop.preview) body.append(node('p', 'delivery-stop-preview', brief(stop.preview, 64)));
      const maps = mapURL(stop); if (maps) body.append(mapLink(maps));
      if (stop.meetingPoint || stop.bring.length) {
        const detail = node('wa-details', 'delivery-field-card'); detail.append(node('summary', '', '入口与随身准备'));
        if (stop.meetingPoint) detail.append(node('p', '', `到这里：${stop.meetingPoint}`));
        if (stop.bring.length) detail.append(node('p', '', `随身带：${stop.bring.join('、')}`)); body.append(detail);
      }
      if (stop.transport) {
        const detail = node('wa-details', 'delivery-transfer');
        detail.append(node('summary', '', '怎么到这里'), node('p', '', stop.transport)); body.append(detail);
      }
      row.append(marker, body); sequence.append(row);
    });
    if (!sequence.children.length) sequence.append(node('li', 'delivery-empty', '当天具体安排尚待补充。'));
    const prep = dayPreparation(day, plan, options); if (prep) panel.append(prep);
    panel.append(sequence, node('p', 'delivery-footnote', '时间为方案建议；实际开放与预约情况见出发准备。'),
      button('打开当天行动指南 →', () => options.onDay?.(day.index), 'delivery-button delivery-primary'));
    return panel;
  }
  function preparation(plan, options) {
    const aside = node('aside', 'delivery-preflight'); const content = node('div');
    content.append(node('span', 'delivery-kicker', 'BEFORE YOU GO'), node('h3', '',
      plan.actions.length ? `${plan.actions.length} 项，需要你亲自处理` : '出发前，再看一眼准备状态'));
    content.append(node('p', '', plan.actions.length ? brief(plan.actions.map(item => item.task).join('、'), 90) : '没有列出本人待办，不代表准备全部完成。'));
    if (plan.gaps.length) content.append(node('p', 'delivery-gap', `还有 ${plan.gaps.length} 项信息待查清，暂不能视为准备齐全。`));
    if (plan.calendarWarning) content.append(node('p', 'delivery-gap', plan.calendarWarning));
    aside.append(content, button('查看准备 →', () => options.onPrepare?.())); return aside;
  }
  function render(container, plan, options = {}) {
    const key = options.tripId || plan.title;
    const previous = selections.get(container);
    let selected = previous?.key === key ? Math.min(previous.index, Math.max(0, plan.days.length - 1)) : 0;
    const header = node('div', 'delivery-heading'); const title = node('h2', '', '这一趟，照着走。');
    title.id = 'handbook-overview-title';
    header.append(title, node('p', '', `${plan.days.length} 天 · ${plan.counts.items} 段安排 · 选一天，看看怎么过`));
    // P0-10 #16：天选择二合一——可点的只有天卡片 strip；这里退成当前天 meta 行。
    const meta = node('p', 'delivery-day-meta'); meta.id = 'delivery-day-meta';
    const stage = node('section', 'delivery-stage'); stage.id = 'delivery-selected-day';
    stage.setAttribute('aria-labelledby', 'delivery-day-title');
    const live = node('p', 'delivery-sr'); live.setAttribute('role', 'status');
    const draw = () => {
      selections.set(container, { key, index: selected });
      const day = plan.days[selected];
      if (!day) {
        meta.textContent = '';
        stage.replaceChildren(node('p', 'delivery-empty', '行程正在完善，具体日程尚待补充。'));
        return;
      }
      const stopList = stops(day, plan.supplies);
      const firstTime = stopList.map(stop => stop.time).find(Boolean);
      meta.textContent = `第 ${day.number} 天${firstTime ? ` · ${firstTime} 出发` : ''} · ${stopList.length} 个站点`;
      live.textContent = `已切换至第 ${day.number} 天：${day.title}`;
      const heading = node('header', 'delivery-day-heading');
      const name = node('h3', '', day.title); name.id = 'delivery-day-title';
      heading.append(node('p', 'delivery-kicker', [`第 ${day.number} 天`, day.date, day.city].filter(Boolean).join(' / ')), name);
      const visual = node('div', 'delivery-visual'); visual.append(photo(day, plan));
      if (day.lodging) { const stay = node('wa-details', 'delivery-stay'); stay.append(node('summary', '', '这晚住哪里'), node('p', '', day.lodging)); visual.append(stay); }
      const layout = node('div', 'delivery-layout'); layout.append(visual, route(day, plan, options));
      if (!dayPhoto(day, plan.supplies)) layout.className += ' delivery-layout-no-photo';
      stage.replaceChildren(heading, layout);
    };
    // 选天链：strip/外部入口统一从这里切天（P0-14 #4）。
    draws.set(container, index => {
      if (!Number.isInteger(index) || !plan.days[index]) return;
      selected = index; draw();
    });
    container.replaceChildren(header, preparation(plan, options), meta, stage, live); draw();
  }
  function selectDay(container, index) { draws.get(container)?.(index); }
  return { render, selectDay, dayPhoto, stops, mapURL };
});
