'use strict';

// One canonical plan, three reading views. Export is a public-field allowlist,
// never a dump of the trip/session, raw materials, tool records or model evidence.
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.TravelHandbook = api;
})(typeof window === 'undefined' ? null : window, function () {
  const preparationAPI = typeof module === 'object' && module.exports ? require('./studio-preparation.js') : window.TravelPreparation;
  const presentationAPI = typeof module === 'object' && module.exports ? require('./studio-presentation.js') : window.TravelPresentation;
  const text = value => typeof value === 'string' || typeof value === 'number' ? String(value).trim() : '';
  const list = value => Array.isArray(value) ? value : [];
  const object = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  const rows = value => list(value).filter(value => value && typeof value === 'object' && !Array.isArray(value));
  const strings = value => list(value).map(text).filter(Boolean);
  const categories = { flight: '航班', lodging: '住宿', transport: '交通', dining: '餐饮', activities: '体验', packing: '行前', other: '其他安排' };
  const statuses = { proposed: '方案建议 · 未预订', provided: '用户已提供 · 非平台核验', needs_check: '资料待核实', user_action: '需要本人操作', not_applicable: '本次不适用',
    ready: '资料已备齐 · 不代表已办理', needs_user: '需要本人操作', needs_research: '资料待核实', user_reported: '本人报告已办 · 未经渠道确认' };
  const label = status => statuses[status] || '状态未说明 · 待落实';
  const escapeHTML = value => text(value).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);

  const safeURL = preparationAPI.safeURL;

  function pick(value, keys) {
    const source = object(value); const result = {};
    keys.forEach(key => { result[key] = text(source[key]); }); return result;
  }

  const urlKey = preparationAPI.urlKey;

  function projectURLs(item, keys, unavailable, validated) {
    const result = {};
    keys.forEach(key => {
      const url = safeURL(item[key]); const failed = unavailable.has(urlKey(url));
      result[key] = key === 'image_url' ? (failed ? '' : url) : preparationAPI.sourceURL(item, unavailable, key, validated);
      result[`${key}_text`] = result[key] ? '' : url;
    }); return result;
  }

  function projectItem(item) {
    const result = pick(item, ['id', 'title', 'time', 'end_time', 'why', 'transport', 'preparation', 'supply_id', 'activity_kind']);
    const guide = object(item.action_guide); const experience = object(item.experience);
    result.guide = { ...pick(guide, ['meeting_point', 'booking_note']), steps: strings(guide.steps), bring: strings(guide.bring) };
    result.experience = { ...pick(experience, ['background', 'personal_fit', 'photo_tip', 'practical_tip']), what_to_do: strings(experience.what_to_do) };
    result.title ||= '安排名称尚待补充'; return result;
  }

  function projectSupply(item, unavailable, validated) {
    const result = {
      ...pick(item, ['id', 'name', 'name_zh', 'provider', 'description', 'address', 'city',
        'image_author', 'image_license', 'image_attribution']),
      ...projectURLs(item, ['source_url', 'image_url', 'image_source_url', 'image_license_url'], unavailable, validated)
    };
    if (Number.isFinite(item.lat) && Number.isFinite(item.lng)) {
      result.lat = item.lat; result.lng = item.lng;
    }
    result.traveler = projectTraveler(item, unavailable);
    if (result.traveler) {
      result.source_url = result.traveler.citation_url;
      result.source_url_text = result.source_url ? '' : result.traveler.source_ref;
      result.image_url = ''; // Traveler photos remain original-post references.
    }
    return result;
  }

  function projectTraveler(source, unavailable, alreadyProjected = false) {
    const traveler = alreadyProjected ? source.traveler : preparationAPI?.projectTraveler(source);
    if (!traveler) return null;
    return { ...traveler, citation_url: unavailable.has(urlKey(traveler.source_ref)) ? '' : traveler.citation_url };
  }

  function projectPreparation(item, source = 'preparation', unavailable = new Set(), days = []) {
    const related = strings(item.related_item_ids);
    const inferredDay = days.find(day => rows(day.items).some(row => related.includes(row.id)));
    return { ...pick(item, ['id', 'category', 'status', 'why', 'when']), source,
      day: Number(item.day) > 0 ? Number(item.day) : inferredDay ? Number(inferredDay.day) : null,
      related_item_ids: related, details: preparationAPI.projectDetails(item.details, unavailable),
      task: text(source === 'decision' ? item.choice : item.task) || '具体事项尚待补充',
      why: text(source === 'decision' ? item.reason : item.why),
      steps: strings(item.steps), materials: strings(item.materials), supply_ids: strings(item.supply_ids),
      source_url: '', action_url: '', source_url_text: safeURL(item.source_url),
      action_url_text: safeURL(item.action_url), entry_label: '搜索线索 · 未验证可用性' };
  }

  function projectPackAction(action, pack, unavailable) {
    return { ...projectPreparation({ ...action, task: action.title, steps: action.instructions }),
      action_url: unavailable.has(urlKey(action.action_url)) ? '' : action.action_url,
      action_url_text: action.action_url_text,
      entry_label: unavailable.has(urlKey(action.action_url_text)) ? '链接不可用' : action.entry_label,
      owner: action.owner, day: action.day, details: action.details, completion_condition: action.completion_condition,
      fallback: action.fallback, assumptions: action.assumptions,
      sources: pack.sources.filter(source => action.source_ids.includes(source.id)).map(source => ({
        ...pick(source, ['title', 'excerpt', 'checked_at', 'display_url', 'entry_label']),
        url: unavailable.has(urlKey(source.url)) ? '' : safeURL(source.url),
        entry_label: unavailable.has(urlKey(source.display_url)) ? '链接不可用' : source.entry_label,
        traveler: projectTraveler(source, unavailable, true),
        search_excerpt: text(source.search_lead?.excerpt)
      })) };
  }

  function budgetText(value) {
    if (text(value)) return text(value);
    const budget = object(value);
    return [['amount', '金额'], ['currency', '币种'], ['party', '人数'], ['scope', '范围'], ['note', '说明'],
      ['included', '包含'], ['excluded', '不含']].map(([key, name]) => {
      const entry = text(budget[key]) || strings(budget[key]).join('、'); return entry ? `${name}：${entry}` : '';
    }).filter(Boolean).join('；');
  }

  function projectPlan(value, options = {}) {
    const plan = object(value);
    const unavailable = preparationAPI.unavailableSourceURLs(preparationAPI.planSources(plan), options.unavailableSources || options.helpers?.unavailableSources);
    const validated = preparationAPI.validatedSourceURLs(preparationAPI.planSources(plan));
    const days = rows(plan.days).map((day, index) => ({
      ...pick(day, ['date', 'city', 'lodging', 'rationale']), index,
      number: text(day.day) || String(index + 1), title: text(day.title) || '当天主题尚待补充', items: rows(day.items).map(item => ({ ...projectItem(item), details: presentationAPI.itemDetails(plan, item, unavailable) }))
    }));
    const decisions = rows(plan.decisions).map(item => ({ ...pick(item, ['category', 'choice', 'reason', 'status']), supply_ids: strings(item.supply_ids) }));
    const pack = preparationAPI.projectPack(plan.preparation_pack, unavailable);
    const prioritized = pack ? pack.actions.map(action => projectPackAction(action, pack, unavailable)) : [];
    const legacy = preparationAPI.legacyRemainder(plan, pack).map(item => projectPreparation(item, 'preparation', unavailable, rows(plan.days)));
    const preparation = [...prioritized, ...legacy];
    const linkedLegacy = pack ? legacy.filter(item => item.day || item.details.length) : legacy;
    const actions = [...prioritized, ...linkedLegacy].filter(item => item.status === 'user_action' || item.owner === 'user' && ['ready', 'needs_user'].includes(item.status));
    const gaps = [...prioritized.filter(item => item.status === 'needs_research'),
      ...linkedLegacy.filter(item => !['user_action', 'not_applicable', 'provided', 'proposed'].includes(item.status))];
    const supplies = rows(plan.supplies).map(item => projectSupply(item, unavailable, validated));
    const now = new Date();
    const today = [now.getFullYear(), String(now.getMonth() + 1).padStart(2, '0'), String(now.getDate()).padStart(2, '0')].join('-');
    const calendarWarning = days.some(day => /^\d{4}-\d{2}-\d{2}$/.test(day.date) && day.date < today)
      ? '这份方案包含已过去的日期；如用于下一次出行，需要先确认年份和出行日期。不要据此预订。' : '';
    return { title: text(plan.title) || '你的旅行手册', summary: text(plan.summary), budget: budgetText(plan.budget),
      days, decisions, preparation, actions, gaps, supplies, calendarWarning,
      preparationSummary: pack ? `${pack.summary}\n本次为重点准备，不代表全部事项已齐；原方案未覆盖事项保留原状态。` : '', preparationBoundary: pack?.boundary || '',
      warnings: [calendarWarning, ...strings(object(plan.readiness).warnings)].filter(Boolean),
      signature: { ...pick(plan.signature, ['title', 'why', 'tradeoff']), sequence: strings(object(plan.signature).sequence) },
      routes: rows(plan.routes).map(route => ({ ...pick(route, ['day', 'status', 'mode', 'provider', 'note']),
        ...projectURLs(route, ['source_url'], unavailable, validated) })),
      counts: { days: days.length, items: days.reduce((count, day) => count + day.items.length, 0), actions: actions.length, gaps: gaps.length } };
  }

  const paragraph = (value, className = '') => text(value) ? `<p${className ? ` class="${className}"` : ''}>${escapeHTML(value)}</p>` : '';
  const heading = (level, value) => `<h${level}>${escapeHTML(value)}</h${level}>`;
  const htmlList = (values, ordered = false) => values.length ? `<${ordered ? 'ol' : 'ul'}>${values.map(value => `<li>${escapeHTML(value)}</li>`).join('')}</${ordered ? 'ol' : 'ul'}>` : '';
  const href = (title, url) => safeURL(url) ? `<a href="${escapeHTML(safeURL(url))}" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer">${escapeHTML(title)} ↗</a>` : '';
  const section = (title, body) => `<section>${heading(2, title)}${body}</section>`;
  const when = item => [item.time || '时间待定', item.end_time ? `至 ${item.end_time}` : ''].filter(Boolean).join(' ');
  const credit = supply => [supply.image_attribution, supply.image_author, supply.image_license].filter(Boolean).join(' · ') || '图片署名未返回，见来源页面';
  const travelerHTML = traveler => traveler ? paragraph(traveler.notice)
    + paragraph(`来源署名：${traveler.author_hint}`)
    + (traveler.citation_url ? href('查看原帖', traveler.citation_url) : paragraph(`原帖地址（不可点击）：${traveler.source_ref}`)) : '';
  const travelerText = traveler => traveler ? [traveler.notice, `来源署名：${traveler.author_hint}`,
    traveler.citation_url ? `查看原帖：${traveler.citation_url}` : `原帖地址（不可点击）：${traveler.source_ref}`].join('\n') : '';

  function sourceLinks(ids, plan) {
    return plan.supplies.filter(supply => ids.includes(supply.id)).map(supply =>
      travelerHTML(supply.traveler) || href(supply.name || '地点来源', supply.source_url)
      || paragraph(supply.source_url_text ? `${supply.name || '地点'}来源不可用：${supply.source_url_text}` : '')).filter(Boolean).join(' · ');
  }

  function itemMapURL(item, plan) {
    const supply = plan.supplies.find(value => value.id === item.supply_id);
    const query = Number.isFinite(supply?.lat) && Number.isFinite(supply?.lng)
      ? `${supply.lat},${supply.lng}` : [item.guide.meeting_point, supply?.name_zh, supply?.name, item.title].filter(Boolean).join(' ');
    return query ? safeURL(`https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(query)}`) : '';
  }

  function exportItem(item, plan) {
    const guide = item.guide; const experience = item.experience;
    return `<article class="export-item">${heading(4, `${when(item)} · ${item.title}`)}${paragraph(item.why)}
      ${paragraph(item.transport ? `交通：${item.transport}` : '')}${paragraph(guide.meeting_point ? `集合／入口：${guide.meeting_point}` : '')}
      ${htmlList(guide.steps, true)}${paragraph(guide.bring.length ? `随身带：${guide.bring.join('、')}` : '')}
      ${paragraph(guide.booking_note ? `办理说明：${guide.booking_note}` : '')}${paragraph(item.preparation ? `准备：${item.preparation}` : '')}
      ${paragraph(experience.background)}${htmlList(experience.what_to_do)}${paragraph(experience.personal_fit)}
      ${paragraph(experience.photo_tip ? `观察／拍照：${experience.photo_tip}` : '')}${paragraph(experience.practical_tip ? `到现场：${experience.practical_tip}` : '')}
      ${htmlList(item.details.length ? preparationAPI.detailLines(item.details) : ['这些细节还没核实'])}
      ${rows(item.details).flatMap(detail => rows(detail.source_refs)).map(source => href('查看细节来源', source.url)).join('')}
      ${href('打开地图', itemMapURL(item, plan))} ${sourceLinks([item.supply_id], plan)}</article>`;
  }

  function exportPreparation(item, plan) {
    return `<article>${heading(3, item.task)}${paragraph(label(item.status), 'status')}${paragraph(item.why)}
      ${paragraph(item.when ? `何时办理：${item.when}` : item.status === 'user_action' ? '办理时间尚未给出。' : '')}
      ${htmlList(item.steps, true)}${paragraph(item.materials.length ? `所需材料：${item.materials.join('、')}` : '')}
      ${htmlList(preparationAPI.detailLines(item.details))}
      ${rows(item.details).flatMap(detail => rows(detail.source_refs)).map(source => href('查看细节来源', source.url)).join('')}
      ${paragraph(item.day ? `关联：第 ${item.day} 天` : '')}${paragraph(item.owner ? `责任方：${item.owner === 'user' ? '本人办理或准备' : '旅行助手整理资料'}` : '')}
      ${paragraph(item.completion_condition ? `完成标志：${item.completion_condition}` : '')}${paragraph(item.fallback ? `不顺利时：${item.fallback}` : '')}
      ${htmlList(strings(item.assumptions).map(value => `待确认假设：${value}`))}
      ${rows(item.sources).map(source => `${source.url ? href(source.title || '查看来源', source.url) : paragraph(source.title)}${travelerHTML(source.traveler)}${paragraph(source.entry_label)}${paragraph(!source.url ? source.display_url : '')}${paragraph(source.excerpt)}${paragraph(source.search_excerpt ? `搜索线索（未直接读取）：${source.search_excerpt}` : '')}${paragraph(`查阅时间：${source.checked_at || '未提供'}；页面资料不代表订单或库存确认。`)}`).join('')}
      ${href(item.entry_label || '已读取页面', item.action_url)}
      ${paragraph(!item.action_url && item.action_url_text ? `${item.entry_label}（不可点击）：${item.action_url_text}` : '')}
      ${paragraph(item.source_url_text ? `原方案资料线索（未验证，不可点击）：${item.source_url_text}` : '')}
      ${href('资料来源', item.source_url)} ${sourceLinks(item.supply_ids, plan)}</article>`;
  }

  function exportSupply(supply) {
    const image = supply.image_url ? `<figure><img src="${escapeHTML(supply.image_url)}" alt="${escapeHTML(supply.name)}" loading="lazy" referrerpolicy="no-referrer"><figcaption>${escapeHTML(credit(supply))}。历史来源图片，非实时画面。
      ${href('图片来源', supply.image_source_url || supply.source_url)} ${href('图片许可', supply.image_license_url)}</figcaption></figure>` : '';
    return `<article>${heading(3, supply.name || '地点资料')}${paragraph(supply.provider)}${paragraph(supply.address)}${paragraph(supply.description)}
      ${travelerHTML(supply.traveler) || href('地点来源', supply.source_url)}${image}
      ${['source_url', 'image_url', 'image_source_url', 'image_license_url'].map(key => paragraph(
        supply[`${key}_text`] ? `来源不可用（不可点击）：${supply[`${key}_text`]}` : '')).join('')}</article>`;
  }

  function buildDocument(value, options = {}) {
    const plan = projectPlan(value, options); const suppliedDate = new Date(options.exportedAt || Date.now());
    const date = Number.isNaN(suppliedDate.getTime()) ? new Date().toISOString() : suppliedDate.toISOString();
    const revision = text(options.revision) || '未标注';
    const overview = plan.days.map(day => `<li><a href="#day-${day.index}">第 ${escapeHTML(day.number)} 天 · ${escapeHTML(day.title)}</a>
      ${paragraph([day.date, day.city, day.lodging ? `住宿：${day.lodging}` : ''].filter(Boolean).join(' · '))}</li>`).join('');
    const days = plan.days.map(day => `<section class="export-day" id="day-${day.index}">${heading(2, `第 ${day.number} 天 · ${day.title}`)}
      ${paragraph([day.date || '日期未标注', day.city, day.lodging ? `住宿：${day.lodging}` : ''].filter(Boolean).join(' · '))}${paragraph(day.rationale)}
      ${day.items.map(item => exportItem(item, plan)).join('') || paragraph('这一天的具体安排尚待补充。')}</section>`).join('');
    const choices = plan.decisions.map(item => `<article>${heading(3, categories[item.category] || '旅行选择')}${paragraph(item.choice)}${paragraph(label(item.status), 'status')}${paragraph(item.reason)}${sourceLinks(item.supply_ids, plan)}</article>`).join('');
    return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
      <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src https:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
      <meta name="referrer" content="no-referrer"><title>${escapeHTML(plan.title)} · 旅行手册</title><style>${documentStyles}</style></head><body><main>
      <header>${paragraph('随身旅行手册', 'status')}${heading(1, plan.title)}${paragraph(plan.summary)}${paragraph(`版本 ${revision} · 导出于 ${date}${options.draft ? ' · 未完成草案' : ''}`, 'meta')}
      <aside>这是静态快照，后续修改不会更新此文件。文字可离线阅读，图片和外部链接需要联网。此手册没有执行预订或支付；推荐、用户已提供与供应方确认不是同一状态。时间为规划建议，不代表营业时间。</aside></header>
      ${section('全程总览', `<ol>${overview}</ol>${paragraph(plan.budget ? `预算口径：${plan.budget}` : '预算金额、人数、币种与包含范围未单独汇总；请以当前旅行选择中的明确口径为准。')}`)}
      ${section('尚待落实', paragraph(`${plan.counts.actions} 项需要本人操作 · ${plan.counts.gaps} 条资料缺口。未列出的事项不代表已完成。`) + htmlList(plan.warnings) + plan.gaps.map(item => exportPreparation(item, plan)).join(''))}
      ${days || section('每日指南', paragraph('具体日程尚待补充。'))}
      ${section('全程选择', choices || paragraph('旅行选择尚待补充。'))}
      ${section('出发准备', paragraph(plan.preparationSummary) + paragraph(plan.preparationBoundary) + (plan.preparation.map(item => exportPreparation(item, plan)).join('') || paragraph('尚未列出准备事项，不代表无需准备。')))}
      ${plan.signature.title ? section(plan.signature.title, paragraph(plan.signature.why) + htmlList(plan.signature.sequence, true) + paragraph(plan.signature.tradeoff)) : ''}
      ${plan.routes.length ? section('路线参考', plan.routes.map(route => `<article>${heading(3, `第 ${route.day} 天`)}${paragraph([route.provider, route.note].filter(Boolean).join(' · '))}${href('路线来源', route.source_url)}${paragraph(route.source_url_text ? `路线来源不可用：${route.source_url_text}` : '')}${paragraph('路网参考非实时导航；没有来源的路段不推断通行方式或用时。')}</article>`).join('')) : ''}
      ${section('地点资料与图片署名', plan.supplies.map(exportSupply).join('') || paragraph('尚无可导出的来源资料。'))}
      <footer>旅行手册 · 本文件仅包含当前方案的公开展示字段，不包含会话凭证、原始上传材料或内部工具记录。</footer></main></body></html>`;
  }

  const documentStyles = `*{box-sizing:border-box}body{margin:0;background:#f6f4ed;color:#213c38;font:15px/1.85 system-ui,sans-serif}main{max-width:900px;margin:auto;padding:38px 24px}h1,h2,h3,h4{line-height:1.5}h1{font-size:32px}h2{font-size:24px}h3{font-size:18px}h4{font-size:16px}section{border-top:1px solid #d8dcd1;margin-top:30px;padding-top:14px}article{padding:12px 0;border-bottom:1px solid #d8dcd1}p{white-space:pre-line;overflow-wrap:anywhere}a{color:#255b4d;overflow-wrap:anywhere;display:inline-block;min-height:44px;padding:8px 0}aside{background:#eee4d5;padding:16px;font-size:13px}figure{margin:16px 0}img{display:block;max-width:100%;max-height:350px;object-fit:contain}figcaption,.meta,.status,footer{font-size:12px;color:#56665b}footer{margin-top:30px}li+li{margin-top:7px}@media(max-width:450px){main{padding:22px 16px}h1{font-size:27px}h2{font-size:22px}}@media print{body{background:white}main{max-width:none;padding:0}a{min-height:0;padding:0}article,figure{break-inside:avoid}h2,h3,h4{break-after:avoid}aside{border:1px solid #ccc}}`;

  const plainLines = values => values.filter(Boolean).join('\n');
  const plainSources = (ids, plan) => plan.supplies.filter(supply => ids.includes(supply.id) && (supply.source_url || supply.source_url_text))
    .map(supply => travelerText(supply.traveler) || `${supply.name || '地点来源'}${supply.source_url ? '' : '来源不可用'}：${supply.source_url || supply.source_url_text}`);
  function plainItem(item, plan) {
    const guide = item.guide; const experience = item.experience;
    return plainLines([`${when(item)} · ${item.title}`, item.why,
      item.transport && `交通：${item.transport}`, guide.meeting_point && `集合／入口：${guide.meeting_point}`,
      ...guide.steps.map((step, index) => `${index + 1}. ${step}`), guide.bring.length && `随身带：${guide.bring.join('、')}`,
      guide.booking_note && `办理说明：${guide.booking_note}`, item.preparation && `准备：${item.preparation}`,
      experience.background, ...experience.what_to_do.map(step => `体验：${step}`), experience.personal_fit,
      experience.photo_tip && `观察／拍照：${experience.photo_tip}`, experience.practical_tip && `到现场：${experience.practical_tip}`,
      ...(item.details.length ? preparationAPI.detailLines(item.details) : ['这些细节还没核实']),
      itemMapURL(item, plan) && `地图：${itemMapURL(item, plan)}`,
      ...plainSources([item.supply_id], plan)]);
  }
  function plainPreparation(item, plan) {
    return plainLines([`${item.task}（${label(item.status)}）`, item.why, item.when && `何时办理：${item.when}`,
      ...item.steps.map((step, index) => `${index + 1}. ${step}`), item.materials.length && `材料：${item.materials.join('、')}`,
      ...preparationAPI.detailLines(item.details),
      item.day && `关联：第 ${item.day} 天`, item.owner && `责任方：${item.owner === 'user' ? '本人办理或准备' : '旅行助手整理资料'}`,
      item.completion_condition && `完成标志：${item.completion_condition}`, item.fallback && `不顺利时：${item.fallback}`,
      ...strings(item.assumptions).map(value => `待确认假设：${value}`),
      ...rows(item.sources).map(source => plainLines([source.title, source.entry_label, travelerText(source.traveler), source.url || source.display_url,
        source.excerpt, source.search_excerpt && `搜索线索（未直接读取）：${source.search_excerpt}`, `查阅时间：${source.checked_at || '未提供'}；页面资料不代表订单或库存确认。`])),
      item.action_url && `${item.entry_label}：${item.action_url}`,
      !item.action_url && item.action_url_text && `${item.entry_label}（不可点击）：${item.action_url_text}`,
      item.source_url_text && `原方案资料线索（未验证，不可点击）：${item.source_url_text}`,
      item.source_url && `资料来源：${item.source_url}`, ...plainSources(item.supply_ids, plan)]);
  }
  function buildText(value, options = {}) {
    const plan = projectPlan(value, options); const stamp = new Date(options.exportedAt || Date.now());
    const time = Number.isNaN(stamp.getTime()) ? new Date().toISOString() : stamp.toISOString();
    const days = plan.days.map(day => plainLines([`第 ${day.number} 天 · ${day.title}`,
      [day.date || '日期未标注', day.city, day.lodging && `住宿：${day.lodging}`].filter(Boolean).join(' · '), day.rationale,
      ...day.items.map(item => plainItem(item, plan)), !day.items.length && '这一天的具体安排尚待补充。']));
    const choices = plan.decisions.map(item => plainLines([`${categories[item.category] || '旅行选择'} · ${item.choice}（${label(item.status)}）`, item.reason, ...plainSources(item.supply_ids, plan)]));
    const sources = plan.supplies.map(supply => plainLines([supply.name || '地点资料', supply.provider, supply.address, supply.description,
      ...['source_url', 'image_url', 'image_source_url', 'image_license_url'].map(key =>
        supply[`${key}_text`] && `来源不可用（不可点击）：${supply[`${key}_text`]}`),
      travelerText(supply.traveler) || supply.source_url && `地点来源：${supply.source_url}`, supply.image_url && `来源图片：${supply.image_url}`,
      supply.image_url && `署名：${credit(supply)}。历史照片，非实时画面。`,
      supply.image_source_url && `图片来源：${supply.image_source_url}`, supply.image_license_url && `图片许可：${supply.image_license_url}`]));
    return [plan.title, plan.summary, `版本 ${text(options.revision) || '未标注'} · ${time}${options.draft ? ' · 未完成草案' : ''}`,
      '这是静态快照，后续修改不会自动更新。外部链接与来源图片需要联网。没有执行预订或支付；时间为规划建议，不代表营业时间。',
      `全程总览\n${plan.counts.days} 天 · ${plan.counts.items} 段安排 · ${plan.counts.actions} 项本人操作 · ${plan.counts.gaps} 条资料待核实。`,
      plan.budget ? `预算口径：${plan.budget}` : '预算金额、人数、币种与范围尚未单独汇总。',
      '每日指南', ...days, '全程选择', ...choices, '出发准备', plan.preparationSummary, plan.preparationBoundary,
      ...(plan.preparation.length ? plan.preparation.map(item => plainPreparation(item, plan)) : ['尚未列出准备事项，不代表无需准备。']),
      plan.gaps.length && '资料缺口（不是交给用户重新搜索的待办）', ...plan.gaps.map(item => plainPreparation(item, plan)),
      ...plan.warnings, plan.signature.title, plan.signature.why, ...plan.signature.sequence, plan.signature.tradeoff,
      ...plan.routes.map(route => plainLines([`第 ${route.day} 天路线参考`, route.provider, route.note, route.source_url,
        route.source_url_text && `路线来源不可用：${route.source_url_text}`, '非实时导航；未取得的路段与用时不作推断。'])),
      '地点资料与图片署名', ...sources].filter(Boolean).join('\n\n');
  }

  // DOM is created only by mount/render, so pure functions work in Node tests.
  let mounted = false; let view = 'overview'; let callbacks = {}; let currentPlan = {}; let renderOptions = {}; let lastTripId;
  const $ = id => document.getElementById(id);
  const node = (tag, className, value) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value !== undefined) element.textContent = text(value);
    return element;
  };
  function button(title, callback, className = 'handbook-button') {
    const element = node('button', className, title); element.type = 'button'; element.addEventListener('click', callback); return element;
  }
  function appendText(parent, title, value) {
    if (text(value)) parent.append(node('p', 'handbook-note', title ? `${title} · ${text(value)}` : value));
  }
  function appendList(parent, values, ordered = false) {
    if (!values.length) return;
    const element = node(ordered ? 'ol' : 'ul', 'handbook-list'); values.forEach(value => element.append(node('li', '', value))); parent.append(element);
  }
  function publicLink(title, value) {
    const url = safeURL(value);
    if (!url) return null;
    const anchor = node('a', 'handbook-source', `${title} ↗`); anchor.href = url; anchor.target = '_blank';
    anchor.rel = 'noopener noreferrer'; anchor.referrerPolicy = 'no-referrer'; return anchor;
  }
  function appendSources(parent, item, plan) {
    const links = node('div', 'handbook-sources');
    if (!item.action_url) appendText(parent, '原方案入口（未验证，不可点击）', item.action_url_text);
    appendText(parent, '原方案资料线索（未验证，不可点击）', item.source_url_text);
    plan.supplies.filter(supply => item.supply_ids.includes(supply.id) && supply.source_url_text)
      .forEach(supply => appendText(parent, `${supply.name || '地点'}来源不可用`, supply.source_url_text));
    const supplies = plan.supplies.filter(supply => item.supply_ids.includes(supply.id));
    [...supplies, ...rows(item.sources)].filter(source => source.traveler).forEach(source => {
      appendText(parent, '', source.traveler.notice); appendText(parent, '来源署名', source.traveler.author_hint);
      const citation = publicLink('查看原帖', source.traveler.citation_url);
      if (citation) links.append(citation);
      else appendText(parent, '原帖地址（不可点击）', source.traveler.source_ref);
    });
    [publicLink(item.entry_label || '已读取页面', item.action_url), publicLink('资料来源', item.source_url),
      ...supplies.filter(supply => !supply.traveler).map(supply => publicLink(supply.name || '地点来源', supply.source_url))]
      .filter(Boolean).forEach(link => links.append(link));
    if (links.children.length) parent.append(links);
  }

  function mountCopyPanel(nav) {
    const panel = node('section', 'handbook-copy-panel'); panel.id = 'handbook-copy-panel'; panel.hidden = true;
    panel.setAttribute('aria-labelledby', 'handbook-copy-label');
    const title = node('label', '', '完整旅行手册 · 可手动选取'); title.id = 'handbook-copy-label'; title.htmlFor = 'handbook-copy-text';
    const note = node('p', 'handbook-note'); note.id = 'handbook-copy-note';
    const field = node('textarea', 'handbook-copy-text'); field.id = 'handbook-copy-text'; field.readOnly = true;
    field.setAttribute('aria-describedby', 'handbook-copy-note'); field.spellcheck = false;
    const actions = node('div', 'handbook-copy-actions');
    actions.append(button('全选完整文本', () => { field.focus(); field.select(); }, 'handbook-download'),
      button('收起文本', () => { panel.hidden = true; $('handbook-copy').setAttribute('aria-expanded', 'false'); $('handbook-copy').focus(); }, 'handbook-text-button'));
    panel.append(title, note, actions, field); nav.after(panel);
  }

  function refreshCopyPanel() {
    $('handbook-copy-text').value = buildText(currentPlan, renderOptions);
    $('handbook-copy-note').textContent = `当前版本 ${text(renderOptions.revision) || '未标注'} · 静态快照。页面收到新版本时会更新此处；已复制或下载的旧文本不会自动更新。可全选后手动复制。`;
  }

  function mount(options = {}) {
    callbacks = options;
    if (mounted) return;
    const container = $('plan-content'); const daily = container?.querySelector('.itinerary-section');
    if (!container || !daily || !$('story')) return;
    daily.id ||= 'handbook-daily';
    const nav = node('nav', 'handbook-nav'); nav.id = 'handbook-nav'; nav.setAttribute('aria-label', '旅行手册视图');
    const views = node('div', 'handbook-views');
    [['overview', '全程总览'], ['daily', '每日指南'], ['prepare', '出发准备']].forEach(([name, title]) => {
      const control = button(title, () => selectView(name), 'handbook-tab'); control.dataset.handbookView = name;
      control.setAttribute('aria-controls', name === 'daily' ? daily.id : `handbook-${name}`); views.append(control);
    });
    const download = button('下载行程手册', downloadDocument, 'handbook-download'); download.id = 'handbook-download';
    const copy = button('复制完整手册', copyDocument, 'handbook-download'); copy.id = 'handbook-copy';
    copy.setAttribute('aria-controls', 'handbook-copy-panel'); copy.setAttribute('aria-expanded', 'false');
    const actions = node('div', 'handbook-actions'); actions.append(copy, download);
    nav.append(views, actions); $('story').after(nav); mountCopyPanel(nav);
    ['overview', 'prepare'].forEach(name => {
      const panel = node('section', `handbook-panel handbook-${name}`); panel.id = `handbook-${name}`;
      panel.setAttribute('aria-labelledby', `handbook-${name}-title`); daily.before(panel);
    });
    mounted = true; selectView(view);
  }

  function selectView(next) {
    if (!['overview', 'daily', 'prepare'].includes(next)) return;
    view = next;
    if (!mounted) return;
    $('handbook-overview').hidden = view !== 'overview'; $('handbook-prepare').hidden = view !== 'prepare';
    $('plan-content').querySelector('.itinerary-section').hidden = view !== 'daily';
    $('handbook-nav').querySelectorAll('[data-handbook-view]').forEach(control => control.setAttribute('aria-pressed', String(control.dataset.handbookView === view)));
    callbacks.onView?.(view);
  }

  function sectionHead(id, title, note) {
    const header = node('div', 'handbook-heading'); const heading = node('h2', '', title); heading.id = id;
    header.append(heading); if (note) header.append(node('p', 'handbook-note', note)); return header;
  }
  function renderGaps(plan) {
    const block = node('aside', 'handbook-readiness');
    block.append(node('h3', '', plan.counts.actions || plan.counts.gaps ? '这趟旅行，仍有待落实的安排' : '行前状态仍需留意'));
    appendText(block, '', `${plan.counts.actions} 项本人操作 · ${plan.counts.gaps} 条资料待核实。方案建议不等于已预约。`);
    if (plan.calendarWarning) appendText(block, '日期需确认', plan.calendarWarning);
    if (plan.gaps.length) appendText(block, '当前缺口', shortText(plan.gaps[0].task, 90));
    block.append(button('查看准备与具体缺口 →', () => selectView('prepare'), 'handbook-text-button')); return block;
  }

  function dayRow(day, plan) {
    const row = node('li', 'handbook-day-row');
    const index = node('div', 'handbook-day-index', `第 ${day.number} 天`);
    index.append(node('span', '', day.date || '日期未标注'));
    const body = node('div', 'handbook-day-body'); body.append(node('h3', '', day.title));
    if (day.city) appendText(body, '所在城市', day.city);
    const sequence = node('p', 'handbook-day-sequence', day.items.map(item => item.title).join(' → ') || '当天具体安排尚待补充。');
    body.append(sequence);
    appendText(body, '住宿', day.lodging || (plan.decisions.some(item => item.category === 'lodging') ? '见全程住宿选择；此日未单独标注' : '尚未明确安排'));
    const open = button('看当天指南 →', () => { callbacks.onDay?.(day.index); selectView('daily'); }, 'handbook-day-open');
    open.setAttribute('aria-label', `查看第 ${day.number} 天：${day.title}`); row.append(index, body, open); return row;
  }

  function renderChoices(plan) {
    const section = node('section', 'handbook-choices'); section.append(node('h3', '', '已选的方向，与还没落实的部分'));
    plan.decisions.forEach(choice => {
      const row = node('div', 'handbook-choice'); row.append(node('span', 'handbook-choice-kind', categories[choice.category] || '安排'));
      const body = node('div'); const content = dedupeNames(choice.choice || '具体选择尚待补充');
      body.append(node('p', '', shortText(content, 90)));
      const meta = node('div', 'handbook-choice-meta'); meta.append(node('small', 'handbook-status', label(choice.status)));
      if (content.length > 90 || choice.reason) {
        const detail = node('wa-details', 'handbook-choice-detail'); detail.append(node('summary', '', '完整选择与依据'));
        if (content.length > 90) appendText(detail, '', content);
        appendText(detail, '依据', dedupeNames(choice.reason)); meta.append(detail);
      }
      body.append(meta);
      row.append(body); section.append(row);
    });
    if (!plan.decisions.length) appendText(section, '', '全程住宿、交通等选择尚待补充。');
    return section;
  }

  // 模型文案偶发「Crowne Plaza (Crowne Plaza)」式自我重复：括号内与前文完全一致时去掉括号
  function dedupeNames(value) {
    return text(value).replace(/([A-Za-z0-9][A-Za-z0-9 &'.,-]{1,60}?) \(\1\)/g, '$1');
  }

  function shortText(value, limit) {
    const source = text(value); return source.length > limit ? `${source.slice(0, limit)}…` : source;
  }

  function revealAction(root, actionId) {
    if (!actionId || !root?.querySelectorAll) return null;
    const target = [...root.querySelectorAll('[data-action-id]')].find(item => item.dataset?.actionId === actionId) || null;
    if (!target) return null;
    for (let parent = target.parentElement; parent; parent = parent.parentElement) {
      if (parent.tagName === 'DETAILS' || parent.tagName === 'WA-DETAILS') parent.open = true;
    }
    for (const detail of target.querySelectorAll?.('details') || []) {
      if (detail.classList?.contains('preparation-operation') || detail.className === 'preparation-operation') detail.open = true;
    }
    return target;
  }

  function renderOverview(plan) {
    if (typeof window !== 'undefined' && window.TravelDelivery) {
      const section = $('handbook-overview');
      const openView = (next, actionId = '') => {
        selectView(next);
        const target = revealAction(document, actionId);
        const heading = target || $(next === 'daily' ? 'itinerary-title' : 'handbook-prepare-title');
        if (heading) { heading.setAttribute('tabindex', '-1'); heading.focus({ preventScroll: true }); heading.scrollIntoView({ block: 'center' }); }
        $('handbook-nav').scrollIntoView({ block: 'start' });
      };
      window.TravelDelivery.render(section, plan, { tripId: renderOptions.tripId,
        onDay: index => { callbacks.onDay?.(index); openView('daily'); },
        onPrepare: actionId => openView('prepare', actionId)
      });
      const choices = node('wa-details', 'delivery-choices');
      choices.append(node('summary', '', '住宿、吃饭、交通与预算 · 展开查看'), renderChoices(plan));
      appendText(choices, '预算口径', plan.budget || '金额与包含范围尚待补充。');
      section.append(choices); return;
    }
    const section = $('handbook-overview'); const sequence = node('ol', 'handbook-days');
    plan.days.forEach(day => sequence.append(dayRow(day, plan)));
    if (!plan.days.length) sequence.append(node('li', 'handbook-empty', '具体日程尚待补充。'));
    section.replaceChildren(sectionHead('handbook-overview-title', '整趟旅行，一眼看清',
      `${plan.counts.days} 天 · ${plan.counts.items} 段安排 · 时间为建议，未标注的日期和住宿不作推断。`), renderGaps(plan), sequence, renderChoices(plan));
    appendText(section, '预算口径', plan.budget || '尚未单独汇总金额、人数、币种与包含范围；不把推荐价格视为最终报价。');
  }

  function preparationRow(item, plan) {
    const row = node('article', 'handbook-preparation-row'); const heading = node('div', 'handbook-preparation-title');
    row.dataset.actionId = item.id;
    heading.append(node('h4', '', item.task), node('span', 'handbook-status', label(item.status))); row.append(heading);
    appendText(row, '', item.why); appendText(row, '何时', item.when);
    appendList(row, item.steps, true); appendText(row, '需要材料', item.materials.join('、'));
    appendList(row, item.details.length ? preparationAPI.detailLines(item.details) : ['这些细节还没核实']);
    rows(item.details).flatMap(detail => rows(detail.source_refs)).forEach(source => {
      const anchor = publicLink('查看细节来源', source.url); if (anchor) row.append(anchor);
    });
    if (item.status === 'user_action') {
      const missing = [!item.when && '办理时间', !item.steps.length && '具体步骤'].filter(Boolean);
      if (missing.length) appendText(row, '操作参数待补齐', `${missing.join('、')}尚未给出；这部分仍是方案的信息缺口。`);
    }
    appendSources(row, item, plan); return row;
  }
  function preparationGroup(title, note, items, plan, empty) {
    const section = node('section', 'handbook-preparation-group'); section.append(node('h3', '', title)); appendText(section, '', note);
    items.forEach(item => section.append(preparationRow(item, plan)));
    if (!items.length) appendText(section, '', empty); return section;
  }
  function renderPreparation(plan) {
    const panel = $('handbook-prepare');
    const research = preparationGroup(`资料待核实 · ${plan.gaps.length}`, '这些是方案尚未解决的信息缺口，不是让你重新搜索的待办。没有核实前，相关安排仍是条件建议。', plan.gaps, plan, '当前未列出资料缺口，不等于外部服务已全部确认。');
    research.classList.add('handbook-research-gaps');
    panel.replaceChildren(sectionHead('handbook-prepare-title', '出发前，还要落实什么', '本人确认、支付和实物准备，与方案需要补齐的研究分开。此处没有执行预订，也不提供虚假的完成勾选。'),
      preparationGroup(`需要你行动 · ${plan.actions.length}`, '只列当前方案明确交给本人完成的操作。', plan.actions, plan,
        plan.preparation.length ? '当前方案没有列出本人操作；不代表一切已落实。' : '尚未列出准备事项，不代表无需准备。'), research);
    const other = plan.preparation.filter(item => ['not_applicable', 'provided', 'proposed'].includes(item.status));
    if (other.length) {
      const more = node('wa-details', 'handbook-other'); more.append(node('summary', '', `其他准备说明 · ${other.length}`));
      other.forEach(item => more.append(preparationRow(item, plan))); panel.append(more);
    }
    if (plan.warnings.length) { const warnings = node('aside', 'handbook-warnings'); warnings.append(node('h3', '', '使用前仍需注意')); appendList(warnings, plan.warnings); panel.append(warnings); }
  }

  function render(plan, options = {}) {
    currentPlan = object(plan); renderOptions = options;
    if (options.tripId !== undefined && text(options.tripId) !== lastTripId) { view = 'overview'; lastTripId = text(options.tripId); }
    if (!mounted) return;
    const projected = projectPlan(plan, options); renderOverview(projected); renderPreparation(projected);
    $('handbook-download').disabled = !projected.days.length;
    $('handbook-copy').disabled = !projected.days.length;
    if (!$('handbook-copy-panel').hidden) refreshCopyPanel();
    selectView(view); // Same-version panels refresh together, selected day belongs to the parent.
  }

  function downloadDocument() {
    if (!projectPlan(currentPlan).days.length) return;
    let url = '';
    try {
      const html = buildDocument(currentPlan, renderOptions);
      url = URL.createObjectURL(new Blob([html], { type: 'text/html;charset=utf-8' }));
      const anchor = node('a'); anchor.href = url;
      anchor.download = `${(text(currentPlan.title) || '旅行手册').replace(/[<>:"/\\|?*\x00-\x1f]/g, '-').slice(0, 70)}-旅行手册.html`;
      document.body.append(anchor); anchor.click(); anchor.remove();
      callbacks.announce?.('已交给浏览器下载 HTML 行程手册（静态快照）。图片与外部链接需要联网；没有执行预订。');
    } catch { callbacks.announce?.('文件未能保存，请稍后重试。当前旅行方案仍保留在页面中。'); }
    finally { if (url) setTimeout(() => URL.revokeObjectURL(url), 1000); }
  }

  async function copyDocument() {
    if (!projectPlan(currentPlan).days.length) return;
    $('handbook-copy-panel').hidden = false; $('handbook-copy').setAttribute('aria-expanded', 'true'); refreshCopyPanel();
    try {
      await navigator.clipboard.writeText($('handbook-copy-text').value);
      callbacks.announce?.('已尝试复制；如未能粘贴，可从下方完整文本选取。');
    } catch { callbacks.announce?.('已尝试复制，但浏览器未允许写入；可从下方完整文本选取并手动复制。'); }
  }

  return { mount, render, selectView, projectPlan, buildDocument, buildText, revealAction };
});
