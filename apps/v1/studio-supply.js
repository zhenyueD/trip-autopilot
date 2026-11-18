'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const records = new Map(), selections = new Map(), evidence = new Map();
  const pendingResearch = new Set();
  let state = null, cases = [], activeCaseId = '', epoch = 0, busy = '';
  let evidenceEpoch = 0, evidenceBusy = false, needsRefresh = false, candidateInputs = [];
  const values = value => Array.isArray(value) ? value : [];
  const node = (tag, cls, text) => {
    const element = document.createElement(tag); element.className = cls || '';
    if (text != null) element.textContent = String(text);
    return element;
  };
  const list = (target, items) => target.replaceChildren(...values(items).map(text => node('li', '', text)));
  const status = (text, error = false) => {
    $('supply-status').textContent = text; $('supply-status').dataset.kind = error ? 'error' : '';
  };
  const current = (ticket, caseId) => ticket === epoch && caseId === activeCaseId;
  const selected = () => selections.get(activeCaseId)?.ids || [];
  const running = () => pendingResearch.has(activeCaseId) || ['generating', 'researching'].includes(state?.status);
  const insufficient = data => data?.status === 'insufficient_evidence';
  const insufficientMessage = '证据不足：本轮没有取得合格原文，暂不生成候选或比较输入。';
  const claimStatus = value => ({ unknown: '未知，待确认', single_source: '单一来源，未交叉确认',
    uncorroborated: '未交叉确认', social_unverified: '社交线索，未核实',
    corroborated: '已交叉确认', conflict: '来源有冲突' }[value] || '未知，待确认');
  const claimType = value => ({ hard_fact: '外部事实', permission: '许可条件', transport: '交通',
    experience: '体验线索', background: '背景线索' }[value] || '依据');

  function controls() {
    const remaining = state?.budget?.remaining;
    const budgetAvailable = typeof remaining === 'number' && remaining >= 2;
    $('supply-case').disabled = !cases.length;
    $('supply-refresh').disabled = Boolean(busy);
    $('supply-generate').hidden = Boolean(state?.card);
    $('supply-generate').disabled = Boolean(busy) || !state || Boolean(state.card) || running() || insufficient(state) || needsRefresh || !budgetAvailable || !state.csrf_token;
    $('supply-export').disabled = Boolean(busy) || !state?.card || running() || needsRefresh || selected().length !== 2 || !state.csrf_token;
    $('supply-evidence-refresh').disabled = !state?.card || evidenceBusy;
    for (const input of candidateInputs) input.disabled = Boolean(busy) || running();
    $('supply-result').setAttribute('aria-busy', String(Boolean(busy)));
  }

  function renderPicker() {
    $('supply-case').replaceChildren(...cases.map(item => {
      const option = node('option', '', item.label || item.id); option.value = item.id; return option;
    }));
    if (activeCaseId && !cases.some(item => item.id === activeCaseId)) {
      const option = node('option', '', activeCaseId); option.value = activeCaseId; $('supply-case').append(option);
    }
    $('supply-case').value = activeCaseId;
    const url = new URL(location.href);
    if (activeCaseId) url.searchParams.set('case_id', activeCaseId);
    history.replaceState(null, '', url);
  }

  function renderSelection() {
    const ids = selected();
    const names = values(state?.card?.candidates).filter(item => ids.includes(item.id)).map(item => item.name);
    $('supply-selected').textContent = names.length ? `已选 ${names.length}/2：${names.join('、')}` : '还没有选定。挑两项，备好下一步的比较输入。';
    for (const input of candidateInputs) {
      input.checked = ids.includes(input.value);
      input.closest('.supply-candidate').dataset.selected = String(input.checked);
    }
    controls();
  }

  function sourceReference(ids) {
    if (!values(ids).length) return node('span', 'supply-claim-state', '未附来源');
    const link = node('a', 'supply-source-reference', `来源：${ids.join('、')}`);
    link.href = '#supply-evidence';
    link.addEventListener('click', () => { $('supply-evidence').open = true; loadEvidence(); });
    return link;
  }

  function renderClaims(candidate) {
    const details = node('details'), claims = values(candidate.claims);
    details.append(node('summary', '', `依据与核验状态（${claims.length}）`));
    const items = claims.map(claim => {
      const item = node('li');
      const risk = claim.status === 'corroborated' ? '' : claim.risk_level === 'S1' ? ' · S1 相关确定承诺待确认'
        : claim.risk_level === 'S2' ? ' · S2 可带条件比较' : '';
      item.append(node('p', 'supply-claim-text', claim.text),
        node('p', 'supply-claim-state', `${claimType(claim.claim_type)} · ${claimStatus(claim.status)}${risk}`));
      if (claim.uncertainty) item.append(node('p', 'supply-claim-uncertainty', claim.uncertainty));
      item.append(sourceReference(claim.source_ids)); return item;
    });
    const claimList = node('ul', 'supply-claims'); claimList.append(...items); details.append(claimList);
    return details;
  }

  function candidateConditions(candidate) {
    const section = node('div', 'supply-conditions');
    const uncertain = values(candidate.claims).filter(claim => claim.status !== 'corroborated');
    if (uncertain.some(claim => claim.risk_level === 'S1')) {
      section.append(node('p', 'supply-risk', '有重要条件待确认；相关确定承诺暂不能作出，其他取舍仍可比较。'));
    } else if (uncertain.length) {
      section.append(node('p', 'supply-risk', '有未交叉确认的信息，可带着这些条件继续比较。'));
    }
    const unknowns = values(candidate.unknowns);
    if (unknowns.length) {
      section.append(node('p', '', '这项仍需确认'));
      const items = node('ul'); list(items, unknowns); section.append(items);
    }
    section.hidden = !uncertain.length && !unknowns.length;
    return section;
  }

  function renderCandidate(candidate, index) {
    const section = node('article', 'comparison-option supply-candidate');
    const heading = node('div', 'comparison-option-heading'), title = node('div');
    title.append(node('p', 'comparison-option-number', String(index + 1).padStart(2, '0')), node('h3', '', candidate.name));
    heading.append(title);
    if (values(state.card.suggested_pair_ids).includes(candidate.id)) heading.append(node('span', 'supply-suggested', '建议先比较'));
    const tradeoff = node('p', 'supply-tradeoff');
    tradeoff.append(node('strong', '', '要接受的取舍：'), node('span', '', candidate.tradeoff));
    section.append(heading, node('p', 'supply-fit', candidate.fit_reason), tradeoff, candidateConditions(candidate), renderClaims(candidate));
    const label = node('label', 'supply-pick'), input = node('input');
    input.type = 'checkbox'; input.value = candidate.id;
    input.addEventListener('change', () => {
      const ids = selected();
      if (input.checked && ids.length >= 2) {
        input.checked = false; status('已经选了两项。先取消一项，就能换成这个选择。'); return;
      }
      selections.set(activeCaseId, { version: state.version, ids: input.checked ? [...ids, candidate.id] : ids.filter(id => id !== candidate.id) });
      $('supply-export-receipt').hidden = true; status(''); renderSelection();
    });
    label.append(input, node('span', '', `选择 ${candidate.name} 参与比较`));
    section.append(label); candidateInputs.push(input); return section;
  }

  function renderOffList() {
    const item = state?.card?.off_list_candidate;
    const candidate = values(state?.card?.candidates).find(entry => entry.id === item?.candidate_id);
    $('supply-off-list').hidden = !item || !candidate;
    $('supply-off-list').replaceChildren();
    if (!item || !candidate) return;
    $('supply-off-list').append(node('h3', '', `常见清单之外，也看看 ${candidate.name}`),
      node('p', '', item.reason), sourceReference(item.source_ids));
  }

  function resetEvidence() {
    evidenceEpoch += 1; evidenceBusy = false;
    $('supply-evidence').open = false; $('supply-sources').replaceChildren();
    $('supply-evidence-status').textContent = '';
    $('supply-evidence-refresh').textContent = '读取来源';
  }

  function render() {
    renderPicker(); candidateInputs = []; resetEvidence();
    const context = cases.find(item => item.id === activeCaseId);
    $('supply-brief').textContent = context?.decision_need || '';
    $('supply-title').textContent = state?.card?.title || '先找到，真正适合你的。';
    $('supply-framing').textContent = state?.card?.framing || '';
    $('supply-framing').hidden = !state?.card;
    const budget = state?.budget;
    $('supply-budget').textContent = typeof budget?.remaining === 'number'
      ? budget.remaining < 2 ? '剩余额度不足一次新研究。已有候选仍可查看和导出。' : `本轮剩余 ${budget.remaining} 次调用；每次研究预计用 2 次。读取和导出不调用模型。`
      : '读取研究额度后，才能发起新研究。';
    $('supply-result').hidden = !state?.card; $('supply-export-receipt').hidden = true;
    $('supply-candidates').replaceChildren();
    if (!state?.card) { controls(); return; }
    const candidates = values(state.card.candidates), saved = selections.get(activeCaseId);
    if (!saved || saved.version !== state.version) {
      const ids = [...new Set(values(state.card.suggested_pair_ids))].filter(id => candidates.some(item => item.id === id));
      selections.set(activeCaseId, { version: state.version, ids: ids.length === 2 ? ids : [] });
    }
    $('supply-pair-hint').textContent = values(state.card.suggested_pair_ids).length === 2 ? '已先勾选建议的两项，也可以换成你的选择。' : '选出两项，再细看取舍。';
    $('supply-candidates').append(...candidates.map(renderCandidate)); renderOffList();
    list($('supply-unknowns'), state.card.unknowns);
    $('supply-unknown-section').hidden = !values(state.card.unknowns).length;
    renderSelection();
  }

  async function request(path, options = {}) {
    const response = await fetch(path, { cache: 'no-store', credentials: 'same-origin', ...options });
    let data;
    try { data = await response.json(); } catch (_) { throw new Error('返回内容暂时无法读取'); }
    if (!response.ok) {
      const error = new Error(data.error?.message_zh || '本次请求没有完成'); error.status = response.status; throw error;
    }
    return data;
  }

  function accept(data, caseId) {
    if (!data || typeof data.case_id !== 'string' || (caseId && data.case_id !== caseId)
      || !Number.isInteger(data.version) || !Array.isArray(data.cases)
      || (data.card && !Array.isArray(data.card.candidates))) throw new Error('候选数据暂时无法读取');
    activeCaseId = data.case_id; state = data; cases = data.cases; records.set(activeCaseId, data); needsRefresh = false;
    render();
  }

  async function load(caseId) {
    const ticket = ++epoch;
    activeCaseId = caseId || ''; state = records.get(activeCaseId) || null;
    busy = 'load'; needsRefresh = false; render(); status('正在读取这次旅行的候选…');
    try {
      const data = await request(`/api/supply${caseId ? `?case_id=${encodeURIComponent(caseId)}` : ''}`);
      if (!current(ticket, caseId || '')) return;
      accept(data, caseId);
      status(insufficient(data) ? insufficientMessage : running() ? '候选仍在研究中。稍后手动读取已有结果，无需重复提交。'
        : data.last_error ? '上次研究没有完成；已有候选仍然保留，没有自动重试。' : '');
    } catch (error) {
      if (current(ticket, caseId || '')) {
        needsRefresh = true;
        status(state?.card ? '暂时没能读取最新结果。这次旅行的已有候选和选择已保留。' : `${error.message}。可手动重新读取。`, true);
      }
    } finally { if (ticket === epoch) { busy = ''; controls(); } }
  }

  async function generate() {
    if ($('supply-generate').disabled) return;
    const caseId = activeCaseId, ticket = ++epoch, token = state.csrf_token;
    pendingResearch.add(caseId); busy = 'generate'; controls();
    status('正在寻找值得比较的选择，并梳理依据与取舍。');
    try {
      const data = await request('/api/supply/generate', { method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Comparison-Token': token },
        body: JSON.stringify({ case_id: caseId, request_id: crypto.randomUUID() }) });
      if (!current(ticket, caseId)) return;
      accept(data, caseId);
      status(insufficient(data) ? insufficientMessage : ['generating', 'researching'].includes(data.status) ? '候选仍在研究中，稍后手动读取即可。'
        : data.card ? '候选已备好。选两项，继续看清取舍。' : '本次还没有候选结果，可手动读取已有结果。');
    } catch (error) {
      if (current(ticket, caseId)) { needsRefresh = true; status(`${error.message}。已有候选保留；请先读取已有结果，没有自动重试。`, true); }
    } finally {
      pendingResearch.delete(caseId);
      if (current(ticket, caseId)) busy = '';
      controls();
    }
  }

  function safeURL(value) {
    try {
      const url = new URL(value);
      return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
    } catch (_) { return null; }
  }

  function renderEvidence(data) {
    $('supply-sources').replaceChildren(...values(data.sources).map(source => {
      const item = node('li'), url = safeURL(source.url), title = `${source.id || '来源'} · ${source.title || '未命名来源'}`;
      const link = node(url ? 'a' : 'span', '', title);
      if (url) { link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; }
      item.append(link);
      const meta = [source.platform || source.source_type, source.status ? claimStatus(source.status) : '', !url ? '未提供可安全打开的来源链接' : ''].filter(Boolean);
      if (meta.length) item.append(node('span', 'supply-source-meta', meta.join(' · ')));
      return item;
    }));
    $('supply-evidence-status').textContent = values(data.sources).length
      ? '来源链接供回看；每条说法的核验状态与不确定性，保留在对应候选旁。'
      : '当前未附可打开的来源。相关说法仍以候选旁的证据状态为准。';
    $('supply-evidence-refresh').textContent = '重新读取来源';
  }

  async function loadEvidence(force = false) {
    if (!state?.card || evidenceBusy) return;
    const caseId = activeCaseId, version = state.version, key = `${caseId}:${version}`;
    if (!force && evidence.has(key)) { renderEvidence(evidence.get(key)); return; }
    const ticket = epoch, sourceTicket = ++evidenceEpoch; evidenceBusy = true; controls();
    $('supply-evidence-status').textContent = '正在读取这次旅行的来源…';
    try {
      const data = await request(`/api/supply/evidence?case_id=${encodeURIComponent(caseId)}`);
      if (!current(ticket, caseId) || sourceTicket !== evidenceEpoch || state.version !== version) return;
      if (!data || !Array.isArray(data.sources) || (data.case_id && data.case_id !== caseId)) throw new Error('来源数据暂时无法读取');
      evidence.set(key, data); renderEvidence(data);
    } catch (_) {
      if (current(ticket, caseId) && sourceTicket === evidenceEpoch) $('supply-evidence-status').textContent = '来源暂时没有取回。候选和已选项已保留，可手动重新读取来源。';
    } finally {
      if (sourceTicket === evidenceEpoch) { evidenceBusy = false; controls(); }
    }
  }

  function download(data, caseId) {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob), link = node('a');
    link.href = url; link.download = `comparison-input-${caseId.replace(/[^a-zA-Z0-9_-]/g, '_')}.json`;
    link.hidden = true; document.body.append(link);
    try { link.click(); } finally { link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
  }

  async function exportPair() {
    if ($('supply-export').disabled) return;
    const caseId = activeCaseId, ticket = ++epoch, pairIds = [...selected()], version = state.version;
    const token = state.csrf_token;
    busy = 'export'; controls(); status('正在备好这两项的比较输入…');
    try {
      const data = await request('/api/supply/export', { method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Comparison-Token': token },
        body: JSON.stringify({ case_id: caseId, pair_ids: pairIds, expected_version: version }) });
      if (!current(ticket, caseId)) return;
      if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('比较输入暂时无法读取');
      download(data, caseId);
      $('supply-export-receipt').textContent = '这两项的比较输入已备好，并已发起 JSON 文件下载。本轮未调用比较模型，比较卡尚未生成。';
      $('supply-export-receipt').hidden = false; status('');
    } catch (error) {
      if (current(ticket, caseId)) {
        if (error.status === 409) needsRefresh = true;
        status(`${error.message}。所选候选保留${error.status === 409 ? '，请读取最新结果后再导出' : '，可手动再次导出'}。`, true);
      }
    } finally { if (current(ticket, caseId)) { busy = ''; controls(); } }
  }

  $('supply-case').addEventListener('change', () => load($('supply-case').value));
  $('supply-refresh').addEventListener('click', () => load(activeCaseId));
  $('supply-generate').addEventListener('click', generate);
  $('supply-export').addEventListener('click', exportPair);
  $('supply-evidence').addEventListener('toggle', () => { if ($('supply-evidence').open) loadEvidence(); });
  $('supply-evidence-refresh').addEventListener('click', () => loadEvidence(true));
  load(new URL(location.href).searchParams.get('case_id') || '');
})();
