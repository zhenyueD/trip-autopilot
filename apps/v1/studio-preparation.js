'use strict';

(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.TravelPreparation = api;
})(typeof window === 'undefined' ? null : window, function () {
  const text = value => typeof value === 'string' || typeof value === 'number' ? String(value).trim() : '';
  const rows = value => Array.isArray(value) ? value.filter(item => item && typeof item === 'object' && !Array.isArray(item)) : [];
  const strings = value => Array.isArray(value) ? value.map(text).filter(Boolean) : [];
  const travelerTypes = new Set(['xiaohongshu_post', 'instagram_post', 'tripadvisor_forum',
    'reddit_post', 'google_maps_review', 'mafengwo_post']);
  const statusLabels = { ready: '资料已备齐 · 不代表已办理', needs_user: '需要你操作', needs_research: '仍需查清',
    user_reported: '本人报告已办 · 未经渠道确认' };
  const detailLabels = { time: '时间', price: '费用', phone: '电话', url: '渠道地址', booking: '预约方式', caution: '注意事项' };
  const sourceLabels = { hotel_official: '酒店资料', transport_authority: '交通机构资料',
    tripadvisor_forum: '旅行者论坛', xiaohongshu_post: '旅行者分享', google_maps_review: '地点评论',
    mafengwo_post: '旅行者攻略', blog_article: '博客攻略', user_text: '用户分享' };
  function projectDetails(value, unavailable = new Set()) {
    return rows(value).filter(row => Object.hasOwn(detailLabels, row.field)).map(row => ({
      field: row.field, label: detailLabels[row.field], value: row.value == null ? null : text(row.value),
      status: row.value == null ? 'unknown' : 'unverified', detail_ids: strings(row.detail_ids),
      source_refs: row.value == null ? [] : rows(row.source_refs).map(source => {
        const display = safeURL(source.source_url);
        return { source_id: text(source.source_id), source_type: text(source.source_type),
          attribution: text(source.attribution) || text(source.author_hint),
          indirectness: text(source.indirectness) || (source.indirect === true ? '间接来源 · 转述' : ''), quote: text(source.quote),
          source_label: sourceLabels[source.source_type] || '来源资料',
          applicability: text(source.applicability?.reason) || text(source.applicability), display_url: display,
          url: source.direct_read === true && source.link_available === true && !unavailable.has(urlKey(display)) ? display : '' };
      }) }));
  }
  function detailLines(details) {
    return rows(details).flatMap(row => [`${row.label || detailLabels[row.field]}：${row.value == null ? '尚未查清 · 未核实' : `${row.value} · 未核实`}`,
      ...rows(row.source_refs).map(source => [source.attribution || '来源署名未提供', source.indirectness,
        source.source_label, source.applicability, source.quote && `原文：${source.quote}`, source.display_url && `来源地址${source.url ? '' : '（不可点击）'}：${source.display_url}`].filter(Boolean).join(' · '))]);
  }
  function renderDetails(parent, details) {
    rows(details).forEach(detail => {
      note(parent, detail.label, detail.value == null ? '尚未查清 · 未核实' : `${detail.value} · 未核实`);
      rows(detail.source_refs).forEach(source => {
        note(parent, '来源署名', [source.attribution || '未提供', source.indirectness].filter(Boolean).join(' · '));
        note(parent, '来源类别', source.source_label);
        note(parent, '适用条件', source.applicability); note(parent, '原文', source.quote);
        if (source.url) parent.append(link('查看细节来源 ↗', source.url));
        else note(parent, '来源地址（不可点击）', source.display_url);
      });
    });
  }
  function renderDetailSummary(parent, details) {
    const summary = node('div', 'inline-material-details');
    if (!rows(details).length) {
      // 未核实 → wa-badge 行内徽标（琥珀点 + 灰字），不再整行米色块包裹
      const badge = node('wa-badge', 'unverified', '这些细节还没核实');
      badge.setAttribute('variant', 'neutral');
      summary.append(badge);
    }
    rows(details).forEach(detail => {
      note(summary, detail.label || detailLabels[detail.field], detail.value == null ? '尚未查清 · 未核实' : `${detail.value} · 未核实`);
      rows(detail.source_refs).forEach(source => {
        note(summary, '来源', [source.attribution || '署名未提供', source.indirectness, source.source_label].filter(Boolean).join(' · '));
        if (source.url) summary.append(link('查看来源 ↗', source.url));
        else if (source.display_url) note(summary, '来源地址（不可点击）', source.display_url);
      });
    });
    parent.append(summary);
  }
  function safeURL(value) {
    try {
      if (typeof value !== 'string' || value.length > 2048 || /[\u0000-\u0020\u007f\\]/.test(value)) return '';
      const decoded = decodeURIComponent(value);
      if (/%[0-9a-f]{2}|[\\\u0000-\u001f\u007f]/i.test(decoded) || /%2e/i.test(value)) return '';
      const url = new URL(value); const host = url.hostname.replace(/\.$/, '');
      if (url.protocol !== 'https:' || url.username || url.password || url.port
        || !host.includes('.') || host.includes(':') || /^[\d.]+$/.test(host)
        || /(^|\.)(localhost|local|internal)$/.test(host)) return '';
      const path = decodeURIComponent(url.pathname);
      if (path.includes(';') || path.split('/').some(part => part !== part.trim())
        || /(?:^|\/)(?:private|admin|internal|home|users|~[^/]*|accounts?|my-account|orders?|login|log-in|sign-in|signin|signout|auth|oauth|reset-password|password-reset|reservations?|confirmations?)(?:\/|$)|(?:^|\/)bookings?\/(?:manage|view|details?|[a-z0-9_-]{16,})(?:\/|$)/i.test(path)) return '';
      const sensitive = /password|passwd|secret|token|session|signat|credential|auth|api.?key|(?:^|[?&#;=])(?:key|pwd|auth|code|sig)=|login|sign.?in|order|booking|confirmation|reservation|密码|口令|订单|验证码|登录|授权|凭证/i;
      if (sensitive.test(decodeURIComponent(url.search + '&' + url.hash))) return '';
      return url.href;
    } catch { return ''; }
  }
  function urlKey(value) {
    try {
      const url = new URL(value);
      if (!['http:', 'https:'].includes(url.protocol)) return '';
      url.protocol = 'https:'; url.hash = ''; url.hostname = url.hostname.replace(/\.$/, '');
      url.pathname = decodeURIComponent(url.pathname);
      const params = [...url.searchParams].filter(([key]) => !/^(utm_|fbclid$|gclid$)/i.test(key));
      params.sort(([a, x], [b, y]) => a.localeCompare(b) || x.localeCompare(y));
      url.search = ''; params.forEach(([key, value]) => url.searchParams.append(key, value));
      return url.href;
    } catch { return ''; }
  }
  const safeTravelerURL = safeURL;
  function directReceipt(source) {
    return source?.status === 'read' && source.method === 'direct_http' && source.http_status === 200
      && source.verification !== 'search_grounded_not_direct_page';
  }
  function validatedSourceURLs(sources) {
    return new Set(rows(sources).filter(directReceipt).map(source =>
      safeURL(source.url || source.source_url || source.source_provenance?.source_ref)).filter(Boolean).map(urlKey));
  }
  function sourceURL(source, unavailable = new Set(), key = 'source_url', validated = new Set()) {
    unavailable = unavailableSourceURLs([source], unavailable);
    const raw = source?.[key] || (key === 'source_url' ? source?.url || source?.source_provenance?.source_ref : '');
    const safe = safeURL(raw);
    const ownReceipt = directReceipt(source)
      && urlKey(source.url || source.source_url || source.source_provenance?.source_ref) === urlKey(safe);
    return safe && !unavailable.has(urlKey(safe)) && (ownReceipt || validated.has(urlKey(safe))) ? safe : '';
  }
  function projectTraveler(source, unavailable = new Set()) {
    unavailable = unavailableSourceURLs([source], unavailable);
    const provenance = source?.source_provenance;
    if (!provenance || !travelerTypes.has(provenance.source_type)) return null;
    const ref = safeTravelerURL(provenance.source_ref);
    const url = safeTravelerURL(source.url || source.source_url || provenance.source_ref);
    const failed = unavailable.has(urlKey(ref)) || unavailable.has(urlKey(url)) || ['blocked', 'unavailable'].includes(source.status)
      || Number.isInteger(source.http_status) && source.http_status !== 200;
    const matches = ref && ref === url && source.verification?.status === 'social_unverified';
    const tier = !ref || !url || failed ? 'unavailable' : matches ? 'traveler_tested' : 'search_lead';
    return { entry_trust: tier, author_hint: text(provenance.author_hint) || '旅行者分享',
      source_ref: ref, citation_url: tier === 'traveler_tested' && directReceipt(source) ? ref : '',
      verification_status: 'social_unverified',
      notice: `${tier === 'traveler_tested' ? '旅行者实测' : '旅行者来源记录'} · 未核实，仅供经验参考，不代表官方确认` };
  }
  function projectSource(source, failedURLs) {
    const traveler = projectTraveler(source, failedURLs);
    const displayURL = traveler ? traveler.source_ref : safeURL(source.url || source.source_url || source.source_provenance?.source_ref);
    const direct = displayURL && source.status === 'read' && source.http_status === 200
      && source.method === 'direct_http' && source.verification !== 'search_grounded_not_direct_page';
    const unavailable = failedURLs.has(urlKey(displayURL)) || ['blocked', 'unavailable'].includes(source.status)
      || Number.isInteger(source.http_status) && source.http_status !== 200;
    const unverified = ['social_unverified', 'user_provided_unverified'].includes(source.verification?.status);
    const trust = traveler ? traveler.entry_trust : unavailable ? 'unavailable'
      : unverified ? 'search_lead' : direct ? source.entry_trust === 'official_verified' ? 'official_verified' : 'public_page' : 'search_lead';
    const labels = { official_verified: '已验证官方入口', public_page: '公开页面 · 官方身份未核验',
      search_lead: '搜索线索 · 未验证可用性', unavailable: '链接不可用', traveler_tested: '旅行者实测 · 未核实' };
    return { id: text(source.id), title: text(source.title), url: direct && !unavailable && !traveler && !unverified ? displayURL : '', display_url: displayURL,
      excerpt: text(source.excerpt), checked_at: text(source.checked_at), status: text(source.status),
      method: text(source.method), verification: text(source.verification), reason: text(source.reason),
      http_status: Number.isInteger(source.http_status) ? source.http_status : null,
      entry_trust: trust, entry_label: labels[trust],
      verification_status: traveler?.verification_status || text(source.verification?.status),
      traveler,
      search_lead: source.search_lead && typeof source.search_lead === 'object'
        ? { excerpt: text(source.search_lead.excerpt), checked_at: text(source.search_lead.checked_at) } : null };
  }
  function projectAction(action, sources, unavailable = new Set()) {
    const status = Object.hasOwn(statusLabels, action.status) ? action.status : 'needs_research';
    const id = text(action.id); const owner = action.owner === 'user' ? 'user' : 'agent';
    const travelerEvidence = sources.some(source => source.traveler && strings(action.source_ids).includes(source.id));
    const actionURL = travelerEvidence ? safeTravelerURL(action.action_url) : safeURL(action.action_url);
    const evidence = sources.find(source => strings(action.source_ids).includes(source.id) && source.display_url === actionURL);
    return { id, title: text(action.title) || '具体事项仍待补充', category: text(action.category), owner, status,
      statusLabel: statusLabels[action.status] || '状态待核实',
      day: Number.isInteger(action.day) && action.day > 0 ? action.day : null,
      related_item_ids: strings(action.related_item_ids), why: text(action.why),
      instructions: strings(action.instructions), when: text(action.when), completion_condition: text(action.completion_condition),
      source_ids: strings(action.source_ids), action_url: evidence?.url || '', action_url_text: actionURL,
      entry_trust: evidence?.entry_trust || 'search_lead', entry_label: evidence?.entry_label || '搜索线索 · 未验证可用性',
      materials: strings(action.materials), details: projectDetails(action.details, unavailable),
      fallback: text(action.fallback), assumptions: strings(action.assumptions), report_note: text(action.report_note),
      canReport: Boolean(id && owner === 'user' && ['ready', 'needs_user'].includes(status)),
      canReopen: Boolean(id && owner === 'user' && status === 'user_reported') };
  }
  function unavailableSourceURLs(sources, previous = new Set()) {
    const unavailable = new Set([...previous].map(urlKey).filter(Boolean));
    const groups = [];
    rows(sources).forEach(source => {
      const aliases = [source.url, source.source_url, source.source_provenance?.source_ref,
        ...(source.method === 'direct_http' ? [source.requested_url, ...strings(source.redirect_chain)] : [])].filter(Boolean);
      const keys = aliases.map(urlKey).filter(Boolean); groups.push(keys);
      if (['blocked', 'unavailable'].includes(source.status)
        || Number.isInteger(source.http_status) && source.http_status !== 200 || aliases.some(url => !safeURL(url))) {
        keys.forEach(key => unavailable.add(key));
      }
    });
    let changed = true;
    while (changed) {
      changed = false;
      groups.forEach(keys => {
        if (keys.some(key => unavailable.has(key))) keys.forEach(key => {
          if (!unavailable.has(key)) { unavailable.add(key); changed = true; }
        });
      });
    }
    return unavailable;
  }
  function planSources(plan) {
    return [...rows(plan?.preparation_pack?.sources), ...rows(plan?.source_evidence), ...rows(plan?.supplies), ...rows(plan?.routes),
      ...rows(plan?.routes).flatMap(route => rows(route.legs)),
      ...rows(plan?.supplies).map(item => item.geo_source).filter(item => item && typeof item === 'object')];
  }
  function projectPack(value, unavailable = unavailableSourceURLs(value?.sources)) {
    if (!value || value.version !== 1 || !Array.isArray(value.actions)) return null;
    const originals = rows(value.sources);
    const sources = originals.map(source => projectSource(source, unavailable));
    return { version: 1, status: value.status === 'ready' ? 'ready' : 'partial', summary: text(value.summary),
      actions: rows(value.actions).map(action => projectAction(action, sources, unavailable)), boundary: text(value.boundary), sources };
  }
  function legacyRemainder(plan, pack = projectPack(plan?.preparation_pack)) {
    const covered = item => pack?.actions.some(action => (text(item.id) && action.id === text(item.id)) || action.title === text(item.task || item.choice));
    return [...rows(plan?.preparation).map((item, index) => ({ ...item, id: text(item.id) || `legacy-preparation-${index}` })).filter(item => !covered(item)),
      ...rows(plan?.decisions).map((item, index) => ({ ...item, id: text(item.id) || `legacy-decision-${index}` }))
        .filter(item => item.status === 'needs_check' && !covered(item)).map(item => ({ ...item, task: item.choice, why: item.reason }))];
  }
  function node(tag, className, value) {
    const element = document.createElement(tag); if (className) element.className = className;
    if (value !== undefined) element.textContent = text(value); return element;
  }
  function note(parent, title, value) {
    if (text(value)) parent.append(node('p', 'preparation-note', title ? `${title}：${text(value)}` : value));
  }
  function link(title, url) {
    const anchor = node('a', 'preparation-link', title); anchor.href = url;
    anchor.target = '_blank'; anchor.rel = 'noopener noreferrer'; anchor.referrerPolicy = 'no-referrer'; return anchor;
  }
  function list(parent, values, ordered = false) {
    if (!values.length) return;
    const element = node(ordered ? 'ol' : 'ul', 'preparation-steps');
    values.forEach(value => element.append(node('li', '', value))); parent.append(element);
  }
  function actionButton(title, key, callback, options) {
    const button = node('button', 'handbook-download', title); button.type = 'button';
    button.dataset.preparationKey = key; button.disabled = Boolean(options.busy || options.running);
    button.addEventListener('click', async () => {
      if (button.disabled) return;
      button.disabled = true;
      try {
        const accepted = await callback();
        if (accepted === false) {
          const progress = document.getElementById('preparation-progress');
          if (progress) { progress.hidden = false; progress.textContent = '提交未确认。已有内容仍保留，请查看页面顶部的错误提示并核对最新状态；不会自动重复提交。'; }
        }
      }
      finally { button.disabled = Boolean(options.busy || options.running); }
    }); return button;
  }
  function renderEvidence(action, pack) {
    const detail = node('wa-details', 'preparation-evidence'); detail.dataset.preparationDetail = `evidence-${action.id}`;
    detail.append(node('summary', '', '依据、假设与适用范围'));
    note(detail, '为什么需要', action.why); list(detail, action.assumptions.map(item => `待确认假设：${item}`));
    const sources = pack.sources.filter(source => action.source_ids.includes(source.id));
    if (!sources.length) note(detail, '', '未附可追溯的来源记录；不要将这些说明视为渠道确认。');
    sources.forEach(source => {
      const row = node('div', 'preparation-source');
      row.append(source.url ? link(source.title || '查看来源', source.url) : node('p', '', source.title || '来源链接不可用'));
      note(row, '', source.entry_label + (source.http_status ? `（HTTP ${source.http_status}）` : ''));
      if (source.traveler) {
        note(row, '来源署名', source.traveler.author_hint); note(row, '', source.traveler.notice);
        if (source.traveler.citation_url) row.append(link('查看原帖 ↗', source.traveler.citation_url));
      }
      if (!source.url && !source.traveler?.citation_url) note(row, '来源地址（不可点击）', source.display_url);
      note(row, '来源摘录', source.excerpt); note(row, '查阅时间', source.checked_at || '未提供');
      if (source.search_lead) note(row, '搜索线索（未直接读取）', source.search_lead.excerpt);
      if (source.method === 'google_search_grounded') note(row, '', '这是搜索引擎基于来源生成的摘要，不是本系统直接读取的官网正文。');
      note(row, '', '查阅页面不代表当前库存、订单或办理结果已确认。'); detail.append(row);
    }); return detail;
  }
  function renderAction(action, pack, options) {
    const row = node('article', 'preparation-action'); row.dataset.actionId = action.id;
    row.setAttribute('data-action-status', action.status);
    const heading = node('div', 'preparation-action-heading');
    heading.append(node('h3', '', action.title));
    if (action.when) heading.append(node('span', 'preparation-when', action.when)); row.append(heading);
    const preview = action.instructions[0] || action.why || '具体操作仍待补齐，展开查看当前资料。';
    row.append(node('p', 'preparation-preview', preview.length > 84 ? `${preview.slice(0, 84)}…` : preview));
    renderDetailSummary(row, action.details);
    const controls = node('div', 'preparation-controls');
    if (action.action_url) controls.append(link(`${action.entry_label} ↗`, action.action_url));
    if (action.canReport) controls.append(actionButton('我已办好', `report-${action.id}`, () => options.onReport?.(action.id, 'user_reported'), options));
    if (action.canReopen) controls.append(actionButton('恢复为待办', `reopen-${action.id}`, () => options.onReport?.(action.id, 'reopen'), options));
    if (action.status === 'needs_research' && action.id) controls.append(actionButton('继续查这一项', `research-${action.id}`, () => options.onPrepare?.(action), options));
    if (controls.children.length) row.append(controls);
    const operation = drawer('完整步骤与材料', `operation-${action.id}`, 'preparation-operation');
    note(operation, '', action.statusLabel);
    if (!action.action_url && action.action_url_text) note(operation, action.entry_label, action.action_url_text);
    note(operation, '', [action.day ? `第 ${action.day} 天` : '', action.owner === 'user' ? '由你办理或准备' : '由旅行助手整理资料'].filter(Boolean).join(' · '));
    note(operation, '何时', action.when || '时间仍需补齐'); note(operation, '', action.report_note);
    list(operation, action.instructions, true);
    renderDetails(operation, action.details);
    if (!action.instructions.length) note(operation, '', '具体步骤尚未查清，不能视为可直接办理。');
    note(operation, '要带／要准备', action.materials.join('、'));
    note(operation, '完成标志', action.completion_condition || '完成条件仍需补齐');
    note(operation, '不顺利时', action.fallback);
    operation.append(renderEvidence(action, pack)); row.append(operation); return row;
  }
  function drawer(title, key, className = 'preparation-more') {
    const detail = node('wa-details', className); detail.dataset.preparationDetail = key;
    detail.append(node('summary', '', title)); return detail;
  }
  function renderLegacy(item, compact = false, index = 0) {
    const row = node('article', 'preparation-action'); row.dataset.actionId = text(item.id) || `legacy-preparation-${index}`;
    const heading = node('div', 'preparation-action-heading'); heading.append(node('h3', '', item.task || '原方案事项'));
    if (compact) heading.append(node('span', 'preparation-when', '原方案待办')); row.append(heading);
    const body = compact ? drawer('完整步骤与材料', `legacy-operation-${index}`, 'preparation-operation') : row;
    if (compact) {
      const preview = strings(item.steps)[0] || text(item.why) || '展开查看原方案的具体准备说明。';
      row.append(node('p', 'preparation-preview', preview.length > 84 ? `${preview.slice(0, 84)}…` : preview));
    }
    renderDetailSummary(row, item.details);
    const labels = { needs_check: '仍待核实', user_action: '仍需本人操作', provided: '本人已提供 · 非渠道确认', proposed: '原方案建议', not_applicable: '原方案标注不适用' };
    note(body, '原状态', labels[item.status] || '尚待落实'); note(body, '', item.why);
    note(body, '何时', item.when); list(body, strings(item.steps), true); note(body, '材料', strings(item.materials).join('、'));
    renderDetails(body, item.details || []);
    note(body, '原方案入口（未验证，不可点击）', safeURL(item.action_url));
    note(body, '原方案资料线索（未验证，不可点击）', safeURL(item.source_url));
    if (compact) row.append(body);
    return row;
  }
  function renderResearch(pack, legacy, options) {
    const research = pack.actions.filter(action => action.status === 'needs_research');
    const count = research.length + legacy.length;
    if (!count && pack.status !== 'partial') return null;
    const detail = drawer(count ? `还有${count}项影响出行的信息待查清` : '准备信息仍不完整，展开查看', 'research', 'preparation-research');
    note(detail, '', '这些缺口尚未解决。需要时点“继续查这一项”发起研究，不会自动在后台跟进。');
    research.forEach(action => detail.append(renderAction(action, pack, options)));
    legacy.forEach(item => detail.append(renderLegacy(item)));
    if (legacy.length || !count) detail.append(actionButton('帮我继续查漏补缺', 'research-legacy', () => options.onPrepare?.(), options));
    return detail;
  }
  function renderPack(section, plan, pack, options) {
    const mine = pack.actions.filter(action => action.owner === 'user' && ['ready', 'needs_user'].includes(action.status));
    const primary = mine.slice(0, 2); const deferred = mine.slice(2);
    const reported = pack.actions.filter(action => action.status === 'user_reported');
    const other = pack.actions.filter(action => !mine.includes(action) && !reported.includes(action) && action.status !== 'needs_research');
    const legacy = legacyRemainder(plan, pack);
    const research = renderResearch(pack, [], options); if (research) section.append(research);
    const personal = node('div', 'preparation-personal');
    primary.forEach(action => personal.append(renderAction(action, pack, options)));
    section.append(personal);
    if (!mine.length) note(section, '', '当前没有列出待你操作的重点事项，未查清的信息仍需落实。');
    if (mine.length) section.append(node('p', 'preparation-report-note', '“我已办好”仅记录本人报告，未经渠道确认。打开链接不会标记完成。'));
    if (reported.length) {
      const done = drawer(`你报告已办好 · ${reported.length} 项（未经渠道确认）`, 'reported');
      reported.forEach(action => done.append(renderAction(action, pack, options))); section.append(done);
    }
    const more = drawer('其他准备、原方案说明与使用边界', 'other');
    note(more, '', pack.summary); note(more, '', '重点准备不代表全部事项已齐；原方案未覆盖的说明保留原状态。');
    deferred.forEach(action => more.append(renderAction(action, pack, options)));
    other.forEach(action => more.append(renderAction(action, pack, options)));
    legacy.forEach(item => more.append(renderLegacy(item)));
    note(more, '', pack.boundary); note(more, '', '这里只更新准备资料。要改酒店、路线或日期，请调整行程。');
    more.append(actionButton('再帮我查漏补缺', 'prepare', () => options.onPrepare?.(), options)); section.append(more);
  }
  function updateControls(container, options = {}) {
    if (!container) return;
    container.querySelectorAll('button').forEach(button => { button.disabled = Boolean(options.busy || options.running); });
    const progress = document.getElementById('preparation-progress');
    if (progress) {
      progress.textContent = options.pending ? '正在提交准备请求，原方案保持可读…'
        : options.running ? '任务正在处理，已有准备内容仍可阅读；不会自动替你预订或付款。' : '';
      progress.hidden = !progress.textContent;
    }
  }
  function render(container, plan, options = {}) {
    if (!container) return;
    const unavailable = unavailableSourceURLs(planSources(plan), options.unavailableSources);
    plan = { ...plan, preparation: rows(plan?.preparation).map(item => ({ ...item, details: projectDetails(item.details, unavailable) })) };
    const pack = projectPack(plan?.preparation_pack, unavailableSourceURLs(planSources(plan), options.unavailableSources));
    const focusKey = options.focusKey || document.activeElement?.dataset?.preparationKey;
    const replacementKey = focusKey?.startsWith('report-') ? focusKey.replace(/^report-/, 'reopen-')
      : focusKey?.startsWith('reopen-') ? focusKey.replace(/^reopen-/, 'report-') : focusKey;
    const expanded = new Set(options.expanded || [...container.querySelectorAll('details[open]')].map(item => item.dataset.preparationDetail));
    const section = node('div', 'preparation-pack'); section.id = 'preparation-pack';
    const header = node('div', 'preparation-heading');
    const title = node('h2', '', '出发前，你需要做的'); title.id = 'handbook-prepare-title';
    header.append(title); section.append(header);
    if (!pack) {
      note(section, '', '按现有行程查漏补缺，整理办理步骤、所需材料和未查清事项。不会重新生成日程。');
      section.append(actionButton('帮我查漏补缺', 'prepare', () => options.onPrepare?.(), options));
      const personal = node('div', 'preparation-personal');
      legacyRemainder(plan).filter(item => item.status === 'user_action').forEach((item, index) => personal.append(renderLegacy(item, true, index)));
      section.append(personal);
      // Preserve older controls and notes inside one optional appendix.
      const oldTitle = document.getElementById('handbook-prepare-title'); if (oldTitle) oldTitle.id = 'handbook-legacy-prepare-title';
      const unresolved = legacyRemainder(plan).filter(item => !['user_action', 'provided', 'proposed', 'not_applicable'].includes(item.status)).length;
      const legacy = drawer(unresolved ? `还有${unresolved}项影响出行的信息待查清` : '查看原方案准备说明', 'legacy', unresolved ? 'preparation-research' : 'preparation-more');
      [...container.children].forEach(child => legacy.append(child)); section.append(legacy); container.replaceChildren(section);
    } else {
      renderPack(section, plan, pack, options);
      container.replaceChildren(section);
    }
    const progress = node('p', 'preparation-progress'); progress.id = 'preparation-progress'; progress.setAttribute('role', 'status');
    progress.setAttribute('aria-live', 'polite'); section.prepend(progress);
    // Restore expanded evidence and keyboard position when a saved report returns.
    const restore = (element, ancestors = []) => {
      if (expanded.has(element.dataset.preparationDetail)) element.open = true;
      if (focusKey && [focusKey, replacementKey].includes(element.dataset.preparationKey)) {
        ancestors.forEach(parent => { if (['details', 'wa-details'].includes(parent.tagName.toLowerCase())) parent.open = true; });
        element.focus?.({ preventScroll: true });
      }
      [...element.children].forEach(child => restore(child, [...ancestors, element]));
    };
    restore(section); updateControls(section, options);
  }
  function renderRevisionProgress(container, progress, context = {}) {
    if (!container) return;
    container.replaceChildren(); container.hidden = !progress?.total;
    if (!progress?.total) return;
    const accepted = Number(progress.accepted) || 0, total = Number(progress.total) || 0;
    const stage = context.stage || {};
    const activeDays = new Set(Array.isArray(stage.day_numbers) ? stage.day_numbers : []);
    // 进度卡置顶：n/total、当前项、当前项已用时三要素。
    const card = node('section', 'revision-progress-card');
    const headline = node('p', 'revision-progress-headline');
    headline.append(node('strong', '', `已保存 ${accepted} / ${total} 份调整草稿`),
      node('span', 'revision-progress-note', '完整核对通过前，原方案保留。'));
    const bar = node('wa-progress-bar', 'revision-progress-bar');
    bar.setAttribute('value', String(Math.round((accepted / total) * 100)));
    bar.setAttribute('aria-label', `已保存 ${accepted} / ${total} 份调整草稿`);
    card.append(headline, bar);
    const stageAt = Date.parse(stage.at || '');
    const stageSeconds = stage.status === 'started' && Number.isFinite(stageAt)
      ? Math.max(0, Math.floor((Date.now() - stageAt) / 1000)) : null;
    const meta = [context.stageLabel, context.restored,
      stageSeconds !== null ? `本项已用时 ${Math.floor(stageSeconds / 60)} 分 ${stageSeconds % 60} 秒` : '',
      context.elapsed].filter(Boolean);
    if (meta.length) card.append(node('p', 'revision-progress-meta', meta.join(' · ')));
    container.append(card);

    const domains = { preparation: '出发准备', preparation_pack: '办理事项', decisions: '全程选择', supplies: '地点资料', routes: '交通路线' };
    const titleOf = fragment => {
      const match = /^\/plan\/days\/(\d+)$/.exec(fragment.root);
      return match ? `第 ${Number(match[1]) + 1} 天` : domains[text(fragment.root).split('/')[2]] || '行程调整';
    };
    const kindOf = fragment => {
      if (fragment.status !== 'pending') return fragment.status;
      const match = /^\/plan\/days\/(\d+)$/.exec(fragment.root);
      return match && activeDays.has(Number(match[1]) + 1) ? 'active' : 'queued';
    };
    const statusText = { accepted: '草稿已保存 · 尚未发布', failed: '本次未完成，可继续', active: '正在处理' };
    const queued = [];
    const rowDrawer = (key, className = 'revision-row') => {
      const detail = node('wa-details', className); detail.dataset.preparationDetail = key;
      detail.append(node('summary', '', '')); return detail;
    };
    const summaryOf = detail => [...detail.children].find(child => child.tagName.toLowerCase() === 'summary');
    rows(progress.fragments).forEach(fragment => {
      const kind = kindOf(fragment);
      if (kind === 'queued') { queued.push(fragment); return; }
      const detail = rowDrawer(`revision-${text(fragment.id)}`);
      const summary = summaryOf(detail);
      const icon = node('span', `revision-row-icon is-${kind}`, kind === 'accepted' ? '✓' : kind === 'failed' ? '!' : '');
      icon.setAttribute('aria-hidden', 'true');
      summary.append(icon, node('span', 'revision-row-title', `${titleOf(fragment)} · ${statusText[kind] || '处理中'}`));
      list(detail, strings(fragment.preview)); container.append(detail);
    });
    if (queued.length) {
      const rest = rowDrawer('revision-queued', 'revision-row is-queued');
      const icon = node('span', 'revision-row-icon is-queued-dot', '');
      icon.setAttribute('aria-hidden', 'true');
      const summary = summaryOf(rest);
      summary.append(icon, node('span', 'revision-row-title', `还有 ${queued.length} 项排队`));
      list(rest, queued.map(titleOf)); container.append(rest);
    }
  }
  return { safeURL, sourceURL, validatedSourceURLs, urlKey, planSources, projectPack, projectTraveler, unavailableSourceURLs, legacyRemainder,
    projectDetails, detailLines, renderDetails, renderDetailSummary, renderRevisionProgress, render, updateControls };
});
