'use strict';

(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.TravelSessionComparison = api;
})(typeof window === 'undefined' ? null : window, function () {
  const libraryKey = 'atom-travel-studio-library-v2';
  function ownedSession(storage, requestedId) {
    try {
      const library = JSON.parse(storage.getItem(libraryKey));
      const valid = session => session && /^[0-9a-f]{32}$/.test(session.id)
        && typeof session.token === 'string' && /^[\x21-\x7E]{20,500}$/.test(session.token);
      if (Array.isArray(library?.sessions)) {
        return library.sessions.find(session => valid(session)
          && session.id === (requestedId || library.activeId)
          && !(library.forgottenIds || []).includes(session.id)) || null;
      }
      const legacy = JSON.parse(storage.getItem('atom-travel-studio-session-v1'));
      return valid(legacy) && (!requestedId || requestedId === legacy.id) ? legacy : null;
    } catch (_) { return null; }
  }
  function comparisonTransport(transport, session, caseId, binding) {
    const base = `/api/agent/trips/${session.id}/comparisons/${caseId}`;
    return async (path, options = {}) => {
      const action = new URL(path, 'https://local.invalid').pathname.replace('/api/comparison', '');
      if (!['', '/generate', '/select', '/adjust'].includes(action)) throw new Error('Unsupported comparison action');
      const body = options.body ? JSON.stringify({ ...JSON.parse(options.body), ...binding }) : undefined;
      return transport(base + action, { ...options, headers: { ...options.headers,
        Authorization: `Bearer ${session.token}` }, ...(body ? { body } : {}) });
    };
  }
  function errorCopy(code) {
    return code === 'budget_unavailable' ? '尚未读取到已授权的调用额度。请返回旅行页确认本轮额度后再比较。'
      : code === 'BUDGET_EXHAUSTED' ? '这趟旅行本轮的调用额度已用完。已有比较与手册仍保留。'
        : code === 'revision_conflict' ? '旅行手册已更新。请刷新此页，使用当前版本重新比较。'
          : code === 'REQUEST_IN_PROGRESS' ? '比较仍在处理中，请稍后刷新此页读取结果；不会自动重复提交。'
            : code === 'not_found' ? '无法读取这趟旅行，请返回旅行页重新选择。'
              : '这次比较尚未确认完成。输入已保留，请先刷新读取已有结果，再决定是否重试。';
  }
  async function mount({ container, document: doc = globalThis.document,
    fetch: transport = globalThis.fetch.bind(globalThis), storage = globalThis.localStorage,
    location: locationRef = globalThis.location, history: historyRef = globalThis.history,
    comparison = globalThis.TravelComparison }) {
    if (!container) return;
    let capability;
    try {
      const response = await transport('/api/agent/capabilities', { method: 'GET', cache: 'no-store', credentials: 'same-origin' });
      if (response.status === 404) return comparison.mount({ container, document: doc, fetch: transport, location: locationRef, history: historyRef });
      if (!response.ok) throw new Error('capabilities unavailable');
      capability = await response.json();
      if (!capability.single_session) return comparison.mount({ container, document: doc, fetch: transport, location: locationRef, history: historyRef });
    } catch (_) { container.hidden = false; container.textContent = '暂时无法读取服务状态，请刷新后再试。'; return; }
    doc.body.classList.add('comparison-mode'); container.hidden = false;
    ['onboarding', 'workspace', 'recent-trips', 'notice', 'storage-warning'].forEach(id => {
      const node = doc.getElementById(id); if (node) node.hidden = true;
    });
    const create = (tag, text = '') => { const node = doc.createElement(tag); node.textContent = text; return node; };
    const back = create('a', '← 返回旅行工作台'); back.href = './studio.html'; back.className = 'text-button';
    const contextBar = create('p'); contextBar.className = 'comparison-context'; contextBar.hidden = true;
    const note = create('p', '比较研究与模型调用共用这趟旅行的本轮额度。选择只保存在比较卡中，不会自动修改正式行程。');
    const shell = create('section'); shell.className = 'comparison-empty';
    const status = create('p'); status.setAttribute('role', 'status');
    const cardHost = create('div');
    container.replaceChildren(back, contextBar, note, shell, status, cardHost);
    const offerRefresh = message => {
      status.replaceChildren();
      status.textContent = '';
      status.append(create('span', message), ' ');
      const refresh = create('button', '刷新读取最新结果'); refresh.type = 'button'; refresh.className = 'secondary-button';
      refresh.addEventListener('click', () => locationRef.reload?.());
      status.append(refresh);
    };
    const text = value => typeof value === 'string' || typeof value === 'number' ? String(value).trim() : '';
    const url = new URL(locationRef.href);
    const session = ownedSession(storage, url.searchParams.get('trip_id'));
    if (!session) { shell.append(create('h1', '先选择一趟旅行'), create('p', '返回旅行页创建或打开已有旅行，就能结合同行人和偏好比较两个实际选择。')); return; }
    back.href = `./studio.html?trip=${session.id}`;
    const base = `/api/agent/trips/${session.id}/comparisons`;
    const read = async (path, body) => {
      const response = await transport(path, { method: body ? 'POST' : 'GET', cache: 'no-store', credentials: 'same-origin',
        headers: { Accept: 'application/json', Authorization: `Bearer ${session.token}`,
          ...(body ? { 'Content-Type': 'application/json' } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}) });
      const value = await response.json();
      if (!response.ok) throw Object.assign(new Error('comparison request failed'), { code: value.error?.code });
      return value;
    };
    let context;
    try { context = await read(base); }
    catch (error) { offerRefresh(errorCopy(error.code)); return; }
    const tripState = await read(`/api/agent/trips/${session.id}`).catch(() => null);
    if (tripState) {
      const title = text(tripState.plan?.title) || '当前旅行';
      const version = Number.isInteger(tripState.document?.version) ? `手册版本 v${tripState.document.version}` : '手册版本读取中';
      contextBar.textContent = [title, version].join(' · ');
      contextBar.hidden = false;
    }
    const openCard = (caseId, binding) => {
      shell.hidden = true; status.textContent = '';
      url.searchParams.set('trip_id', session.id); url.searchParams.set('case_id', caseId);
      historyRef.replaceState(null, '', url);
      const fresh = create('a', '比较其他两个选择');
      const freshURL = new URL(url); freshURL.searchParams.delete('case_id'); fresh.href = freshURL.href;
      shell.before(fresh);
      return comparison.mount({ container: cardHost, document: doc,
        fetch: comparisonTransport(transport, session, caseId, binding), location: locationRef, history: historyRef });
    };
    const existingId = url.searchParams.get('case_id');
    if (existingId && context.comparisons.some(item => item.id === existingId)) {
      const existing = await read(`${base}/${existingId}`).catch(error => { offerRefresh(errorCopy(error.code)); });
      if (existing) return openCard(existingId, { expected_revision: existing.expected_revision, expected_hash: existing.expected_hash });
      return;
    }
    shell.append(create('h1', '这次想比较哪两个选择？'));
    const form = create('form'), first = create('input'), second = create('input'), question = create('textarea');
    [[first, '选择一', '例如：苏州博物馆'], [second, '选择二', '例如：拙政园'], [question, '最在意什么（可选）', '例如：带老人，不想走太多路']].forEach(([input, label, placeholder]) => {
      const wrapper = create('label', label); input.placeholder = placeholder;
      input.required = input !== question; input.maxLength = input === question ? 2000 : 200; wrapper.append(input); form.append(wrapper);
    });
    const submit = create('button', '比较这两个选择'); submit.type = 'submit'; submit.className = 'primary-button'; form.append(submit); shell.append(form);
    const pendingKey = `atom-travel-comparison-pending-${session.id}`;
    let pending;
    try { pending = JSON.parse(storage.getItem(pendingKey)); } catch (_) { /* No replayable draft. */ }
    if (pending?.options?.length === 2) { first.value = pending.options[0]; second.value = pending.options[1]; question.value = pending.question || ''; }
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (submit.disabled) return;
      const input = { options: [first.value.trim(), second.value.trim()], question: question.value.trim(),
        expected_revision: context.expected_revision, expected_hash: context.expected_hash };
      if (!input.options.every(Boolean) || input.options[0] === input.options[1]) { status.textContent = '请填写两个不同的实际地点或方案名称。'; return; }
      const { request_id: previousId, ...previous } = pending || {};
      pending = { ...input, request_id: JSON.stringify(previous) === JSON.stringify(input) && previousId
        ? previousId : globalThis.crypto.randomUUID() };
      try { storage.setItem(pendingKey, JSON.stringify(pending)); }
      catch (_) { status.textContent = '浏览器无法保存这次请求，请恢复本地存储后再比较，避免断线后重复提交。'; return; }
      submit.disabled = true; status.textContent = '正在结合这趟旅行比较两个选择…';
      try {
        const result = await read(base, pending);
        try { storage.removeItem(pendingKey); } catch (_) { /* The server receipt is already durable. */ }
        openCard(result.case_id, { expected_revision: result.expected_revision, expected_hash: result.expected_hash });
      } catch (error) { offerRefresh(errorCopy(error.code)); }
      finally { submit.disabled = false; }
    });
    for (const item of context.comparisons) {
      const link = create('a', `查看已有比较：${item.title}`), savedURL = new URL(url);
      savedURL.searchParams.set('trip_id', session.id); savedURL.searchParams.set('case_id', item.id); link.href = savedURL.href;
      const row = create('p'); row.append(link); shell.append(row);
    }
  }
  return { mount, ownedSession, comparisonTransport, errorCopy };
});
