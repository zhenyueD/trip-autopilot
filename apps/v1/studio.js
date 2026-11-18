'use strict';

(() => {
  const $ = id => document.getElementById(id);
  if (new URL(location.href).searchParams.get('view') === 'comparison') {
    window.TravelSessionComparison.mount({ container: $('comparison-view') });
    return;
  }
  const SESSION_KEY = 'atom-travel-studio-session-v1';
  const LIBRARY_KEY = 'atom-travel-studio-library-v2';
  const CREATE_PROOF_KEY = 'atom-travel-studio-create-proof-v1';
  const DEMO_FEEDBACK = '加一个6岁孩子，每天中午回酒店休息2小时，不自驾。';
  const DEMO_BRIEF = '我们是2个成年人，从杭州出发，2026年10月2日至5日在阿布扎比玩4天。当地预算12000元人民币，不含国际机票。喜欢建筑、光影和当地饮食，不想每天赶景点。请安排具体体验、每天的路线、住宿餐饮和行前准备。';
  const state = { trip: null, session: null, cap: null, busy: false,
    library: { sessions: [], activeId: null, forgottenIds: [] }, exampleDraft: false, pendingCreate: null,
    storageFailed: false, pollTimer: null, pollFailures: 0, generation: 0, mutationNotice: '',
    renderedPlan: '', renderedPreview: '', day: 0, mapsReady: false, mapsConfigured: false, preparing: false, messageContext: '', messageLog: [] };
  let maincore = null;
  const revisionStage = window.TravelRevisionProgress?.create({ container: $('revision-stage') });
  const revisionElapsed = window.TravelRevisionProgress?.createElapsed?.();
  const statuses = { running: '正在把想法安排成一趟旅行', ready: '你的旅行提案已准备好',
    needs_attention: '提案还有待确认的安排', error: '这次旅行提案没有完成', interrupted: '安排被中断了，可以继续',
    completed: '旅行手册与导出文件已准备好', partial: '手册已保存，仍有交付缺口', failed: '本轮已停止，已保存的内容保留' };
  const category = { flight: '航班', lodging: '住宿', transport: '交通', dining: '餐饮',
    activities: '体验', packing: '行前', other: '其他' };
  const labels = { proposed: '方案建议', provided: '用户已提供', needs_check: '待核验',
    user_action: '需要你行动', not_applicable: '本次不适用' };
  function singleSessionBaseCopy(trip) {
    const generating = trip.generation_status && trip.generation_status !== 'completed'
      && trip.generation_status !== trip.status;
    if (generating) return `生成状态：${trip.generation_status}。已保存的手册保持可读，完成后会切换到新版本。`;
    if (trip.document) return trip.delivery?.status === 'partial'
      ? '手册已保存，部分交付内容仍有缺口，现有文件可继续查看和下载。'
      : trip.delivery?.status === 'ready' ? '手册与导出文件已准备好。' : '手册已保存，可以继续查看交付进度。';
    return '这轮尚未保存手册草稿，可以调整需求后继续。';
  }
  const list = value => Array.isArray(value) ? value : [];
  const records = value => list(value).filter(item => item && typeof item === 'object' && !Array.isArray(item));
  const str = value => typeof value === 'string' || typeof value === 'number' ? String(value) : '';
  const currentPlan = () => state.trip?.plan || state.trip?.draft_plan;
  const managedTrip = (trip = state.trip) => maincore?.enabled && trip?.id === maincore.client.state.snapshot?.trip?.id
    && list(maincore.client.state.snapshot?.goals).length > 0;
  const tripRunning = (trip = state.trip) => managedTrip(trip) ? maincore.running : trip?.status === 'running';
  // One append-only set for this frontend session, shared by every rendering path.
  let unavailableSources = new Set();
  let validatedSources = new Set();
  const experienceHelpers = () => ({ safeURL, link, navigationLink, imageCredit, unavailableSources, validatedSources });
  window.TravelDock?.mount();
  window.TravelJourney.mount();
  window.TravelHandbook.mount({
    onDay: index => { state.day = index; renderDays(currentPlan()); },
    onView: view => { if (view === 'daily') requestAnimationFrame(refreshMapLayout); window.TravelChrome?.syncView?.(view); },
    announce
  });
  window.TravelChrome?.mount({ onDay: index => { state.day = index; renderDays(currentPlan()); } });

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = str(text);
    return node;
  }
  function safeURL(value) {
    const safe = window.TravelPreparation.safeURL(value);
    return safe && !unavailableSources.has(window.TravelPreparation.urlKey(safe)) ? safe : '';
  }
  function link(text, value, className = '') {
    const url = safeURL(value);
    if (!url || !validatedSources.has(window.TravelPreparation.urlKey(url))) {
      return el('span', className, `${text}${url ? ` · 来源未验证（不可点击）：${url}` : ''}`);
    }
    const node = el('a', className, text);
    node.href = url; node.target = '_blank'; node.rel = 'noopener noreferrer';
    return node;
  }
  // Only generated map controls receive this capability. Source renderers receive link.
  function navigationLink(text, value, className = '') {
    const url = safeURL(value);
    if (!url || !/^https:\/\/www\.google\.com\/maps\//.test(url)) return el('span', className, text);
    const node = el('a', className, text); node.href = url;
    node.target = '_blank'; node.rel = 'noopener noreferrer'; return node;
  }
  function date(value, withTime = false) {
    const parsed = new Date(value);
    if (!value || Number.isNaN(parsed.valueOf())) return '时间未提供';
    return parsed.toLocaleString('zh-CN', withTime
      ? { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }
      : { month: 'long', day: 'numeric' });
  }
  function notice(message, reconnect = false) {
    $('notice').hidden = !message; $('notice-text').textContent = message || '';
    $('reconnect').hidden = !reconnect;
  }
  function announce(message) { $('announcer').textContent = message; }
  let uncertaintyRequestSequence = 0;
  const uncertaintyPanel = window.TravelUncertainty?.create({ container: $('uncertainty-panel'),
    getTrip: () => state.trip, safeURL,
    async query(body) {
      const requestSequence = ++uncertaintyRequestSequence;
      const tripId = state.trip?.id;
      const result = await api(tripPath('/uncertainty/availability'), 'POST', body);
      if (requestSequence === uncertaintyRequestSequence && state.trip?.id === tripId
          && state.trip.revision === body.expected_revision)
        state.trip = { ...state.trip, uncertainty: result.uncertainty };
      return result;
    } });
  const startMaterials = window.TravelIntake.createComposer({ draftFeedback: 'brief', draftStatus: 'start-draft-status', form: 'brief-form',
    zone: 'image-input-wrap', file: 'images', choose: 'choose-images', list: 'image-list',
    documentFile: 'documents', documentChoose: 'choose-documents', documentList: 'document-list',
    evidence: 'evidence', links: 'links', status: 'start-material-status', help: 'image-help',
    vision: 'vision-note', safeURL, onChange: renderControls });
  const revisionMaterials = window.TravelIntake.createComposer({ draftFeedback: 'feedback', draftStatus: 'revision-draft-status', form: 'feedback-form', details: 'revision-materials',
    zone: 'revision-image-input-wrap', file: 'revision-images', choose: 'choose-revision-images', list: 'revision-image-list',
    documentFile: 'revision-documents', documentChoose: 'choose-revision-documents', documentList: 'revision-document-list',
    evidence: 'revision-evidence', links: 'revision-links', status: 'revision-material-status', help: 'revision-image-help',
    vision: 'revision-vision-note', safeURL, onChange: renderControls });
  const profileEditor = window.TravelProfile?.create({ get: $, getTrip: () => state.trip,
    blocked: () => state.busy || maincore?.busy || !state.trip || tripRunning(),
    appendFeedback(text) {
      const current = $('feedback').value;
      const next = `${current}${current ? '\n' : ''}${text}`;
      if (next.length > 4000) { revisionMaterials.status('追加后超过 4000 字，未添加建议。已有输入和材料完整保留，请先整理输入。', true); return; }
      $('feedback').value = next; revisionMaterials.saveDraft?.(); $('feedback').focus();
      revisionMaterials.status('建议已放入调整草稿，尚未提交；请检查后自行提交。');
    },
    async save(body) {
      setBusy(true); const generation = state.generation;
      try {
        const result = await api(tripPath('/profile'), 'POST', body);
        if (generation !== state.generation) return null;
        acceptTrip(result.trip); return result.trip;
      } catch (error) { if (generation === state.generation) await syncTrip(true); throw error; }
      finally { if (generation === state.generation) setBusy(false); }
    }
  });
  const revisionReceipt = window.TravelRevisionReceipt?.create({ container: $('revision-receipt'),
    getTrip: () => state.trip, blocked: () => state.busy || revisionMaterials.preparing,
    submit: request => submitFeedback(request), link });
  function correctUnderstanding(text) {
    const current = $('feedback').value.trim();
    $('feedback').value = `${current ? `${current}\n` : ''}${text}`.slice(0, 4000);
    revisionMaterials.saveDraft?.();
    $('feedback').focus({ preventScroll: true });
    $('feedback').scrollIntoView({ behavior: 'instant', block: 'center' });
    revisionMaterials.status('已放入调整意见，尚未提交。补充正确内容后，再更新方案。');
    announce('纠正内容已放入调整意见，尚未提交。');
  }
  const validSession = value => value && /^[a-zA-Z0-9-]{8,100}$/.test(value.id)
    && /^[\x21-\x7E]{20,500}$/.test(value.token);
  function loadCreateProof() {
    try { state.pendingCreate = JSON.parse(localStorage.getItem(CREATE_PROOF_KEY)); }
    catch { state.pendingCreate = { invalid: true }; }
    return state.pendingCreate;
  }
  function clearCreateProof(expectedToken = null) {
    try {
      if (expectedToken && loadCreateProof()?.client_token !== expectedToken) return false;
      if (localStorage.getItem(CREATE_PROOF_KEY)) localStorage.removeItem(CREATE_PROOF_KEY);
      state.pendingCreate = null; renderRecent(); return true;
    } catch { notice('本机未能清除未确认提交的凭证，请保留当前页面后重试。', true); return false; }
  }
  const operationProof = window.TravelOperationProof || null;
  const randomHex = bytes => Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  function versionBinding() {
    try { return operationProof?.versionBinding?.(state.trip) || {}; } catch { return {}; }
  }
  // One pending identity per trip: an undetermined submission keeps its request_id
  // for explicit replay; changed input mints a new id. Never resends by itself.
  function bindOperation(kind, input) {
    if (operationProof && state.trip?.capabilities?.single_session && state.trip?.document) {
      try { return operationProof.bind({ tripId: state.trip.id, kind, input, storage: localStorage }); } catch { /* fall through */ }
    }
    return { request_id: randomHex(crypto.getRandomValues(new Uint8Array(16))), replay: false };
  }
  function confirmOperation() {
    try { operationProof?.confirm?.({ tripId: state.trip?.id, storage: localStorage }); } catch { /* pending proof stays */ }
  }
  const SINGLE_SESSION_MUTATIONS = ['/revise', '/resume', '/artifacts/retry'];
  async function createWithProof(payload) {
    if (startMaterials.saveDraft?.() === false)
      throw new Error('原输入未能保存到本机，本次需求尚未发送。请允许本机存储后重试。');
    if (!globalThis.crypto?.subtle || !globalThis.crypto?.getRandomValues)
      throw new Error('当前浏览器无法安全保存提交凭证，本次需求尚未发送。');
    const canonical = value => Array.isArray(value) ? value.map(canonical)
      : value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;
    const hex = bytes => Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
    const fingerprint = hex(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(canonical(payload))))));
    let pending = loadCreateProof();
    if (pending && (!/^[a-f0-9]{32}$/.test(pending.request_id) || !/^[a-f0-9]{64}$/.test(pending.client_token)
        || pending.fingerprint !== fingerprint))
      throw new Error('上一份需求的提交尚未确认。请恢复原输入重试，或点击「新旅行」明确开始另一趟。');
    if (!pending) pending = { fingerprint, request_id: hex(crypto.getRandomValues(new Uint8Array(16))),
      client_token: hex(crypto.getRandomValues(new Uint8Array(32))) };
    try {
      localStorage.setItem(CREATE_PROOF_KEY, JSON.stringify(pending));
      if (localStorage.getItem(CREATE_PROOF_KEY) !== JSON.stringify(pending)) throw new Error('proof not persisted');
    } catch { throw new Error('浏览器未能保存提交凭证，本次需求尚未发送。请允许本机存储后重试。'); }
    state.pendingCreate = pending; renderRecent();
    return { ...payload, request_id: pending.request_id, client_token: pending.client_token };
  }
  function persistLibrary() {
    try {
      const stored = JSON.parse(localStorage.getItem(LIBRARY_KEY));
      const forgotten = new Set([...list(stored?.forgottenIds), ...state.library.forgottenIds]);
      const merged = new Map();
      [...state.library.sessions, ...list(stored?.sessions)].filter(validSession).forEach(item => {
        if (!forgotten.has(item.id) && !merged.has(item.id)) merged.set(item.id, item);
      });
      state.library.sessions = [...merged.values()]; state.library.forgottenIds = [...forgotten];
      if (!merged.has(state.library.activeId)) state.library.activeId = null;
      localStorage.setItem(LIBRARY_KEY, JSON.stringify(state.library)); state.storageFailed = false;
    }
    catch { state.storageFailed = true; }
    $('storage-warning').hidden = !state.storageFailed;
    renderRecent(); return !state.storageFailed;
  }
  function saveSession(session, trip = null) {
    revisionMaterials.restoreDraft?.(session?.id);
    state.session = session; state.library.activeId = session?.id || null;
    if (session) {
      const prior = state.library.sessions.find(item => item.id === session.id);
      const entry = { ...prior, id: session.id, token: session.token,
        title: trip?.plan?.title || prior?.title || '正在准备的旅行',
        createdAt: trip?.created_at || prior?.createdAt, isExample: Boolean(session.isExample || prior?.isExample) };
      state.library.sessions = [entry, ...state.library.sessions.filter(item => item.id !== session.id)];
    }
    persistLibrary(); maincore?.select(session);
  }
  function loadLibrary() {
    try {
      const stored = JSON.parse(localStorage.getItem(LIBRARY_KEY));
      if (stored && Array.isArray(stored.sessions)) {
        const forgottenIds = list(stored.forgottenIds);
        state.library = { sessions: stored.sessions.filter(validSession).filter(item => !forgottenIds.includes(item.id)),
          activeId: stored.activeId, forgottenIds };
      } else {
        const legacy = JSON.parse(localStorage.getItem(SESSION_KEY));
        if (validSession(legacy)) saveSession(legacy);
      }
    } catch { state.storageFailed = true; $('storage-warning').hidden = false; }
    state.session = state.library.sessions.find(item => item.id === state.library.activeId) || null;
    renderRecent();
  }
  function renderRecent() {
    const sessions = state.library.sessions;
    $('recent-toggle').hidden = !sessions.length;
    $('new-trip').hidden = !state.session && !state.pendingCreate;
    $('recent-list').replaceChildren(...sessions.map(session => {
      const row = el('li'); const button = el('button', 'recent-trip'); button.type = 'button';
      button.dataset.tripId = session.id; button.disabled = state.busy;
      button.setAttribute('aria-current', String(session.id === state.session?.id));
      const copy = el('span', '', session.title || '上次的旅行');
      copy.append(el('small', '', `${session.isExample ? '示例需求 · ' : ''}${date(session.createdAt)} · 此浏览器可恢复`));
      button.append(copy, el('span', '', session.id === state.session?.id ? '当前' : '继续 ↗'));
      button.addEventListener('click', () => restoreSession(session)); row.append(button); return row;
    }));
  }
  function showRecent(show) {
    $('recent-trips').hidden = !show; $('recent-toggle').setAttribute('aria-expanded', String(show));
    if (show) $('recent-list').querySelector('button')?.focus();
  }
  async function api(path, method = 'GET', body, session = state.session) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), path.endsWith('/message') ? 30000 : 20000);
    const headers = { Accept: 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (session) headers.Authorization = `Bearer ${session.token}`;
    try {
      const response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal, credentials: 'same-origin', cache: 'no-store' });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = new Error(data.error?.message_zh || `服务暂时无法处理请求（${response.status}）。`);
        error.status = response.status;
        error.retryAfter = Math.min(60, Math.max(2, Number(response.headers.get('Retry-After')) || 5));
        throw error;
      }
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('连接超时。任务可能仍在服务端继续，请重新连接查看状态。');
      if (error instanceof TypeError) throw new Error('暂时连接不到服务。任务不会因此被标成完成，请检查连接后重试。');
      throw error;
    } finally { clearTimeout(timer); }
  }
  // Raw text fetch for saved-document HTML (reading-view media reuse). api() JSON-parses; the document endpoint serves HTML.
  async function apiText(path, method = 'GET', session = state.session) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20000);
    const headers = { Accept: 'text/html' };
    if (session) headers.Authorization = `Bearer ${session.token}`;
    try {
      const response = await fetch(path, { method, headers, signal: controller.signal, credentials: 'same-origin', cache: 'no-store' });
      if (!response.ok) throw new Error(`saved document ${response.status}`);
      return await response.text();
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('连接超时。');
      if (error instanceof TypeError) throw new Error('暂时连接不到服务。');
      throw error;
    } finally { clearTimeout(timer); }
  }
  const tripPath = (suffix = '') => `/api/agent/trips/${encodeURIComponent(state.session.id)}${suffix}`;
  function setBusy(value) { state.busy = value; renderControls(); }
  function deliveryGapImpact(gap) {
    const artifact = { handbook_pdf: '完整手册 PDF', first_eye_pdf: '第一眼 PDF', first_eye: '第一眼内容' }[gap.artifact] || '手册';
    if (gap.rule === 'image_decode_gap') return `${artifact}部分配图未能嵌入，相应页面以文字说明为主。`;
    if (gap.rule === 'embedded_image_mismatch') return `${artifact}配图与内容未完全对应，以实际嵌入的为准。`;
    if (gap.rule === 'over_page_limit') return `${artifact}页数偏多，阅读时可以靠目录直接定位。`;
    const fields = { design: '设计与取舍', days: '逐日安排', route: '路线图', photos: '景点照片',
      rationale: '安排理由', seasons: '季节建议', correction: '说法核对' };
    const field = fields[str(gap.field_path).split('.')[1]?.split('[')[0]];
    return `${artifact}${field ? ` · ${field}` : ''}：${str(gap.message) || '这部分内容尚未齐备。'}`;
  }
  function deliveryGapDetail(gap) {
    const artifact = { handbook_pdf: '完整手册 PDF', first_eye_pdf: '第一眼 PDF', first_eye: '第一眼内容' }[gap.artifact] || '手册';
    if (['image_decode_gap', 'embedded_image_mismatch'].includes(gap.rule)
      && Number.isInteger(gap.expected) && Number.isInteger(gap.actual)) {
      return `${artifact}（应有 ${gap.expected} 张，${gap.rule === 'image_decode_gap' ? '成功加载' : '实际嵌入'} ${gap.actual} 张）`;
    }
    if (gap.rule === 'over_page_limit' && Number.isInteger(gap.actual)) return `${artifact}（共 ${gap.actual} 页，超过 3 页精简阅读标准）`;
    return deliveryGapImpact(gap);
  }
  function renderDeliveryGaps(trip) {
    let panel = $('delivery-quality-gaps');
    if (!panel) {
      panel = el('div', 'preparation-progress'); panel.id = 'delivery-quality-gaps';
      panel.setAttribute('aria-live', 'polite'); $('task-state').append(panel);
    }
    const gaps = records(trip.delivery?.quality_gaps);
    panel.replaceChildren(); panel.hidden = !gaps.length;
    if (!gaps.length) return;
    const items = el('ul');
    items.append(...gaps.map(gap => el('li', '', deliveryGapImpact(gap))));
    const details = el('details', 'gap-details');
    const summary = el('summary', '', '查看详细诊断');
    const detailItems = el('ul');
    detailItems.append(...gaps.map(gap => el('li', '', deliveryGapDetail(gap))));
    details.append(summary, detailItems);
    panel.append(el('p', '', '已保存内容仍可阅读，还有几处缺口在处理：'), items, details);
  }
  function sessionRecovery() {
    if (maincore?.enabled || !state.trip?.capabilities?.single_session) return null;
    if (state.trip.status === 'failed' && state.trip.generation_status !== 'completed')
      return { path: '/resume', label: '继续未完成的准备', message: '已请求从保存的进度继续准备。' };
    if (state.trip.document && state.trip.delivery?.retry_url)
      return { path: '/artifacts/retry', label: '重试导出文件', message: '已请求重新导出保存的手册，不会重新生成行程。' };
    if (state.trip.status === 'failed')
      return { path: '/resume', label: '继续未完成的准备', message: '已请求从保存的进度继续准备。' };
    return null;
  }
  function renderControls() {
    const running = tripRunning();
    const busy = state.busy || maincore?.busy;
    const routingPending = !maincore?.known || (maincore.enabled && state.session && maincore.client.state.snapshot?.trip?.id !== state.session.id);
    profileEditor?.update();
    revisionReceipt?.render();
    if (state.trip?.capabilities?.single_session) window.TravelDeliveryReceipt?.render?.($('revision-receipt'), state.trip, document);
    renderPendingMaterials();
    $('open-preparation').hidden = !currentPlan();
    // Legacy handbook actions export the plan document; keep them dead whenever
    // there is nothing exportable (single-session failure states never reach the
    // legacy handbook render path, which is the only other place they are gated).
    const handbookExportable = Boolean(currentPlan()?.days?.length);
    if ($('handbook-download')) $('handbook-download').disabled = !handbookExportable;
    if ($('handbook-copy')) $('handbook-copy').disabled = !handbookExportable;
    window.TravelPreparation.updateControls($('preparation-pack'), { busy, running, pending: state.preparing });
    $('create-trip').disabled = busy || startMaterials.preparing || !maincore?.known || (maincore.enabled ? !maincore.modelReady : !state.cap?.planner_available);
    $('brief').disabled = busy; $('feedback').disabled = busy;
    const materialCap = maincore?.enabled ? { ...state.cap, vision_available: false } : state.cap;
    startMaterials.update({ busy, cap: materialCap });
    revisionMaterials.update({ busy: busy || running, cap: materialCap });
    $('revise').disabled = busy || running || routingPending || (maincore?.enabled && !maincore.modelReady) || revisionMaterials.preparing || !state.trip;
    $('undo').hidden = !state.trip?.can_undo || (state.trip?.capabilities?.single_session && !maincore?.enabled);
    $('undo').disabled = busy || running;
    const recovery = sessionRecovery();
    const singleSession = Boolean(state.trip?.capabilities?.single_session);
    $('retry').hidden = singleSession ? !recovery : (!recovery && !['error', 'interrupted', 'needs_attention'].includes(state.trip?.status));
    $('retry').disabled = busy || running || routingPending;
    $('retry').textContent = recovery?.label || (state.trip?.revision_progress?.can_resume ? '继续未完成的调整' : '继续研究');
    $('delete-trip').disabled = busy || running;
    $('start-demo').disabled = busy;
    $('start-demo').classList.toggle('primary-button', !state.exampleDraft);
    $('start-demo').classList.toggle('secondary-button', state.exampleDraft);
    $('new-trip').disabled = busy;
    $('try-demo-feedback').disabled = busy || running;
    document.querySelectorAll('.recent-trip').forEach(button => { button.disabled = busy; });
    document.querySelectorAll('.lock-button').forEach(button => { button.disabled = busy || running || !state.trip?.plan; });
    $('maincore-settings').hidden = !maincore?.enabled;
    $('maincore-search').disabled = busy; $('maincore-auto-apply').disabled = busy;
    document.querySelectorAll('.reaction-button').forEach(button => { button.disabled = state.busy || running || !state.trip?.plan; });
    document.querySelectorAll('[data-correction]').forEach(button => { button.disabled = state.busy || running || !state.trip; });
    document.querySelectorAll('.learning-select input').forEach(input => { input.disabled = state.busy || running || !state.trip?.plan; });
    document.querySelectorAll('[data-transfer]').forEach(button => { button.disabled = state.busy || running || !state.trip?.plan || !document.querySelector('.learning-select input:checked'); });
    // P0-10 #9：0 项时整行不渲染，有锁定才出现。
    const lockCount = list(state.trip?.locked_item_ids).length;
    $('lock-count').textContent = state.trip?.plan ? `${lockCount} 项安排已锁定` : '原有材料会一并保留';
    $('lock-count').hidden = Boolean(state.trip?.plan) && lockCount === 0;
  }
  function renderPendingMaterials() {
    const pool = $('pending-materials');
    const pending = records(state.trip?.pending_materials).filter(item => item.status === 'awaiting_assignment');
    pool.hidden = !pending.length;
    pool.replaceChildren();
    if (!pending.length) return;
    pool.append(el('p', 'field-hint', '材料已保存，可供参考；用途尚未指派，你也可以补充日期和事项'));
    const items = el('ul');
    pending.forEach(material => {
      const label = str(material.label) || str(material.source_id);
      const row = el('li');
      const button = el('button', 'secondary-button', '补充用途（可选）');
      button.type = 'button';
      button.disabled = state.busy || tripRunning();
      button.addEventListener('click', () => {
        if (state.busy || tripRunning()) return;
        const input = $('feedback');
        const suggestion = `使用材料「${label}」：请补充第几天、用于交通/住宿/活动等事项`;
        const draft = `${input.value}${input.value ? '\n' : ''}${suggestion}`;
        if (draft.length > 4000) {
          revisionMaterials.status('调整意见空间不足，请先整理已有内容，再填写材料用途。');
          return;
        }
        input.value = draft; revisionMaterials.saveDraft?.();
        input.focus({ preventScroll: true });
        revisionMaterials.status('已放入调整意见，尚未提交。请填写具体日期和事项后，再更新方案。');
        announce('材料用途已放入调整意见，尚未提交。');
      });
      row.append(el('span', '', label), el('p', 'field-hint', str(material.reason)), button);
      items.append(row);
    });
    pool.append(items);
  }
  // 问答记录（PRD 拍板 #3）：流里只留最新一条，历史进档案，只存本次打开期间。
  function renderQaHistory() {
    const listEl = $('qa-history-list');
    if (!listEl || !$('qa-history')) return;
    $('qa-history').hidden = !state.messageLog.length;
    listEl.replaceChildren(...state.messageLog.map(entry => {
      const card = el('article', 'qa-entry');
      card.append(el('p', 'qa-question', `你：${entry.question}`));
      const body = el('div', 'qa-answer');
      if (entry.kind === 'clarify') {
        body.append(el('p', '', `系统请你确认这次输入的意思：${str(entry.result.question)}`));
      } else {
        body.append(el('p', 'message-answer', str(entry.result.answer?.text) || '当前材料不足以回答。'));
        records(entry.result.answer?.judgments).forEach(judgment => {
          if (typeof judgment?.text === 'string' && judgment.text)
            body.append(el('p', 'qa-judgment', `${str(judgment.label) || '判断'} · 非已核实事实：${judgment.text}`));
        });
      }
      card.append(body);
      return card;
    }));
  }
  async function capabilities() {
    try {
      const [legacy] = await Promise.all([api('/api/agent/capabilities', 'GET', undefined, null),maincore.capabilities()]);
      state.cap = legacy;
      $('capability').textContent = maincore.enabled
        ? maincore.modelReady ? '从当前需求开始，必要时请你做选择；依据、安排与后续调整都留在这趟旅行里。'
          : '旅行助手尚未启用模型。可以查看已有旅行、保留草稿，暂不提交新的规划或调整。'
        : state.cap.planner_available
        ? '先呈现找到的地点与图片，再组织每日安排；完整方案按内容复杂度需要一些时间。'
        : '规划模型暂不可用。请确认本地服务的模型配置后重新连接。';
      if (!state.cap.planner_available && !maincore.enabled && !state.trip) notice('规划模型暂不可用，你可以先写下需求，配置恢复后继续。', true);
      if (maincore.enabled && state.session) await syncTrip(true);
    } catch (error) {
      state.cap = null;
      $('capability').textContent = '尚未连接规划服务。你的输入仍保留在当前页面。';
      if (!state.trip) notice(error.message, true);
    } finally { renderControls(); }
  }
  async function configureMaps() {
    try {
      const config = await api('/api/agent/maps-config', 'GET', undefined, null);
      window.TravelGoogleMaps.configure(config); state.mapsConfigured = Boolean(config.configured);
    } catch {
      window.TravelGoogleMaps.configure({ configured: false }); state.mapsConfigured = false;
    }
    state.mapsReady = true;
    if (currentPlan()) {
      const day = records(currentPlan().days)[state.day]; renderMap(records(day?.items), day);
      renderPlaceMedia();
    }
  }
  function renderPlaceMedia() {
    if (!state.trip || !state.session || !state.mapsReady) return;
    const session = { ...state.session };
    const path = `/api/agent/trips/${encodeURIComponent(session.id)}/media`;
    window.TravelPlaceMedia.render(state.trip, { safeURL, link, unavailableSources, validatedSources,
      google: state.mapsConfigured ? window.TravelGoogleMaps : null,
      request: (method, body) => api(path, method, body, session),
      savedMediaRequest: (method, body) => apiText(tripPath('/document'), method, session) });
    window.TravelPlaceMedia.selectDay(records(currentPlan()?.days)[state.day]);
  }
  function schedulePoll(delay = 1800) {
    clearTimeout(state.pollTimer);
    if (state.session && tripRunning()) state.pollTimer = setTimeout(syncTrip, delay);
  }
  async function syncTrip(force = false) {
    if (!state.session) return;
    if (state.busy && !force) { schedulePoll(); return; }
    const generation = state.generation;
    try {
      if (maincore?.enabled) await maincore.refresh();
      if (generation !== state.generation) return;
      const result = await api(tripPath());
      if (generation !== state.generation) return;
      if (!result.trip) throw new Error('服务未返回任务状态，请重新连接。');
      state.pollFailures = 0;
      notice(state.mutationNotice || (state.storageFailed ? '浏览器未允许保存任务访问凭证。当前页面可用，但关闭后无法自动恢复。' : ''), Boolean(state.mutationNotice));
      acceptTrip(result.trip); settleMaincoreDraft(); schedulePoll();
    } catch (error) {
      if (generation !== state.generation) return;
      state.pollFailures += 1;
      const unauthorized = error.status === 401 || error.status === 403 || error.status === 404;
      notice(unauthorized ? `${error.message} 此浏览器的访问记录仍保留；可重连，或在删除任务时选择移除本地记录。`
        : `${error.message} 已显示的方案保持不变。`, true);
      if (!unauthorized) {
        const delay = error.status === 429 ? error.retryAfter * 1000 : Math.min(30000, 2000 * 2 ** Math.min(state.pollFailures, 4));
        clearTimeout(state.pollTimer); state.pollTimer = setTimeout(syncTrip, delay);
      }
    }
  }
  function acceptTrip(trip) {
    const previousStatus = state.trip?.status;
    state.trip = trip;
    const hasSavedDelivery = Boolean(trip.plan || (trip.capabilities?.single_session && trip.document));
    const running=tripRunning(trip), managed=managedTrip(trip);
    const displayTrip=managed ? {...trip,status:running ? 'running' : trip.plan ? 'ready' : 'needs_attention'} : trip;
    profileEditor?.accept(trip);
    revisionStage?.render(trip, state.generation);
    unavailableSources = window.TravelPreparation.unavailableSourceURLs([
      ...window.TravelPreparation.planSources(trip.plan), ...window.TravelPreparation.planSources(trip.draft_plan),
      ...records(trip.discovery?.supplies), ...records(trip.materials), ...(window.TravelRevisionReceipt?.sources(trip.revision_receipt) || [])
    ], unavailableSources);
    validatedSources = window.TravelPreparation.validatedSourceURLs([
      ...window.TravelPreparation.planSources(trip.plan), ...window.TravelPreparation.planSources(trip.draft_plan),
      ...records(trip.discovery?.supplies), ...records(trip.materials)
    ]);
    if (state.messageContext && state.messageContext !== messageContext()) {
      state.messageContext = ''; $('message-response').replaceChildren(); $('message-response').hidden = true;
      revisionMaterials.status('方案或来源状态已更新，旧答复已收起；输入仍保留，可重新提问。');
    }
    const displayPlan = currentPlan();
    uncertaintyPanel?.render(trip);
    $('workspace-records').hidden = false;
    const saved = state.library.sessions.find(item => item.id === trip.id);
    if (state.session && trip.plan?.title && saved?.title !== trip.plan.title) saveSession(state.session, trip);
    $('demo-guide').hidden = !saved?.isExample || !trip.plan;
    $('onboarding').hidden = true; $('workspace').hidden = false;
    window.TravelDock?.show(true);
    $('trip-meta').textContent = `${date(trip.created_at)} · ${trip.capabilities?.single_session
      ? trip.document ? `手册版本 ${trip.document.version}` : '尚未保存手册草稿'
      : Number.isInteger(trip.plan_revision) ? `方案版本 ${trip.plan_revision}` : '历史方案版本未记录'}`;
    $('state-title').textContent = managed ? (running ? '旅行助手正在处理这次需求' : maincore.run()?.result?.error_code
      ? '本轮已停止，已有方案与草稿保留' : trip.plan ? '你的旅行安排可以继续查看和调整' : '接着选择或补充，让旅行继续往前')
      : running && trip.job_kind === 'prepare'
      ? '正在查漏补缺，现有行程保持不变' : running && hasSavedDelivery
        ? '正在调整，原方案仍可阅读' : statuses[trip.status] || '正在读取任务状态';
    const events = records(trip.events);
    const elapsedLabel = revisionElapsed?.label(trip) || '';
    const candidates = records(trip.discovery?.supplies);
    const visibleResults = candidates.length
      ? `已找到 ${candidates.length} 个地点，${candidates.filter(s => s.image_url).length} 张来源图片` : '';
    const graph = trip.trip_graph?.summary;
    const restored = graph ? `已恢复 ${Number(graph.evidence_backed_facts) || 0} 个有依据事实 · ${Number(graph.needs_user) || 0} 个需你确认` : '';
    const progressActive = running && Boolean(trip.revision_progress?.total);
    $('state-copy').textContent = managed ? maincore.run()?.result?.user_summary || (running ? '需求已保存，实际进展见下方。已有安排保持可读。' : '选择、材料与实际结果都保存在这趟旅行里。') : running
      ? (progressActive
        // 进度要素已收入进度卡，此处不重复表达。
        ? [visibleResults, trip.plan ? '上一份有效方案仍保留。' : '完整行程还未完成；已显示内容可先看，任务已保存。'].filter(Boolean).join(' · ')
        : [str(events.at(-1)?.label) || '等待服务端进展', restored, elapsedLabel, visibleResults,
          trip.plan ? '上一份有效方案仍保留。' : '完整行程还未完成；已显示内容可先看，任务已保存。'].filter(Boolean).join(' · '))
      : trip.capabilities?.single_session ? singleSessionBaseCopy(trip)
      : str(trip.error?.message_zh || trip.error?.message || trip.error) || (trip.plan
        ? [restored, '这是可调整的旅行提案，没有自动预订或付款。'].filter(Boolean).join(' · ')
        : '还没有形成完整方案，可以补充材料或继续研究。');
    if (!maincore?.enabled && trip.capabilities?.single_session && !running && trip.status === 'failed' && trip.document) {
      $('state-title').textContent = '本轮没有完成，已保存的手册仍可阅读';
      const failureReason = str(trip.error?.message_zh || trip.error?.message);
      if (failureReason) $('state-copy').textContent = `本轮没有完成：${failureReason} 已保存的手册与资料保留，可选择继续未完成的准备。`;
    }
    if (!maincore?.enabled && trip.capabilities?.single_session && !running && !trip.document) {
      $('state-title').textContent = '本轮尚未生成可保存的手册';
      const reason = trip.error?.type || trip.error?.code;
      const action = reason === 'BudgetExhausted' ? '本轮调用额度已用完。可以保留现有资料，确认后继续本轮。'
        : reason === 'WriterTokenizerError' ? '本轮在提交内容前未通过输入检查。可以调整需求后继续，已有资料会保留。'
          : reason === 'interrupted' ? '处理曾中断。可以点击继续本轮，沿用已收集的资料。'
            : '本轮生成未完成。可以补充或调整需求后继续本轮，已有资料会保留。';
      $('state-copy').textContent = `这轮尚未保存手册草稿。${action}`;
    }
    if (!maincore?.enabled && trip.capabilities?.single_session && trip.document && trip.handbook_url) {
      try {
        const reader = new URL(trip.handbook_url, location.href);
        if (reader.origin === new URL(location.href).origin && reader.pathname === `/api/agent/trips/${trip.id}/handbook`) {
          $('state-copy').textContent = running ? '本轮仍在准备，已保存的手册可先阅读。'
            : trip.delivery?.retry_url ? '手册已保存，部分导出文件缺失或损坏，可点击重试导出文件。'
              : trip.delivery?.status === 'partial' ? '手册已保存，内容或图片仍有缺口，现有文件可继续查看和下载。'
              : trip.status === 'failed' ? '本轮尚未确认完成，已保存的手册仍可阅读。' : '已保存的手册与导出文件可继续查看。';
          const open = el('a', '', '独立打开手册'); open.href = reader.href;
          open.target = '_blank'; open.rel = 'noopener noreferrer';
          $('state-copy').append(document.createTextNode(' '), open);
        }
      } catch { /* Keep the existing status when a reader link is invalid. */ }
    }
    window.TravelSavedHandbook?.update?.(trip);
    renderDeliveryGaps(trip);
    $('task-state').classList.toggle('is-error', ['error', 'interrupted'].includes(trip.status));
    $('task-state').classList.toggle('has-plan', hasSavedDelivery);
    window.TravelDock?.setStatus(running && !progressActive
      ? [str(events.at(-1)?.label), elapsedLabel].filter(Boolean).join(' · ') : '');
    let revisionProgress = $('revision-progress');
    if (!revisionProgress) {
      revisionProgress = el('div', 'preparation-progress'); revisionProgress.id = 'revision-progress';
      revisionProgress.setAttribute('aria-live', 'polite'); $('task-state').append(revisionProgress);
    }
    window.TravelPreparation.renderRevisionProgress(revisionProgress, trip.revision_progress, {
      stage: trip.revision_stage, stageLabel: str(events.at(-1)?.label), restored, elapsed: elapsedLabel });
    if (previousStatus === 'running' && trip.plan && !running) {
      $('event-details').open = false;
      window.TravelDock?.scrollToLatest();
    }
    $('events').replaceChildren(...events.map(event => {
      const row = el('li'); const time = el('time', '', date(event.at, true));
      if (event.at) time.dateTime = str(event.at);
      row.append(time, el('span', '', event.label)); return row;
    }));
    if (!events.length) $('events').append(el('li', '', '服务尚未返回事件。'));
    window.TravelJourney.renderWork(displayTrip);
    window.TravelIntake.renderIntake(displayTrip, { link, unavailableSources, validatedSources, correct: correctUnderstanding });
    renderPreview(displayTrip);
    window.TravelExperience.renderDiscovery(displayTrip, experienceHelpers());
    $('plan-content').hidden = !displayPlan;
    $('draft-note').hidden = Boolean(trip.plan) || !trip.draft_plan;
    $('feedback-section').hidden = !trip.plan && running;
    if (maincore?.active) $('feedback-section').hidden = false;
    $('model-meta').hidden = !trip.plan;
    $('supply-section').hidden = !displayPlan;
    $('legacy-understanding').hidden = !displayPlan;
    $('reaction-status').textContent = running && hasSavedDelivery
      ? '正在调整相关安排。原方案仍可阅读，完成后可以撤销。' : '';
    $('feedback-title').replaceChildren(document.createTextNode(hasSavedDelivery ? '想问什么，' : '补充一点，'), el('br'), document.createTextNode(hasSavedDelivery ? '或改哪里。' : '继续准备。'));
    $('feedback-guidance').textContent = hasSavedDelivery ? (trip.capabilities?.single_session
      ? '可以继续调整已保存的手册。修改会给出回执，原版本保留。'
      : '提问直接回答，调整会更新方案。锁定的安排会保留。')
      : '还没有形成完整方案。可以补充或纠正材料，提交后会继续准备，不必重新开始。';
    $('revise-label').textContent = hasSavedDelivery ? '提交问题或调整' : '补充并继续准备';
    if (!hasSavedDelivery && !running) $('revision-materials').open = true;
    if (displayPlan) {
      const fingerprint = JSON.stringify([displayPlan, trip.locked_item_ids, Boolean(trip.plan), [...unavailableSources].sort()]);
      if (fingerprint !== state.renderedPlan) { state.renderedPlan = fingerprint; renderPlan(displayPlan); }
    }
    renderControls();
    maincore?.render();
    if (previousStatus && previousStatus !== trip.status) announce(statuses[trip.status] || '任务状态已更新');
  }
  function supplyMap() { return new Map([...records(state.trip?.discovery?.supplies), ...records(currentPlan()?.supplies)].map(supply => [supply.id, supply])); }
  function renderPreview(trip) {
    const preview = trip.preview;
    const direction = trip.core_direction;
    const understandingItems = trip.intake ? [] : records(preview?.understanding);
    const experienceAngles = records(direction?.experience_angles || preview?.experience_angles);
    $('preview').hidden = Boolean(trip.plan || trip.draft_plan) || (!preview && !direction)
      || (!direction && !understandingItems.length && !experienceAngles.length);
    if ($('preview').hidden) return;
    const fingerprint = JSON.stringify([preview, direction, trip.status]);
    if (fingerprint === state.renderedPreview) return;
    state.renderedPreview = fingerprint;
    const heading = el('div', 'preview-heading');
    heading.append(el('span', 'folio', '先给这趟旅行一个方向'), el('h2', '', direction?.title || '这趟旅行，可以这样展开'),
      el('p', 'direction-premise', direction?.premise || '这是根据你提供的信息提出的旅行方向，接着会把它安排到每一天。'),
      el('p', 'direction-state', trip.status === 'running' ? '正在寻找合适的地点，并串起住宿、餐饮与每天的安排。' : '方向已提出，完整安排尚未完成；可以继续准备。'));
    const columns = el('div', 'preview-columns preview-angles-only');
    const understanding = el('wa-details', 'direction-assumptions');
    understanding.append(el('summary', '', '我从材料里找到了什么'));
    const understandingList = el('ul', 'understanding-list');
    const assumptions = list(direction?.assumptions);
    understandingItems.forEach(item => {
      const backed = item.kind && item.kind !== 'hypothesis';
      const row = el('li', '', item.text);
      row.append(el('small', '', `${backed ? '材料有依据' : '待确认推测'} · ${str(item.source)}`)); understandingList.append(row);
    });
    assumptions.forEach(value => {
      const row = el('li', '', value); row.append(el('small', '', '可纠正的临时条件')); understandingList.append(row);
    });
    understanding.append(understandingList);
    const angles = el('div'); angles.append(el('h3', '', '我想让你这样度过'));
    experienceAngles.forEach(angle => {
      const row = el('article', 'preview-angle'); row.append(el('h4', '', angle.idea), el('p', '', angle.why));
      if (angle.tradeoff) row.append(el('p', 'tradeoff', `待权衡：${str(angle.tradeoff)}`)); angles.append(row);
    });
    columns.append(angles);
    if (understandingList.children.length) columns.append(understanding);
    $('preview').replaceChildren(heading, columns);
    announce('旅行方向已返回，正在继续安排具体行程。');
  }
  function supplyLinks(ids) {
    const group = el('div', 'source-links'); const supplies = supplyMap();
    [...new Set(list(ids))].forEach(id => {
      const supply = supplies.get(id);
      if (!supply) return;
      const anchor = el('a', '', supply.name); anchor.href = `#${encodeURIComponent(`supply-${str(id)}`)}`;
      anchor.addEventListener('click', () => { $('workspace-records').open = true; });
      group.append(anchor);
    });
    return group;
  }
  function verificationText(value) {
    if (typeof value === 'string') return value;
    if (!value || typeof value !== 'object') return '来源核验方式未说明';
    if (value.status === 'official_content_reviewed') return '已查阅官方页面的供给内容；营业安排需按日期复核，价格与库存仍未知。';
    const names = { status: '状态', method: '核验方式', note: '说明', notes: '说明',
      checked_at: '检查时间', scope: '范围', source: '来源', detail: '详情' };
    return Object.entries(value).filter(([, entry]) => typeof entry === 'string' || typeof entry === 'boolean')
      .map(([key, entry]) => `${names[key] || key}：${entry === true ? '是' : entry === false ? '否' : entry}`).join(' · ') || '来源核验信息仍待补充';
  }
  function imageCredit(supply) {
    const parts = [supply.image_author, supply.image_license].filter(value => typeof value === 'string' && value.trim());
    return `${str(supply.image_attribution) || parts.join(' · ') || '图片来源见供给页面'}。历史照片，非实时画面；展示时按版式裁切。`;
  }
  function renderStory(plan) {
    const copy = el('div', 'story-copy'); copy.append(el('span', 'proposal-label', `${records(plan.days).length} 天 · 你的随身旅行手册`));
    const title = el('h1', '', plan.title || '你的旅行方案'); title.id = 'plan-title';
    const summary = window.TravelJourney.shortCopy(plan.summary, 108);
    copy.append(title, el('p', 'summary', summary.preview));
    if (plan.journey?.goal && str(plan.journey.goal) !== str(plan.summary)) {
      copy.append(el('p', 'story-intention', window.TravelJourney.shortCopy(plan.journey.goal, 66).preview));
    }
    const context = el('wa-details', 'story-context');
    context.append(el('summary', '', '这趟旅行为什么这样安排'), el('p', '', plan.summary || '安排说明尚待补充'));
    // goal 与 summary 同源拼接时往往前段重复：前缀重合或互相包含就不再追加，避免一屏两遍
    const goal = str(plan.journey?.goal); const brief = str(plan.summary);
    if (goal && goal !== brief
      && !goal.startsWith(brief.slice(0, 20)) && !brief.startsWith(goal.slice(0, 20))
      && !goal.includes(brief) && !brief.includes(goal)) {
      context.append(el('p', '', plan.journey.goal));
    }
    copy.append(context);
    const itineraryLink = el('a', 'text-link', '看看每一天怎么过 ↓'); itineraryLink.href = '#itinerary-title';
    itineraryLink.addEventListener('click', () => window.TravelHandbook.selectView('daily'));
    copy.append(itineraryLink);
    const figure = el('figure', 'hero-figure'); const media = el('div', 'hero-media');
    const fallback = el('div', 'media-fallback'); fallback.append(el('strong', '', '一趟。'), el('span', '', '每个安排，都回到你的期待。'));
    media.append(fallback);
    const caption = el('figcaption', '', '没有可用的来源图片时，以旅行文字与真实地点呈现。');
    const supply = window.TravelExperience.heroSupply(plan, safeURL);
    $('story').classList.toggle('story-text-only', !supply);
    if (!supply) { $('story').replaceChildren(copy); return; }
    if (supply) {
      const image = el('img'); image.src = safeURL(supply.image_url); image.alt = str(supply.name);
      image.referrerPolicy = 'no-referrer'; image.decoding = 'async';
      image.addEventListener('error', () => { figure.hidden = true; $('story').classList.add('story-text-only'); }, { once: true });
      media.append(image); caption.replaceChildren(link(`${str(supply.name)} · 来源照片`, supply.image_source_url || supply.source_url), el('span', 'image-credit', imageCredit(supply)));
      if (supply.image_license_url) caption.append(link('查看图片许可', supply.image_license_url));
    }
    figure.append(media, caption); context.append(figure); $('story').replaceChildren(copy);
  }
  function renderSignature(signature = {}) {
    const heading = el('div', 'signature-heading'); heading.append(el('span', 'folio', '本次体验构想'), el('h2', '', '值得记住的，\n不止一站。'));
    const body = el('div', 'signature-body'); body.append(el('h3', '', signature.title || '体验构想仍待完善'), el('p', '', signature.why));
    const sequence = el('ol', 'signature-sequence');
    const schedule = records(signature.schedule);
    const steps = schedule.length ? schedule.map(item => `第 ${str(item.day)} 天 ${str(item.time)}${item.end_time ? ` 至 ${str(item.end_time)}` : ''} · ${str(item.title)}`) : list(signature.sequence);
    steps.forEach(step => sequence.append(el('li', '', step)));
    body.append(sequence);
    if (signature.tradeoff) body.append(el('p', 'tradeoff', `需要接受的取舍：${str(signature.tradeoff)}`));
    body.append(supplyLinks(signature.supply_ids)); $('signature').replaceChildren(heading, body);
  }
  function renderReactions(plan) {
    const reactions = records(plan.reactions).filter(item => str(item.label).trim() && str(item.feedback).trim()).slice(0, 4);
    $('reaction-section').hidden = !reactions.length;
    $('reactions').replaceChildren(...reactions.map(reaction => {
      const button = el('button', 'reaction-button'); button.type = 'button';
      button.append(el('span', '', reaction.label));
      button.setAttribute('aria-label', `${str(reaction.label)}，填入调整意见，尚不提交`);
      button.setAttribute('aria-pressed', 'false');
      button.addEventListener('click', () => {
        const feedback = str(reaction.feedback);
        if (!$('feedback').value.includes(feedback)) $('feedback').value = `${$('feedback').value.trim()}${$('feedback').value.trim() ? '\n' : ''}${feedback}`.slice(0, 4000);
        button.setAttribute('aria-pressed', 'true');
        $('reaction-status').textContent = '已放入调整意见，尚未提交。可以继续改成自己的话。';
        revisionMaterials.saveDraft?.();
        $('feedback').focus({ preventScroll: true });
        announce('已填入调整意见。确认后提交。');
      });
      return button;
    }));
  }
  function renderUnderstanding(plan) {
    const understanding = el('div'); understanding.append(el('h3', '', '目前如何理解你'));
    const items = el('ul', 'understanding-list');
    records(plan.understanding).forEach(item => {
      const row = el('li', '', item.text);
      row.append(el('small', '', `${item.kind === 'confirmed' ? '模型整理的信息 · 非外部核验' : '待你确认的假设'} · ${str(item.source) || '来源未说明'}`)); items.append(row);
    });
    if (!items.children.length) items.append(el('li', '', '当前没有额外的理解说明。'));
    understanding.append(items);
    const questions = el('div'); questions.append(el('h3', '', '值得补充的问题'));
    const questionsList = el('ol', 'questions'); list(plan.questions).forEach(question => questionsList.append(el('li', '', question)));
    questions.append(questionsList.children.length ? questionsList : el('p', 'empty-copy', '暂时没有必须补充的问题。你仍可随时调整想法。'));
    $('understanding-content').replaceChildren(understanding, questions);
  }
  function renderDays(plan) {
    const focusedDay = document.activeElement?.dataset.dayIndex;
    const days = [...records(plan.days), ...records(plan.revision_changes?.days).filter(change => change.kind === 'removed').map(change => ({ ...change.before, day: change.day, items: [] }))].sort((a, b) => Number(a.day) - Number(b.day)); state.day = Math.min(state.day, Math.max(0, days.length - 1));
    const dayHasChange = day => plan.revision_changes ? window.TravelPresentation.changesForDay(plan, day.day).length > 0 || records(plan.revision_changes.days).some(change => change.day === day.day) : records(day.items).some(item => list(plan.journey?.changes?.changed_item_ids).includes(item.id));
    const selectDay = index => { state.day = index; renderDays(currentPlan()); };
    // D 天卡横滑：与总览同一条 wa-carousel 链；缺槽位时退回按钮行。
    if (window.TravelChrome?.dayCarousel) {
      $('day-nav').replaceChildren(window.TravelChrome.dayCarousel(days, {
        selected: state.day,
        hasChange: dayHasChange,
        imageURL: day => window.TravelChrome.dayImageURL(day, supplyMap()),
        onSelect: selectDay
      }));
    } else {
      $('day-nav').replaceChildren(...days.map((day, index) => {
        const button = el('button', '', `第 ${str(day.day) || index + 1} 天`);
        button.type = 'button';
        button.dataset.dayIndex = String(index);
        button.classList.toggle('has-change', dayHasChange(day));
        button.setAttribute('aria-current', String(index === state.day));
        button.classList.toggle('selected', index === state.day);
        button.addEventListener('click', () => selectDay(index));
        return button;
      }));
    }
    const day = days[state.day];
    if (!day) { $('timeline').replaceChildren(el('p', 'empty-copy', '具体日期与安排仍待模型补充。')); renderMap([]); return; }
    const heading = el('div', 'day-heading'); heading.append(el('h3', '', day.title));
    window.TravelPresentation.renderChange(heading, records(plan.revision_changes?.days).find(change => change.day === day.day));
    heading.append(el('p', 'experience-caveat', '时间是建议安排，非已确认营业或预约时段。待落实事项见「出发准备」。'));
    const dayPack = records(plan.preparation_pack?.actions).filter(item => Number(item.day) === Number(day.day));
    if (dayPack.length) {
      const card = el('aside', 'delivery-day-prep'); const copy = el('div');
      copy.append(el('span', 'delivery-kicker', '这一天出发前'),
        el('strong', '', dayPack.length === 1 ? dayPack[0].title : `${dayPack.length} 项关键准备`),
        el('p', '', dayPack.map(item => item.title).join('、')));
      const open = el('button', 'delivery-button', '去处理 →'); open.type = 'button';
      open.addEventListener('click', () => window.TravelHandbook.selectView('prepare'));
      card.append(copy, open); heading.append(card);
    }
    if (day.rationale) { const why = el('wa-details', 'day-rationale'); why.append(el('summary', '', '为什么这样安排这一天'), el('p', '', day.rationale)); heading.append(why); }
    const dayArtifacts = window.TravelJourney.contextualArtifacts(plan, day, null, announce);
    if (dayArtifacts) { const materials = el('wa-details', 'day-rationale'); materials.append(el('summary', '', '这一天的随身材料'), dayArtifacts); heading.append(materials); }
    const timeline = el('ol', 'timeline-list');
    records(day.items).forEach(item => timeline.append(timelineItem(item, day)));
    window.TravelPresentation.removedForDay(plan, day.day).forEach(change => {
      const row = el('li', 'timeline-item is-removed'); row.dataset.itemId = change.item_id;
      row.append(el('div', 'timeline-time', change.before?.time || ''));
      const body = el('div', 'item-body'); body.append(el('h4', '', change.before?.title || change.item_id));
      window.TravelPresentation.renderChange(body, change); row.append(body); timeline.append(row);
    });
    if (!timeline.children.length) timeline.append(el('li', 'empty-copy', '这一天的具体安排尚待补充。'));
    $('timeline').replaceChildren(heading, timeline);
    renderMap(records(day.items), day); renderControls();
    window.TravelPlaceMedia.selectDay(day);
    if (focusedDay !== undefined) $('day-nav').querySelectorAll?.('.day-card')?.[state.day]?.focus?.({ preventScroll: true });
  }
  function timelineItem(item, day) {
    const row = el('li', 'timeline-item'); const time = el('div', 'timeline-time', item.time || '待定');
    const changes = currentPlan()?.journey?.changes || {};
    const currentChange = window.TravelPresentation.itemChange(currentPlan(), day.day, item.id);
    const changed = currentPlan()?.revision_changes ? Boolean(currentChange) : list(changes.changed_item_ids).includes(item.id);
    const preserved = !currentPlan()?.revision_changes && list(changes.preserved_item_ids).includes(item.id);
    row.classList.toggle('is-changed', changed); row.classList.toggle('is-preserved', preserved);
    row.dataset.itemId = str(item.id);
    if (item.end_time) time.append(el('span', '', `至 ${str(item.end_time)}`));
    const body = el('div', 'item-body'); const heading = el('div', 'item-heading');
    const button = el('button', 'lock-button', list(state.trip.locked_item_ids).includes(item.id) ? '已锁定' : '锁定');
    button.dataset.itemId = str(item.id);
    button.type = 'button'; button.setAttribute('aria-pressed', String(list(state.trip.locked_item_ids).includes(item.id)));
    button.setAttribute('aria-label', `${list(state.trip.locked_item_ids).includes(item.id) ? '解锁' : '锁定'}：${str(item.title)}`);
    button.addEventListener('click', () => toggleLock(item.id));
    heading.append(el('h4', '', item.title), button); body.append(heading);
    window.TravelPresentation.renderChange(body, currentChange);
    window.TravelPresentation.renderInline(body, window.TravelPresentation.itemDetails(currentPlan(), item, unavailableSources));
    if (!currentChange && (changed || preserved)) body.append(el('span', 'change-note', changed ? '这次调整' : '仍然保留'));
    const detail = window.TravelDayGuide.append(body, item, supplyMap().get(item.supply_id), experienceHelpers());
    const artifacts = window.TravelJourney.contextualArtifacts(currentPlan(), day, item.id, announce);
    if (artifacts) detail.append(artifacts);
    if (item.supply_id) detail.append(supplyLinks([item.supply_id]));
    row.append(time, body); return row;
  }
  function mapLoadStatus(result) {
    $('map').dataset.tileState = result.status;
    $('map-overlay').hidden = result.status === 'ready';
    $('map').inert = !$('map-overlay').hidden;
    $('map-load-label').textContent = result.message || '正在准备Google地图';
    $('map-reload').hidden = result.status === 'loading';
    $('map-reload').textContent = result.status === 'unconfigured' ? '重新检查地图配置' : '重试Google地图';
    $('map-note').textContent = result.status === 'ready'
      ? `Google Maps · 已定位 ${result.located || 0}/${result.total || 0} 处。地点标记不代表准确入场口；分段导航见下方。`
      : 'Google Maps尚未显示；日程仍可阅读，也可打开Google Maps官网。不回退到OSM地图。';
  }
  function refreshMapLayout() {
    if ($('map').getClientRects().length) window.TravelGoogleMaps.resize();
  }
  function renderMap(items, day) {
    const allDestinations = records(state.trip?.core_direction?.destinations);
    // Older plans may omit day.city. Only use a unique explicitly named city;
    // never send a later day's stops against the first cities of a long trip.
    const namedCities = allDestinations.filter(value => [value.name, value.search_name]
      .some(name => name && str(day?.title).includes(name)));
    const dayCity = day?.city || (namedCities.length === 1 ? namedCities[0].name : '');
    const currentCity = allDestinations.find(value => [value.name, value.search_name].includes(dayCity));
    const destinations = dayCity ? [{ name: currentCity?.search_name || dayCity }] : [];
    const city = dayCity;
    const url = window.TravelGoogleMaps.searchURL(city || currentPlan()?.title || '旅行目的地');
    $('map-open-external').href = url;
    // ↗ 字符退役：12px 描线箭头 icon（P0-10 #2）。
    const mapLabel = $('map-open-external');
    mapLabel.textContent = '在 Google Maps 打开 ';
    const mapGlyph = window.TravelIcons?.svg?.('external', 12);
    if (mapGlyph) mapLabel.append(mapGlyph);
    $('map-links').replaceChildren(navigationLink('在 Google Maps 查看目的地 ↗', url));
    $('route-summary').replaceChildren(el('h3', '', '今天，按这个顺序走'),
      el('p', 'field-hint', '打开各站Google地图选择步行、公交或驾车。不把旧驾车路网当作本次公共交通路线。'));
    const supplies = supplyMap();
    $('route-legs').replaceChildren(...items.map((item, index) => {
      const supply = supplies.get(item.supply_id); const query = `${supply?.name || item.title} ${supply?.city || city}`;
      const row = el('li'); row.append(navigationLink(`${index + 1}. ${item.title} ↗`, window.TravelGoogleMaps.searchURL(query)));
      if (item.transport) row.append(el('p', '', item.transport)); return row;
    }));
    if (!state.mapsReady) { mapLoadStatus({ status: 'loading', message: '正在读取Google地图配置…' }); return; }
    window.TravelGoogleMaps.render({ element: $('map'), items, supplies, destinations, onState: mapLoadStatus });
  }
  function renderReadiness(plan) {
    const readiness = plan.readiness || {};
    $('readiness').replaceChildren(el('h3', '', readiness.label || '方案草案 · 仍有事项待核验'),
      el('p', '', `待处理事项：${Number.isInteger(readiness.blocking_count) ? readiness.blocking_count : '未统计'}。本工作区没有执行外部预订，不能据此确认可出发。`));
    const warnings = el('ul'); list(readiness.warnings).forEach(text => warnings.append(el('li', '', text))); $('readiness').append(warnings);
    $('decisions').replaceChildren(...records(plan.decisions).map(item => {
      const row = el('article', 'decision-row'); const heading = el('h4');
      heading.append(el('span', 'status-tag', labels[item.status] || '待核验'), document.createTextNode(`${category[item.category] || '安排'} · ${str(item.choice)}`));
      row.append(heading, el('p', '', item.reason), supplyLinks(item.supply_ids)); return row;
    }));
    $('preparation').replaceChildren(...records(plan.preparation).map(item => {
      const row = el('article', 'preparation-row'); const heading = el('h4');
      heading.append(el('span', 'status-tag', labels[item.status] || '待核验'), document.createTextNode(str(item.task)));
      row.append(heading, el('p', '', item.why), supplyLinks(item.supply_ids)); return row;
    }));
    if (!$('decisions').children.length) $('decisions').append(el('p', 'empty-copy', '安排依据仍待补充。'));
    if (!$('preparation').children.length) $('preparation').append(el('p', 'empty-copy', '尚未列出准备事项，不代表无需准备。'));
  }
  function renderSupplies(plan) {
    $('supplies').replaceChildren(...records(plan.supplies).map(supply => {
      const reading = window.TravelExperience.placeReading(supply);
      const traveler = window.TravelPreparation.projectTraveler(supply, experienceHelpers().unavailableSources);
      const article = el('article', 'supply'); article.id = `supply-${str(supply.id)}`;
      const heading = el('div', 'supply-head'); const imageURL = traveler ? null : safeURL(supply.image_url);
      if (imageURL) {
        const image = el('img', 'supply-image'); image.src = imageURL; image.alt = str(supply.name); image.loading = 'lazy'; image.decoding = 'async'; image.referrerPolicy = 'no-referrer';
        image.addEventListener('error', () => { image.hidden = true; }, { once: true }); heading.append(image);
      }
      const name = el('div'); name.append(el('h3', '', reading.name), el('p', 'supply-provider', reading.localName || supply.provider || '来源未说明')); heading.append(name);
      article.append(heading, el('p', '', reading.description || '中文简介尚未返回，可查看来源原文。'));
      if (!window.TravelExperience.appendTraveler(article, supply, experienceHelpers())) {
        article.append(safeURL(supply.source_url) ? link('打开具体来源 ↗', supply.source_url, 'source-link')
          : el('p', '', `来源未核实或不可用（不可点击）：${window.TravelPreparation.safeURL(supply.source_url || supply.source_provenance?.source_ref) || '地址不可安全展示'}`));
      }
      window.TravelExperience.appendOriginal(article, '查看地点来源原文', reading.originalDescription);
      if (imageURL) article.append(el('p', 'image-credit', imageCredit(supply)));
      if (!traveler && supply.image_source_url) article.append(link('图片来源与许可 ↗', supply.image_source_url, 'source-link'));
      if (!traveler && supply.image_license_url) article.append(link('图片许可 ↗', supply.image_license_url, 'source-link'));
      if (supply.geo_source) {
        const geoURL = typeof supply.geo_source === 'object' ? supply.geo_source.url : supply.geo_source;
        if (safeURL(geoURL)) article.append(link('坐标来源 ↗', geoURL, 'source-link'));
        article.append(el('p', '', str(supply.geo_source.limits) || '坐标仅为地点锚点，不代表入口、营业状态或导航路线。'));
      }
      const checked = el('div', 'supply-check'); checked.append(el('span', '', traveler ? '旅行者来源 · 未核实' : verificationText(supply.verification)),
        el('span', '', `来源检查：${date(supply.checked_at)} · 非实时报价 / 库存未确认`));
      article.append(checked); return article;
    }));
    if (!$('supplies').children.length) $('supplies').append(el('p', 'empty-copy', '暂时没有可呈现的具体来源。模型建议不等于已核验供给。'));
  }
  function renderPlan(plan) {
    const focusedItem = document.activeElement?.dataset.itemId;
    const preparationFocus = document.activeElement?.dataset.preparationKey;
    const preparationOpen = [...($('preparation-pack')?.querySelectorAll('details[open]') || [])].map(item => item.dataset.preparationDetail);
    renderStory(plan); renderSignature(plan.signature || {}); renderUnderstanding(plan);
    renderReactions(plan); renderDays(plan); renderReadiness(plan); renderSupplies(plan);
    window.TravelJourney.render(plan, { supplyLinks, link, announce, correct: correctUnderstanding, carry: carryPreferences });
    window.TravelHandbook.render(plan, { revision: state.trip.revision, tripId: state.trip.id,
      draft: !state.trip.plan, helpers: experienceHelpers() });
    window.TravelPreparation.render($('handbook-prepare'), plan, {
      unavailableSources, busy: state.busy, running: tripRunning(), pending: state.preparing,
      focusKey: preparationFocus, expanded: preparationOpen, onPrepare: prepareTrip, onReport: reportPreparationAction
    });
    renderPlaceMedia();
    window.TravelChrome?.render(plan, { day: state.day });
    $('model-meta').textContent = state.trip.metrics?.model
      ? `本次任务模型：${str(state.trip.metrics.model)} · 模型解释是提案，来源核验单独列示。` : '模型解释是提案，来源核验单独列示。';
    if (focusedItem !== undefined) [...document.querySelectorAll('.lock-button')]
      .find(button => button.dataset.itemId === focusedItem)?.focus({ preventScroll: true });
  }
  function mutationBlocked() {
    const reason = !state.session || !state.trip ? '旅行尚未恢复，请重新连接后再操作。'
      : !maincore?.known || (maincore.enabled && maincore.client.state.snapshot?.trip?.id !== state.session.id) ? '旅行助手状态尚未读到，请重新连接后再操作。'
      : maincore.enabled && !maincore.modelReady ? '旅行助手尚未启用模型，输入与材料草稿仍保留。'
      : state.busy || maincore?.busy ? '另一次提交正在确认，请等待结果。'
      : tripRunning() ? '上一轮仍在处理，请等待结果。' : '';
    if (!reason) return false;
    state.mutationNotice = `本次操作未发送。${reason}`;
    notice(state.mutationNotice, true); announce(state.mutationNotice); return true;
  }
  async function mutate(suffix, body, successMessage) {
    if (mutationBlocked()) return false;
    if (maincore?.enabled && ['/prepare','/retry'].includes(suffix)) {
      return submitMaincore({feedback:body.feedback || '请根据已保存的目标、已答选择和实际进展继续准备，保留现有可用安排。',expected_revision:state.trip.revision});
    }
    if (maincore?.enabled && suffix === '/undo') {
      const revisions = list(maincore.client.state.snapshot?.revisions);
      const revision = revisions.find(value => value.kind === 'apply' && value.can_undo !== false
        && !revisions.some(other => other.undo_of === (value.revision_id || value.id)));
      if (!revision) { notice('当前没有可安全撤销的助手调整。'); return false; }
      await maincore.perform(() => maincore.undo(revision)); return true;
    }
    revisionReceipt?.dismiss();
    clearTimeout(state.pollTimer); revisionStage?.reset(); setBusy(true); const generation = ++state.generation;
    const binding = versionBinding();
    const proof = SINGLE_SESSION_MUTATIONS.includes(suffix) && state.trip?.capabilities?.single_session
      ? bindOperation(suffix, { ...body, ...binding }) : null;
    const envelope = proof ? { ...body, ...binding, request_id: proof.request_id } : body;
    try {
      const result = await api(tripPath(suffix), 'POST', envelope);
      if (generation !== state.generation) return false;
      if (!result.trip) throw new Error('服务未返回更新后的任务，请重新连接确认。');
      acceptTrip(result.trip); confirmOperation(); state.mutationNotice = ''; notice(successMessage); schedulePoll(); return true;
    } catch (error) {
      const code = error.code || error.error?.code;
      notice(error.status === 409 ? '方案已在其他页面更新，正在重新读取。你的调整文字保留，请看过最新版后再次提交。' : error.message, true);
      await syncTrip(true);
      state.mutationNotice = code === 'duplicate_request'
        ? '相同调整已经提交过，正在读取已保存的结果，不会重复执行。文字仍保留。'
        : error.status === 409
        ? '本次操作未被接受。已尝试读取最新方案，调整文字仍保留，请确认版本后重新提交。'
        : `${error.message} 本次提交未确认。已尝试重新读取任务，不会自动重复提交；再次提交同一内容时沿用本次请求编号。`;
      notice(state.mutationNotice, true);
      return false;
    } finally { setBusy(false); }
  }
  async function toggleLock(id) {
    const locked = new Set(list(state.trip.locked_item_ids));
    if (locked.has(id)) locked.delete(id); else locked.add(id);
    await mutate('/revise', { expected_revision: state.trip.revision, locked_item_ids: [...locked] },
      locked.has(id) ? '这项安排已请求锁定；后续调整会保留它。' : '这项安排已请求解锁。');
    [...document.querySelectorAll('.lock-button')].find(button => button.dataset.itemId === str(id))?.focus({ preventScroll: true });
  }
  async function sendMessage(body) {
    if (mutationBlocked()) return;
    revisionReceipt?.dismiss();
    clearTimeout(state.pollTimer); revisionStage?.reset(); setBusy(true); const generation = ++state.generation;
    state.messageContext = ''; $('message-response').replaceChildren(); $('message-response').hidden = true;
    revisionMaterials.status('正在理解这次输入；提问会直接回答，修改要求会进入修订。');
    const binding = versionBinding();
    const proof = state.trip?.capabilities?.single_session ? bindOperation('message', { ...body, ...binding }) : null;
    const envelope = proof ? { ...body, ...binding, request_id: proof.request_id } : body;
    try {
      const result = await api(tripPath('/message'), 'POST', envelope);
      if (generation !== state.generation) return;
      if (result.kind === 'revision') {
        if (!result.trip) throw new Error('服务未返回更新后的任务，请重新连接确认。');
        acceptTrip(result.trip); confirmOperation(); state.mutationNotice = '';
        if ($('feedback').value.trim() === body.feedback) $('feedback').value = '';
        revisionMaterials.reset('调整已提交。新版本准备好前，原方案保持可读。');
        notice('调整已提交。新版本准备好前，原方案保持可读。');
      } else if (['answer', 'clarify'].includes(result.kind)) {
        confirmOperation();
        window.TravelPresentation.renderMessage($('message-response'), result, intent => {
          let current;
          try { current = revisionMaterials.payload($('feedback').value.trim()); } catch { return; }
          const original = { ...body }; delete original.confirmed_intent;
          const next = { feedback: $('feedback').value.trim(), ...current, expected_revision: state.trip?.revision,
            locked_item_ids: list(state.trip?.locked_item_ids) };
          if (JSON.stringify(next) !== JSON.stringify(original)) {
            revisionMaterials.status('输入或方案已变化，请重新提交当前内容再确认。', true); return;
          }
          sendMessage({ ...body, confirmed_intent: intent });
        }, { link });
        state.messageContext = messageContext();
        state.messageLog = [...state.messageLog, { kind: result.kind, question: body.feedback, result }].slice(-20);
        renderQaHistory();
        revisionMaterials.status(result.material_notice || (result.kind === 'answer'
          ? '回答已显示，行程未改动。输入仍保留，便于继续追问。' : '请确认这次输入的意思，行程和材料尚未改动。'));
      } else throw new Error('服务未返回可识别的答复。输入与材料仍保留。');
      schedulePoll();
    } catch (error) {
      if (generation !== state.generation) return;
      await syncTrip(true);
      const code = error.code || error.error?.code;
      state.mutationNotice = code === 'duplicate_request'
        ? '相同调整已经提交过，正在读取已保存的结果，不会重复执行。文字与新增材料仍保留。'
        : error.status === 409
        ? '本次操作未被接受。已尝试读取最新方案，文字与新增材料仍保留，请确认版本后重新提交。'
        : `${error.message} 本次提交未确认。文字与新增材料仍保留，不会自动重复提交；再次提交同一内容时沿用本次请求编号。`;
      notice(state.mutationNotice, true); revisionMaterials.status(state.mutationNotice, true);
    } finally { if (generation === state.generation) setBusy(false); }
  }
  function messageContext() {
    return JSON.stringify([state.trip?.id, state.trip?.revision, state.trip?.profile_revision, state.trip?.traveler_profile, currentPlan(),
      [...unavailableSources].sort(), [...validatedSources].sort()]);
  }
  async function prepareTrip(action) {
    if (state.busy || !state.session || tripRunning()) return false;
    state.preparing = true;
    const body = { expected_revision: state.trip.revision };
    if (action) body.feedback = `请继续研究准备行动 ${action.id}「${action.title}」的未查清事项，补齐具体步骤、完成条件和来源；保持已有行程不变。`;
    try {
      return await mutate('/prepare', body, '查漏补缺已提交。只更新准备资料，现有日程保持不变；不会自动预订或付款。');
    } finally { state.preparing = false; renderControls(); }
  }
  async function reportPreparationAction(actionId, status) {
    const accepted = await mutate('/actions/report', { expected_revision: state.trip.revision, action_id: actionId, status },
      status === 'user_reported' ? '已记录你本人报告办好，未经渠道确认。需要时可恢复为待办。' : '已恢复为待办；没有取消任何外部订单。');
    if (accepted) announce(status === 'user_reported' ? '已记录本人报告，未经渠道确认。' : '已恢复为待办。');
    const focusKey = `${accepted && status === 'user_reported' ? 'reopen' : 'report'}-${actionId}`;
    [...($('preparation-pack')?.querySelectorAll('button') || [])].find(button => button.dataset.preparationKey === focusKey)?.focus({ preventScroll: true });
    return accepted;
  }
  function settleMaincoreDraft() {
    const snapshot=maincore?.client.state.snapshot;
    if (!snapshot || snapshot.trip?.id !== state.trip?.id || snapshot.trip?.revision !== state.trip?.revision) return;
    for (const pending of maincore.pending()) {
      const initial=pending.kind === 'start', composer=initial ? startMaterials : revisionMaterials;
      const input=$(initial ? 'brief' : 'feedback');
      let materials;
      try { materials=composer.payload(input.value.trim()); } catch { continue; }
      if (!window.TravelMaincore.draftOutcome(pending,snapshot,window.TravelMaincore.draftFingerprint(input.value,materials))) continue;
      input.value=''; composer.reset('本次方案已实际应用，已清空对应的文字与材料草稿。');
      maincore.clearPending(pending.runId);
      if (initial) {
        $('maincore-search').value=''; $('maincore-auto-apply').checked=false;
        maincore.client.saveDraft('studio-start-settings',{});
      }
    }
  }
  async function submitMaincore(body) {
    if (mutationBlocked()) return false;
    clearTimeout(state.pollTimer); setBusy(true);
    const generation=state.generation;
    revisionMaterials.status('正在提交本次输入，原方案与草稿保持可读。');
    try {
      await maincore.followup(body);
      if (generation !== state.generation) return false;
      await syncTrip(true);
      revisionMaterials.status('输入已接收。待对应方案实际应用后清空草稿；失败时保留原方案和本次输入。');
      schedulePoll(); return true;
    } catch (error) {
      if (generation === state.generation) {
        revisionMaterials.saveDraft?.(); revisionMaterials.status(error.message+' 本次输入与材料草稿仍保留。',true);
        notice(error.message,true);
      }
      return false;
    } finally { if (generation === state.generation) setBusy(false); }
  }
  async function createTrip(event) {
    event.preventDefault(); if (state.busy || maincore?.busy || startMaterials.preparing || !maincore?.known || (maincore.enabled ? !maincore.modelReady : !state.cap?.planner_available)) return;
    const brief = $('brief').value.trim(); let materials;
    try { materials = startMaterials.payload(brief); }
    catch (error) { $('start-material-drawer').open = true; startMaterials.status(error.message, true); $('links').focus(); return; }
    if (!brief && !startMaterials.hasMaterials(materials)) {
      startMaterials.status('放入一个行程文件、链接、文字或图片，就可以开始；也可以只写想法。', true);
      $('start-material-drawer').open = true; $('brief').focus(); return;
    }
    setBusy(true); notice('正在提交需求，等待服务接受任务…');
    try {
      const intake = { brief, ...materials };
      const ownedCreate = !maincore.enabled && state.cap?.single_session === true;
      const payload = ownedCreate ? await createWithProof(intake) : intake;
      const result = maincore.enabled ? await maincore.start(payload,{
        auto_apply:$('maincore-auto-apply').checked,
        public_search_entities:window.TravelAutopilot.parsePublicSearchEntities($('maincore-search').value)
      }) : await api('/api/agent/trips', 'POST', payload, null);
      if (!result.trip?.id || !result.token) throw new Error('服务没有返回完整任务访问凭证，无法安全恢复，请检查服务。');
      if (ownedCreate && result.token !== payload.client_token) throw new Error('服务返回的访问凭证与本次提交不一致，原提交凭证已保留。');
      revisionStage?.reset(); state.generation += 1; saveSession({ id: result.trip.id, token: result.token, isExample: state.exampleDraft }, result.trip);
      if (ownedCreate && !state.storageFailed) clearCreateProof(payload.client_token);
      if (ownedCreate && state.storageFailed) startMaterials.status('需求已接收，但访问凭证尚未保存到本机；原输入与提交凭证保留，请先保存恢复信息。', true);
      else if (!maincore.enabled) { $('brief').value = ''; startMaterials.reset('材料已提交，处理结果请查看材料收据。'); }
      else startMaterials.status('需求与材料已接收，草稿保留至本次方案实际应用；处理结果可在这趟旅行里继续查看。');
      notice(state.storageFailed ? '浏览器未允许保存任务访问凭证。当前页面可用，但关闭后无法自动恢复。' : '');
      acceptTrip(result.trip);
      if (maincore.enabled) await syncTrip(true);
      schedulePoll(); $('state-title').focus();
    } catch (error) {
      startMaterials.saveDraft?.();
      startMaterials.status('提交未确认，文件、文字、链接和图片草稿仍保留。请留意下方本机保存状态。', true);
      notice(`${error.message} 输入仍在此页面。${state.pendingCreate
        ? '原提交凭证已保留；相同需求重试会恢复原任务。' : '若提交结果不明确，请勿连续重复点击。'}`, true);
    }
    finally { setBusy(false); }
  }
  function resetWorkspace() {
    if (revisionMaterials.detachDraft?.() === false) return false;
    clearTimeout(state.pollTimer); revisionStage?.reset(); state.generation += 1;
    revisionReceipt?.reset();
    revisionElapsed?.reset();
    window.TravelGoogleMaps.reset(); window.TravelPlaceMedia.reset();
    Object.assign(state, { trip: null, renderedPlan: '', renderedPreview: '', day: 0, mutationNotice: '' });
    $('workspace').hidden = true; $('onboarding').hidden = false;
    window.TravelExperience.renderDiscovery({}, experienceHelpers());
    $('workspace-records').hidden = true; $('events').replaceChildren();
    $('feedback').value = ''; revisionMaterials.reset(); profileEditor?.reset();
    state.messageContext = ''; $('message-response').replaceChildren(); $('message-response').hidden = true;
    state.messageLog = [];
    if ($('qa-history')) $('qa-history').hidden = true;
    if ($('qa-history-list')) $('qa-history-list').replaceChildren();
    window.TravelDock?.show(false);
    $('intake-panel').removeAttribute('data-fingerprint'); $('intake-panel').removeAttribute('data-has-plan'); $('intake-details').open = true;
    $('workspace-records').open = false; $('event-details').open = false;
    setView(false);
    return true;
  }
  function newTrip() {
    if (state.busy || maincore?.busy) return false;
    if (resetWorkspace() === false) return false;
    if (!clearCreateProof()) return false;
    saveSession(null); state.exampleDraft = false; showRecent(false);
    renderControls();
    notice('之前的旅行仍保留，可以从「最近旅行」接着读或修改。'); $('brief').focus();
  }
  function carryPreferences(selected) {
    if (state.busy || tripRunning() || !selected.length) return;
    const notes = `来自上一趟的偏好，请结合新旅行重新确认：\n${selected.map(value => `- ${value}`).join('\n')}`;
    const draft = $('brief').value.trim();
    if (draft.length + notes.length + 2 > 6000) { notice('现有草稿加上偏好超过字数上限，请先精简草稿；没有覆盖原内容。'); return; }
    if (newTrip() === false) return;
    $('brief').value = `${draft}${draft ? '\n\n' : ''}${notes}`;
    notice('选中的偏好已放进新旅行草稿，尚未提交。补上这次的目的地与期待后再开始。');
    $('brief').focus();
  }
  function restoreSession(session) {
    if (state.busy || maincore?.busy) return;
    if (resetWorkspace() === false) return;
    saveSession(session); showRecent(false);
    $('onboarding').hidden = true; $('workspace').hidden = false;
    $('discovery').before($('task-state'));
    $('journey-preparation').hidden = true; $('journey-artifacts').hidden = true;
    $('plan-content').hidden = true; $('preview').hidden = true; $('feedback-section').hidden = true; $('demo-guide').hidden = true;
    $('state-title').textContent = '正在恢复这趟旅行'; $('state-copy').textContent = '从本地服务读取任务与方案，不重新生成。';
    $('trip-meta').textContent = session.title || '恢复旅行'; renderControls(); syncTrip();
  }
  function startDemo() {
    if (state.busy) return;
    if ($('brief').value.trim() && $('brief').value.trim() !== DEMO_BRIEF
      && !window.confirm('用阿布扎比示例替换尚未提交的旅行需求？其他材料不会清除。')) return;
    $('brief').value = DEMO_BRIEF; state.exampleDraft = true;
    renderControls();
    notice('已填入示例需求，尚未调用模型。你可以先改内容，再点击「按这个需求开始实时规划」。');
    $('brief').focus();
  }
  function forgetCurrentSession() {
    const id = state.session.id;
    const draftCleared = revisionMaterials.discardDraft?.(id) !== false;
    state.library.forgottenIds.push(id);
    state.library.sessions = state.library.sessions.filter(item => item.id !== id);
    if (resetWorkspace() === false) return;
    saveSession(null);
    if (!state.storageFailed) {
      try { if (JSON.parse(localStorage.getItem(SESSION_KEY))?.id === id) localStorage.removeItem(SESSION_KEY); }
      catch { /* Canonical v2 library takes precedence; never erase unrelated credentials. */ }
    }
    return draftCleared && !state.storageFailed;
  }
  async function deleteTrip() {
    if (!state.session || state.busy) return;
    if (!window.confirm('删除这趟任务及其服务端材料、方案历史？此操作无法撤销，不会取消任何外部订单。')) return;
    setBusy(true); clearTimeout(state.pollTimer); revisionStage?.reset(); ++state.generation;
    try {
      const result = await api(tripPath(), 'DELETE');
      if (!result.deleted) throw new Error('服务未确认删除，任务访问记录仍保留。');
      const cleared = forgetCurrentSession();
      notice(cleared ? '任务、材料和方案历史已从本地服务删除，浏览器访问记录与草稿已移除。'
        : '服务端任务已删除，但本机存储清理未成功。请检查浏览器存储；不能确认本机草稿与访问记录已移除。', !cleared); $('brief').focus();
    } catch (error) {
      notice(`${error.message} 尚未确认服务端删除。`, true);
      if ([401, 403, 404].includes(error.status) && window.confirm('无法访问这趟任务。仅移除此浏览器的访问记录？这不代表服务端数据已删除。')) {
        const cleared = forgetCurrentSession();
        notice(cleared ? '只移除了此浏览器的访问记录与草稿，未确认服务端数据删除。'
          : '本机存储清理未成功，无法确认访问记录或草稿已移除；未确认服务端数据删除。', !cleared);
      } else schedulePoll();
    } finally { setBusy(false); }
  }
  $('brief-form').addEventListener('submit', createTrip);
  ['brief', 'feedback'].forEach(id => $(id).addEventListener('keydown', event => {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && !event.isComposing) {
      event.preventDefault(); $(id === 'brief' ? 'brief-form' : 'feedback-form').requestSubmit();
    }
  }));
  async function submitFeedback(request = null) {
    const feedback = request ? request.feedback.trim() : $('feedback').value.trim();
    if (mutationBlocked()) return false;
    if (request && (request.trip_id !== state.trip.id || request.expected_revision !== state.trip.revision)) {
      revisionMaterials.status('方案已变化，请查看这次调整的最新结果后再操作。', true); return false;
    }
    if (revisionMaterials.preparing) {
      revisionMaterials.status('本次操作未发送，材料仍在读取，请稍后提交。', true); return false;
    }
    let materials;
    try { materials = revisionMaterials.payload(feedback); }
    catch (error) { $('revision-materials').open = true; revisionMaterials.status(error.message, true); $('revision-links').focus(); return false; }
    if (request && ($('feedback').value.trim() || revisionMaterials.hasMaterials(materials))) {
      revisionMaterials.status('已有未提交的文字或材料，请先提交或整理；这一步尚未发送，原内容完整保留。', true); $('feedback').focus(); return false;
    }
    if (!feedback && !revisionMaterials.hasMaterials(materials)) {
      revisionMaterials.status('还没有新增内容。可写调整意见，或补充行程文件、链接、订单文字和图片；不会发送空更新。'); return false;
    }
    if (request) $('feedback').value = feedback;
    revisionMaterials.saveDraft?.();
    if (maincore?.enabled) return submitMaincore({feedback,...materials,
      expected_revision:request ? request.expected_revision : state.trip.revision});
    await sendMessage({ feedback, ...materials, ...(request ? { confirmed_intent: 'revision' } : {}),
      expected_revision: request ? request.expected_revision : state.trip.revision,
      locked_item_ids: list(state.trip.locked_item_ids) });
    return true;
  }
  $('feedback-form').addEventListener('submit', async event => {
    event.preventDefault(); await submitFeedback();
  });
  $('undo').addEventListener('click', () => mutate('/undo', { expected_revision: state.trip.revision }, '已请求恢复上一个方案版本，不涉及外部订单。'));
  $('open-preparation').addEventListener('click', () => {
    window.TravelHandbook.selectView('prepare');
    const title = $('handbook-prepare-title');
    if (title) { title.tabIndex = -1; title.focus({ preventScroll: true }); title.scrollIntoView({ block: 'start', behavior: 'instant' }); }
  });
  $('retry').addEventListener('click', () => {
    const recovery = sessionRecovery();
    if (state.trip?.capabilities?.single_session && !recovery) return false;
    return mutate(recovery?.path || '/retry', { expected_revision: state.trip?.revision }, recovery?.message || '已提交继续研究请求。');
  });
  $('delete-trip').addEventListener('click', deleteTrip);
  $('start-demo').addEventListener('click', startDemo);
  $('try-demo-feedback').addEventListener('click', () => correctUnderstanding(DEMO_FEEDBACK));
  $('new-trip').addEventListener('click', newTrip);
  $('recent-toggle').addEventListener('click', () => showRecent($('recent-trips').hidden));
  $('recent-close').addEventListener('click', () => { showRecent(false); $('recent-toggle').focus(); });
  $('save-recovery').addEventListener('click', () => { if (persistLibrary()) {
    if (state.session?.token === state.pendingCreate?.client_token) clearCreateProof(state.session.token);
    notice('旅行访问凭证已保存到此浏览器。');
  } });
  $('reconnect').addEventListener('click', async () => { notice('正在重新连接…'); await capabilities(); if (state.session) await syncTrip(); });
  function setView(mapVisible) {
    $('itinerary-layout').classList.toggle('mobile-map', mapVisible);
    $('show-map').setAttribute('aria-pressed', String(mapVisible)); $('show-timeline').setAttribute('aria-pressed', String(!mapVisible));
    requestAnimationFrame(refreshMapLayout);
  }
  $('show-map').addEventListener('click', () => setView(true)); $('show-timeline').addEventListener('click', () => setView(false));
  $('map-reload').addEventListener('click', () => {
    window.TravelGoogleMaps.retry(); configureMaps();
  });
  window.addEventListener('online', () => { if (state.session) syncTrip(); else capabilities(); });
  window.addEventListener('resize', () => requestAnimationFrame(refreshMapLayout));
  window.addEventListener('beforeunload', event => {
    if (state.storageFailed && state.library.sessions.length) { event.preventDefault(); event.returnValue = ''; }
  });
  loadCreateProof(); loadLibrary();
  maincore = window.TravelMaincore.mount({doc:document,win:window,container:$('maincore-panel'),
    changed:() => renderControls(), updated:async () => { await syncTrip(true); schedulePoll(); }});
  maincore.select(state.session);
  startMaterials.restoreDraft?.('new-trip');
  const settings = maincore.client.state.saved.drafts['studio-start-settings'] || {};
  $('maincore-search').value = settings.search || ''; $('maincore-auto-apply').checked = settings.auto_apply === true;
  ['maincore-search','maincore-auto-apply'].forEach(id => $(id).addEventListener('change',() => {
    maincore.client.saveDraft('studio-start-settings',{search:$('maincore-search').value,auto_apply:$('maincore-auto-apply').checked});
  }));
  const locationURL = new URL(location.href); const autoDemo = locationURL.searchParams.get('demo') === '1';
  const requested = state.library.sessions.find(session => session.id === locationURL.searchParams.get('trip'));
  if (requested) state.session = requested;
  if (autoDemo) { locationURL.searchParams.delete('demo'); history.replaceState(null, '', locationURL); }
  if (state.session) restoreSession(state.session);
  else if (autoDemo) startDemo();
  capabilities(); configureMaps();
})();
