'use strict';
// Server assessments belong to individual assertions, never the whole trip.
(function (root) {
  const rows = value => Array.isArray(value) ? value : [];
  const labels = { verified: '✓ 已核验', conditional: '⚠ 有条件 / 估算', unknown: '❓ 未知' };
  const hasRestaurantQuery = data => rows(data?.claims).some(claim =>
    claim.conditions?.branch_id === 'bastadubai' || claim.entity_id === 'bastadubai');
  function currentTier(claim, now = Date.now()) {
    if (!['verified', 'conditional'].includes(claim.tier)) return 'unknown';
    const sources = rows(claim.sources);
    return sources.length && sources.every(source => source.state === 'active' &&
      Number.isFinite(Date.parse(source.valid_until)) && Date.parse(source.valid_until) > now)
      ? claim.tier : 'unknown';
  }
  function create({ container, getTrip, query, safeURL }) {
    if (!container) return { render() {} };
    let timer, generation = 0, currentTrip, activeRequest = null, queryError = '';
    let draft = {};
    const el = (tag, text, className) => {
      const node = document.createElement(tag); if (text !== undefined) node.textContent = String(text);
      if (className) node.className = className; return node;
    };
    function sourceLink(source) {
      const url = safeURL(source.source_ref);
      if (!url || source.state !== 'active') return el('span', '来源已失效或不可访问');
      const node = el('a', '查看查询来源'); node.href = url;
      node.target = '_blank'; node.rel = 'noopener noreferrer'; return node;
    }
    function claimCard(claim) {
      const c = claim.conditions || {}, tier = currentTier(claim);
      const card = el('article', undefined, `uncertainty-card uncertainty-${tier}`);
      const alternative = claim.source_field_ref === 'alternative' ? '备选时段 · ' : '';
      card.append(el('h4', `${labels[tier]} · ${alternative}${c.time || '时间待确认'}`));
      card.append(el('p', `${c.date || '日期待确认'} · ${c.party_size || '人数待确认'} 人 · ${c.timezone || '时区待确认'}`));
      card.append(el('p', tier === 'verified' ? (claim.value ? '查询时该条件有可订时段，尚未预约。' : '查询时未返回该条件的线上空位。')
        : tier !== claim.tier ? '查询已过期或来源已失效，需要重新核对当前空位。' : claim.reason || '尚无可用的核验结果。'));
      rows(claim.assumptions).forEach(value => card.append(el('p', `适用假设：${value}`)));
      rows(claim.sources).forEach(source => {
        card.append(el('p', `查询时间：${source.observed_at || '未知'} · 有效至：${source.valid_until || '未知'}`));
        card.append(sourceLink(source));
      });
      card.append(el('p', `下一步：${claim.next_step || '核对缺失条件'} · 处理人：${claim.owner === 'traveler' ? '你' : claim.owner || '待指定'}`));
      return card;
    }
    function render(trip) {
      clearTimeout(timer); const version = ++generation;
      if (currentTrip !== trip?.id) { activeRequest = null; draft = {}; queryError = ''; }
      currentTrip = trip?.id;
      const busy = activeRequest?.tripId === trip?.id;
      container.replaceChildren(); container.hidden = !trip?.uncertainty;
      if (container.hidden) return;
      const data = trip.uncertainty;
      container.append(el('h3', '安排依据与待确认事项'));
      if (rows(data.claims).length) container.append(el('p', '各项条件单独核对；查询结果不代表已经预约。'));
      if (data.audit?.automatic_verified === false) container.append(el('p', '核验者人工审计未达标，已暂停自动标记“已核验”，需要人工复核。', 'uncertainty-warning'));
      if (data.audit?.scheduler_error) container.append(el('p', '本周人工核对清单生成失败，已保留已有核验记录，需操作员处理。', 'uncertainty-warning'));
      const cards = el('div', undefined, 'uncertainty-cards');
      rows(data.claims).forEach(claim => cards.append(claimCard(claim)));
      container.append(cards);
      if (data.run?.reason) container.append(el('p', data.run.reason, 'uncertainty-warning'));
      const warnings = rows(data.blind_spots?.candidates).filter(row => row.warning_visible || row.disposition === 'needs_attention');
      warnings.forEach(row => container.append(el('p', `${row.disposition === 'checked' ? '已查原文，仍需注意' : '待查'}：${row.hypothesis}。${row.reason || ''} 影响：${row.impact || '待确认'}。${row.owner}：${row.next_action}`, 'uncertainty-warning')));
      const details = el('wa-details'); details.append(el('summary', '核查 BASTA! Dubai 的空位'));
      const form = el('form'), inputs = {};
      for (const [name, text, type] of [['local_date', '当地日期', 'date'], ['local_time', '当地时间', 'time'], ['party_size', '人数', 'number']]) {
        const label = el('label', text), input = el('input'); input.name = name; input.type = type;
        input.required = true; if (name === 'party_size') { input.min = 1; input.max = 20; }
        input.value = draft[name] || '';
        input.addEventListener('input', () => { draft[name] = input.value; });
        label.append(input); form.append(label); inputs[name] = input;
      }
      form.append(el('p', 'BASTA! · St. Regis Downtown Dubai · 时区 Asia/Dubai'));
      const status = el('p'); status.setAttribute('role', 'status');
      status.textContent = queryError;
      const button = el('button', busy ? '正在核查…' : '查询空位', 'secondary-button');
      button.type = 'submit'; button.disabled = busy || trip.status === 'running';
      form.append(button, status);
      form.addEventListener('submit', async event => {
        event.preventDefault(); if (activeRequest || getTrip()?.id !== trip.id) return;
        const body = { expected_revision: trip.revision, branch_id: 'bastadubai',
          local_date: inputs.local_date.value, local_time: inputs.local_time.value,
          timezone_name: 'Asia/Dubai', party_size: Number(inputs.party_size.value) };
        const request = { tripId: trip.id, revision: trip.revision };
        activeRequest = request; queryError = ''; button.disabled = true;
        status.textContent = '正在读取餐厅预约渠道，只查询空位。';
        try {
          const value = await query(body);
          if (activeRequest !== request || getTrip()?.id !== trip.id) return;
          activeRequest = null;
          if (getTrip()?.revision === request.revision) render({ ...getTrip(), uncertainty: value.uncertainty });
          else { queryError = '方案条件已变化，请按当前版本重新核查；这次结果没有覆盖新版本。'; render(getTrip()); }
        } catch (error) {
          if (activeRequest === request) queryError = error.message || '查询未完成；输入保留，未创建预约。';
        } finally {
          if (activeRequest === request) { activeRequest = null; if (getTrip()?.id === trip.id) render(getTrip()); }
        }
      });
      details.append(form); if (hasRestaurantQuery(data)) container.append(details);
      const expiries = rows(data.claims).flatMap(claim => rows(claim.sources)).map(source => Date.parse(source.valid_until)).filter(at => at > Date.now());
      if (expiries.length) timer = setTimeout(() => {
        if (getTrip()?.id === trip.id && generation === version) render(trip);
      }, Math.min(...expiries) - Date.now() + 5);
    }
    return { render };
  }
  const api = { create, currentTier, hasRestaurantQuery };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.TravelUncertainty = api;
})(typeof window === 'undefined' ? globalThis : window);
