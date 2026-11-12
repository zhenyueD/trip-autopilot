'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const contract = window.SessionContract;
  const STORAGE_KEY = 'atom-travel-single-session-v1';
  const state = { session: null, trip: null, busy: false, timer: null, generation: 0, lastHandbook: '', lastReceipt: '', handbookMode: 'overview' };
  const node = (tag, className, text) => { const element = document.createElement(tag); if (className) element.className = className; if (text !== undefined) element.textContent = String(text); return element; };
  function error(message = '') { $('error').hidden = !message; $('error').textContent = message; }
  function validSession(value) { return value && /^[0-9a-f]{32}$/.test(value.id) && /^[A-Za-z0-9_-]{20,200}$/.test(value.token); }
  function saveSession() { try { if (state.session) localStorage.setItem(STORAGE_KEY, JSON.stringify(state.session)); else localStorage.removeItem(STORAGE_KEY); } catch { error('浏览器未能保存访问凭证。当前页面仍可使用，刷新前请保留原始手册地址。'); } }
  function loadSession() { try { const value = JSON.parse(localStorage.getItem(STORAGE_KEY)); if (validSession(value)) state.session = value; } catch { /* Invalid local state is ignored, never sent. */ } }
  async function api(path, method = 'GET', body, session = state.session) {
    const headers = { Accept: 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (session) headers.Authorization = `Bearer ${session.token}`;
    let response;
    try { response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), cache: 'no-store', credentials: 'same-origin' }); }
    catch { throw new Error('暂时连接不到 7392。此次请求结果未确认，请不要连续重复提交。'); }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { const issue = new Error(data.error?.message_zh || `服务未能处理请求（HTTP ${response.status}）。`); issue.status = response.status; throw issue; }
    return data;
  }
  const tripPath = () => `/api/agent/trips/${state.session.id}`;
  function setBusy(value) { state.busy = value; $('create-trip').disabled = value; $('revise-trip').disabled = value || state.trip?.status !== 'completed'; }
  function schedule(delay = 1500) { clearTimeout(state.timer); if (state.trip?.status === 'running') state.timer = setTimeout(() => syncTrip(), delay); }
  function showTrip() {
    const trip = state.trip; if (!trip) return;
    $('intake').hidden = true; $('new-trip').hidden = false; $('progress-section').hidden = false;
    const running = trip.status === 'running', failed = trip.status === 'failed';
    $('progress-title').textContent = running ? (trip.job?.kind === 'revise' ? '正在按你的要求修订' : '正在准备你的旅行手册') : failed ? (trip.handbook_url ? '本轮没有完成，已有手册保留' : '本轮没有完成，尚未生成手册') : '手册版本已保存';
    $('status-badge').textContent = running ? '进行中' : failed ? '需查看失败原因' : '已完成';
    $('progress-copy').textContent = failed ? (trip.error?.message || '服务未确认本轮完成；不会自动重试。') : running ? '以下数字来自 7392 当前会话记录，不是模拟进度。' : '已收到服务确认的文档版本，可以阅读或继续修订。';
    const counts = trip.progress?.tool_counts || {}, activity = (counts.search || 0) + (counts.read_page || 0) + (counts.record_evidence || 0) + (counts.save_handbook || 0) + (counts.edit_handbook || 0);
    $('progress-bar').style.width = running ? `${Math.min(92, 8 + activity * 4)}%` : failed ? '100%' : '100%';
    $('progress-bar').style.background = failed ? '#b25000' : '';
    const metrics = $('progress-metrics'); metrics.replaceChildren();
    contract.progressRows(trip).forEach(([label, value, unit]) => { const group = node('div'); group.append(node('dt', '', label), node('dd', '', `${value} ${unit}`)); metrics.append(group); });
    $('last-activity').textContent = trip.progress?.last_activity_at ? `最近真实活动：${new Date(trip.progress.last_activity_at).toLocaleString('zh-CN', { hour12: false })}` : '服务尚未返回活动时间。';
    renderDocument(trip); renderRevision(trip); setBusy(false);
  }
  function updateHandbookMode(mode) {
    state.handbookMode = mode === 'deep' ? 'deep' : 'overview';
    $('handbook-overview').setAttribute('aria-pressed', String(state.handbookMode === 'overview'));
    $('handbook-deep').setAttribute('aria-pressed', String(state.handbookMode === 'deep'));
  }
  function handbookFailure(message) {
    $('handbook-error').textContent = `${message} 可使用“打开原始手册”阅读。`;
    $('handbook-error').hidden = false;
    $('handbook-overview').disabled = true; $('handbook-deep').disabled = true;
  }
  function mountHandbook() {
    if (!state.lastHandbook) return;
    try {
      const presentation = window.HandbookPresentation;
      if (!presentation) throw new Error('两层手册呈现资源未加载。');
      const doc = $('handbook-frame').contentDocument;
      if (!doc) throw new Error('手册尚未加载。');
      if (!doc.__hpV1ModeListener) {
        doc.__hpV1ModeListener = event => updateHandbookMode(event.detail);
        doc.addEventListener('handbook-mode-change', doc.__hpV1ModeListener);
      }
      const stats = presentation.mount(doc, state.handbookMode);
      if (!stats.ok) throw new Error('原始 HTML 不含可识别的 handbook 结构。');
      presentation.bind(doc); updateHandbookMode(state.handbookMode);
      $('handbook-error').hidden = true;
      $('handbook-overview').disabled = false; $('handbook-deep').disabled = false;
    } catch (issue) { handbookFailure(issue.message); }
  }
  function setHandbookMode(mode) {
    try {
      const doc = $('handbook-frame').contentDocument;
      if (!doc || !window.HandbookPresentation) throw new Error('手册尚未加载。');
      if (!doc.querySelector('[data-hp-overview]')) mountHandbook();
      else window.HandbookPresentation.setMode(doc, mode);
      updateHandbookMode(mode);
    } catch (issue) { handbookFailure(issue.message); }
  }
  function renderDocument(trip) {
    const handbook = contract.safeDocumentURL(trip.handbook_url), pdf = contract.safeDocumentURL(trip.pdf_url);
    $('handbook-section').hidden = !handbook;
    if (handbook) {
      if (state.lastHandbook !== handbook) {
        updateHandbookMode('overview');
        $('handbook-overview').disabled = true; $('handbook-deep').disabled = true;
        $('handbook-frame').src = handbook; state.lastHandbook = handbook;
      }
      $('open-handbook').href = handbook; $('document-meta').textContent = `文档版本 ${trip.document?.version ?? trip.revision ?? '未提供'} · 内容由 7392 原始 HTML 直接呈现`;
    }
    $('open-pdf').hidden = !pdf; if (pdf) $('open-pdf').href = pdf;
    renderReceipt(trip);
  }
  function renderReceipt(trip) {
    const receipt = trip.receipt;
    $('receipt-section').hidden = !receipt;
    if (!receipt) return;
    const signature = JSON.stringify([trip.id, trip.document?.version, trip.document?.hash, receipt, trip.diff]);
    if (signature === state.lastReceipt) return; state.lastReceipt = signature;
    $('receipt-version').textContent = `版本 ${trip.document?.version ?? trip.revision ?? '未提供'}`;
    const content = $('receipt-content'); content.replaceChildren();
    contract.receiptSections(receipt).forEach(section => { const card = node('article', 'receipt-block'); card.append(node('h3', '', section.title), node('p', '', contract.displayValue(section.value))); content.append(card); });
    $('diff-details').hidden = !trip.diff; $('diff-content').textContent = trip.diff || '';
    const actions = $('receipt-actions'); actions.replaceChildren();
    const next = typeof receipt.next_action === 'string' ? receipt.next_action.trim() : '';
    if (next) {
      const button = node('button', 'secondary-button', `一键行动：${next}`); button.type = 'button';
      button.addEventListener('click', () => {
        if (/修订|调整|编辑|继续/.test(next)) { $('revision-input').focus(); $('revision-section').scrollIntoView({ behavior: 'smooth', block: 'start' }); }
        else { $('handbook-section').scrollIntoView({ behavior: 'smooth', block: 'start' }); $('handbook-frame').focus(); }
      });
      actions.append(button, node('p', 'form-note', '只执行回执中的导航或编辑动作；不会自动预订、付款或再次调用修订。'));
    }
  }
  function renderRevision(trip) { $('revision-section').hidden = !trip.handbook_url; $('revise-trip').disabled = state.busy || trip.status !== 'completed'; }
  async function syncTrip() {
    if (!state.session) return; const generation = state.generation;
    try { const result = await api(tripPath()); if (generation !== state.generation) return; if (!result.trip) throw new Error('服务未返回任务状态。'); state.trip = result.trip; error(); showTrip(); schedule(); }
    catch (issue) { if (generation !== state.generation) return; error(`${issue.message} 已显示的手册保持不变。`); schedule(4000); }
  }
  async function createTrip(event) {
    event.preventDefault(); if (state.busy) return; let payload;
    try { payload = contract.buildIntake(Object.fromEntries(new FormData($('intake-form')).entries())); }
    catch (issue) { error(issue.message); return; }
    setBusy(true); error('正在提交一次真实生成请求，请等待服务接受…');
    try {
      const result = await api('/api/agent/trips', 'POST', payload, null);
      if (!result.trip?.id || !validSession({ id: result.trip.id, token: result.token })) throw new Error('服务未返回完整任务凭证。');
      state.generation += 1; state.session = { id: result.trip.id, token: result.token }; state.trip = result.trip; state.lastHandbook = ''; state.lastReceipt = ''; saveSession(); error(); showTrip(); schedule();
    } catch (issue) { error(`${issue.message} 输入仍保留；提交结果不明确时请勿重复点击。`); }
    finally { setBusy(false); }
  }
  async function reviseTrip(event) {
    event.preventDefault(); if (state.busy || !state.trip || state.trip.status !== 'completed') return;
    let payload; const requestId = crypto.randomUUID();
    try { payload = contract.buildRevision(state.trip, $('revision-input').value, requestId); }
    catch (issue) { error(issue.message); return; }
    setBusy(true); error();
    try {
      const result = await api(`${tripPath()}/message`, 'POST', payload);
      if (result.kind !== 'revision' || !result.trip) throw new Error('服务未按修订接受这次请求；请重新读取状态。');
      state.generation += 1; state.trip = result.trip; $('revision-input').value = ''; showTrip(); schedule();
    } catch (issue) { error(`${issue.message} 修订文字仍保留，不会自动重复提交。`); await syncTrip(); }
    finally { setBusy(false); }
  }
  async function capabilities() {
    try { const value = await api('/api/agent/capabilities', 'GET', undefined, null); $('connection').textContent = value.planner_available ? '7392 · 实时规划可用' : '7392 · 规划暂不可用'; $('create-trip').disabled = !value.planner_available; }
    catch (issue) { $('connection').textContent = '7392 · 尚未连接'; $('create-trip').disabled = true; error(issue.message); }
  }
  $('intake-form').addEventListener('submit', createTrip); $('revision-form').addEventListener('submit', reviseTrip);
  $('handbook-frame').addEventListener('load', mountHandbook);
  $('handbook-overview').addEventListener('click', () => setHandbookMode('overview'));
  $('handbook-deep').addEventListener('click', () => setHandbookMode('deep'));
  $('new-trip').addEventListener('click', () => { if (state.busy) return; clearTimeout(state.timer); state.generation += 1; state.session = null; state.trip = null; state.lastHandbook = ''; state.lastReceipt = ''; updateHandbookMode('overview'); saveSession(); ['progress-section', 'receipt-section', 'handbook-section', 'revision-section'].forEach(id => { $(id).hidden = true; }); $('handbook-frame').src = 'about:blank'; $('intake').hidden = false; $('new-trip').hidden = true; error(); $('destination').focus(); capabilities(); });
  loadSession(); capabilities(); if (state.session) syncTrip();
})();
