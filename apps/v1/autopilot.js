(function (root) {
  'use strict';
  const LIBRARY = 'atom-travel-studio-library-v2';
  const ACTIVE = 'atom-travel-autopilot-v1';
  const BASE = '/api/agent/autopilot';
  const list = value => Array.isArray(value) ? value : [];
  const text = value => typeof value === 'string' ? value : '';
  const segment = value => encodeURIComponent(value);
  const labels = {pending:'待处理', queued:'等待开始', running:'正在处理', in_progress:'正在处理',
    in_flight:'正在处理', succeeded:'已完成', completed:'本轮已结束', failed:'处理失败', error:'处理失败',
    unknown:'结果待核实', interrupted:'已中断', blocked:'暂时受阻', waiting:'等待条件', active:'持续推进中',
    cancelled:'已停止', invalidated:'已失效', revoked:'已撤回', proposed:'待你确认', applied:'已应用',
    undone:'已撤销', claimed:'正在检查', answered:'已回答', ready_for_user:'请查看结果', partial:'部分完成',
    waiting_for_input:'等待你的回答', waiting_for_job:'等待后续检查', complete:'助手已交付，待你确认',
    notrun:'尚未运行', modeldisabled:'助手尚未启用', conflict:'有冲突，尚未修改', expired:'已过期',
    available:'可查看', read:'已读取', active_source:'可查看', retracted:'已撤回', withdrawn:'已撤回', unavailable:'无法读取', rejected:'未应用'};
  const toolLabels = {read_trip:'查看当前旅行', read_goal:'查看目标', search_sources:'查找资料',
    inspect_materials:'核对材料', propose_revision:'准备调整提案', apply_revision:'应用行程调整',
    undo_revision:'撤销指定调整', request_input:'整理待确认问题', update_work:'更新工作进展',
    schedule_check:'安排后续检查', remember:'记录本趟理解', recall:'查看本趟记录',
    assess_uncertainty:'核对不确定信息', read_workspace:'查看准备进度', read_source:'阅读材料',
    research_fact:'核对事实来源', check_constraints:'检查旅行约束', retrieve_memory:'查看本趟理解',
    propose_memory:'整理本趟理解', delegate:'整理证据与反例'};
  const errors = {MODEL_EXECUTION_DISABLED:'助手尚未启用，新的规划与研究不会运行。草稿已保留。',
    PUBLIC_RESEARCH_SCOPE_REQUIRED:'请补充公开查询地点或使用已有材料。',
    request_timeout:'本轮连接超时，已有进展保留，本次尚未完成。',
    VERSION_CONFLICT:'行程已更新，这次操作尚未应用。请刷新进展后查看最新提案。',
    UNDO_CONFLICT:'这次调整暂时无法安全撤销，当前行程已保留。',
    GOAL_VERSION_CONFLICT:'目标状态已变化，请刷新进展后再操作。',
    SOURCE_UNAVAILABLE:'引用的材料已不可用，请补充有效材料。',
    STALE_DEPENDENCIES:'提案依赖的材料或条件已变化，这次修改未应用。请让助手基于最新信息重新准备。',
    STALE_SOURCE_RESULT:'材料已变化，旧结论需要重新核对。',
    STALE_TOOL_CONTEXT:'旅行信息已变化，需要重新核对后继续。',
    CAPABILITY_UNAVAILABLE:'所需的资料获取能力暂不可用，相关事实尚未核实。',
    EXECUTION_ERROR:'本轮处理遇到问题，已有进展保留，请补充信息或稍后继续。',
    FINAL_EVIDENCE_MISSING:'这轮结论缺少可核对的证据，处理已停止，不能视为完成。',
    BUDGET_EXCEEDED:'本轮处理已到授权上限，已保存已有进展。',
    GOAL_NOT_ACTIVE:'这个目标已结束或停止，请建立新的目标继续。'};
  const status = value => labels[value] || '状态待核实';
  function parsePublicSearchEntities(input = '') {
    const entities = text(input).split(/[,，]/).map(value => value.trim().normalize('NFC')).filter(Boolean);
    if (entities.length > 32) throw new Error('公开查询范围最多填写 32 个地点，请修改后提交。');
    if (entities.some(value => Array.from(value).length > 200)) throw new Error('每个公开查询地点不能超过 200 字，请修改后提交。');
    if (new Set(entities.map(value => value.toLowerCase())).size !== entities.length) throw new Error('公开查询地点有重复，请删除重复项后提交。');
    return entities;
  }
  function publicSearchScopeLabel(entities) {
    const values = list(entities).filter(value => typeof value === 'string' && value);
    return values.length ? '公开查询范围：' + values.join('、') : '公开查询范围：未填写；仍可处理已有材料。';
  }
  function fixtureNotice(capabilities) {
    if (!capabilities?.fixture_mode) return '';
    return '工程联调：模型为离线夹具' + (text(capabilities.fixture_notice) ? '。' + capabilities.fixture_notice : '，结果仅用于工程检查。');
  }
  function safeURL(value) {
    try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null; }
    catch { return null; }
  }
  function readable(value) {
    if (typeof value === 'string' || typeof value === 'number') return String(value);
    if (typeof value === 'boolean') return value ? '是' : '否';
    if (Array.isArray(value)) return value.map(readable).filter(Boolean).join('；');
    if (value && typeof value === 'object') return text(value.title) || text(value.label) || text(value.name)
      || text(value.task) || text(value.description) || text(value.summary) || text(value.message) || text(value.content);
    return '';
  }
  function userIssue(value) {
    const message = readable(value);
    return errors[message] || (/^[A-Z][A-Z0-9_]+$/.test(message) ? '这一步暂未完成，已有结果保留，请查看实际操作与待办。' : message);
  }
  function receiptSummary(receipt) {
    if (!receipt) return '';
    if (receipt.status === 'rejected') return errors[receipt.error_code] || '这次修改未应用，当前行程已保留。';
    if (receipt.status === 'succeeded' && receipt.result?.revision_id) return receipt.result.kind === 'undo'
      ? '已撤销你指定的那次调整，下面保留了具体变更回执。' : '这份提案已应用到当前行程，下面是具体变更回执。';
    return '这次操作的结果尚待核实，请查看实际操作记录。';
  }
  function changeValue(entry) {
    if (entry && typeof entry === 'object' && 'exists' in entry) {
      if (!entry.exists) return '无此安排';
      return entry.value == null ? '暂无安排' : readable(entry.value);
    }
    return entry == null ? '暂无安排' : readable(entry);
  }
  function undoAvailability(revision) {
    if (revision.can_undo !== false) return {available:true,notice:''};
    return {available:false,notice:revision.undo_unavailable_reason === 'NO_PREVIOUS_PLAN'
      ? '这是首份方案，可提出新的调整' : '这次调整目前不可撤销，当前行程已保留。'};
  }
  function questionOptions(doc, options, input, saveDraft, blocked = () => false) {
    if (!Array.isArray(options) || options.length < 2 || options.length > 3
      || options.some(value => typeof value !== 'string' || !value.trim() || Array.from(value).length > 200)) return null;
    const group = doc.createElement('div'); group.className = 'actions';
    group.setAttribute('role', 'group'); group.setAttribute('aria-label', '选一个回答，也可继续补充');
    options.forEach(option => {
      const button = doc.createElement('button'); button.type = 'button'; button.className = 'secondary';
      button.setAttribute('data-mutation', 'true'); button.disabled = blocked();
      button.textContent = option;
      button.addEventListener('click', () => {
        if (blocked()) return;
        const previous = options.find(value => input.value === value)
          || [...options].sort((a, b) => b.length - a.length).find(value => input.value.startsWith(value + '\n'));
        const note = previous ? input.value.slice(previous.length + 1) : input.value;
        const value = [option, note].filter(Boolean).join('\n');
        if (input.maxLength > 0 && value.length > input.maxLength) {
          input.setCustomValidity?.('回答过长，请先精简补充文字再选择。'); input.reportValidity?.(); return;
        }
        input.setCustomValidity?.(''); input.value = value; saveDraft(value); input.focus?.();
      });
      group.append(button);
    });
    return group;
  }
  function secureToken() {
    return Array.from(globalThis.crypto.getRandomValues(new Uint8Array(32)), b => b.toString(16).padStart(2,'0')).join('');
  }
  async function readTextFile(file, previous) {
    if (!/\.txt$/i.test(file.name)) throw new Error('目前支持 TXT。文件未导入，原草稿已保留。');
    if (file.size > 100000) throw new Error('文件过大。请选取相关正文，原草稿已保留。');
    const combined = [previous,await file.text()].filter(Boolean).join('\n\n');
    if (combined.length > 20000) throw new Error('合并后正文超过 20000 字，原草稿已保留。');
    return combined;
  }
  function createClient({fetch: fetcher, storage, uuid, token = secureToken, changed = () => {}}) {
    const state = {library:{sessions:[],activeId:null,forgottenIds:[]}, saved:{version:1,drafts:{},pending:{}},
      session:null, snapshot:null, capabilities:null, storageFailed:false, error:'', busy:false, epoch:0, readSequence:0};
    function emit() { changed(state); }
    function persist() {
      try {
        const stored = JSON.parse(storage.getItem(LIBRARY) || 'null') || {};
        const forgotten = new Set([...list(stored.forgottenIds), ...list(state.library.forgottenIds)]);
        const merged = new Map([...list(stored.sessions), ...state.library.sessions].map(s => [s.id, s]));
        state.library.sessions = [...merged.values()].filter(s => !forgotten.has(s.id));
        state.library.forgottenIds = [...forgotten];
        storage.setItem(LIBRARY, JSON.stringify(state.library));
        storage.setItem(ACTIVE, JSON.stringify(state.saved)); state.storageFailed = false;
      } catch { state.storageFailed = true; }
      emit(); return !state.storageFailed;
    }
    function restore(tripId) {
      try {
        const saved = JSON.parse(storage.getItem(ACTIVE) || 'null');
        const library = JSON.parse(storage.getItem(LIBRARY) || 'null');
        if (saved?.version === 1) state.saved = {...saved,drafts:saved.drafts || {},pending:saved.pending || {}};
        if (library) state.library = {...state.library,...library,sessions:list(library.sessions)
          .filter(s => s && typeof s.id === 'string' && typeof s.token === 'string'
            && !list(library.forgottenIds).includes(s.id))};
      } catch { state.storageFailed = true; }
      const id = tripId || state.saved.tripId || state.library.activeId;
      state.session = state.library.sessions.find(s => s.id === id) || null;
      emit(); return state.session;
    }
    async function request(path, body, session = state.session) {
      const headers = {'Accept':'application/json'};
      if (session?.token) headers.Authorization = `Bearer ${session.token}`;
      if (body) headers['Content-Type'] = 'application/json';
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 30000);
      let response;
      try {
        response = await fetcher(BASE + path, {method:body ? 'POST' : 'GET', headers,
          ...(body ? {body:JSON.stringify(body)} : {}), cache:'no-store', signal:controller.signal});
      } catch (error) {
        if (error.name === 'AbortError') throw new Error('连接超时，提交结果尚未确定。草稿已保留，请先刷新进展核对。');
        throw error;
      } finally { clearTimeout(timeout); }
      let data;
      try { data = await response.json(); } catch { throw new Error('服务返回无法读取。请刷新进展核对，草稿仍保留。'); }
      if (!response.ok) {
        const detail = data.error || data;
        const message = detail.message_zh || detail.message;
        const error = new Error(detail.code === 'PUBLIC_RESEARCH_SCOPE_REQUIRED' ? errors[detail.code]
          : message && message !== detail.code ? message : errors[detail.code] || '请求未能完成，草稿仍保留。请刷新进展核对。');
        error.code = detail.code; throw error;
      }
      return data;
    }
    function saveDraft(key, value) { state.saved.drafts[key] = value; persist(); }
    function attach(session, goalId) {
      state.epoch++; state.session = session; state.snapshot = null;
      state.saved.tripId = session?.id || null; state.saved.activeGoal = goalId || null;
      state.library.activeId = session?.id || null; persist();
    }
    async function refresh({clearError = true} = {}) {
      const epoch = state.epoch;
      const sequence = ++state.readSequence;
      const data = state.session ? await request(`/trips/${segment(state.session.id)}/state`)
        : await request('/capabilities', null, null);
      if (epoch !== state.epoch || sequence !== state.readSequence) return null;
      if (state.session) {
        state.snapshot = data; state.capabilities = data.capabilities || state.capabilities;
        const hadGoal = Boolean(state.saved.activeGoal);
        const goal = list(data.goals).find(g => g.id === state.saved.activeGoal)
          || list(data.goals).find(g => g.status === 'active') || list(data.goals)[0];
        state.saved.activeGoal = goal?.id || null;
        const session = state.library.sessions.find(s => s.id === state.session.id);
        if (session && data.trip?.plan?.title) session.title = data.trip.plan.title;
        if (!hadGoal && state.saved.activeGoal) {
          try {
            const latest = JSON.parse(storage.getItem(ACTIVE) || 'null');
            if (!latest?.activeGoal && (!latest?.tripId || latest.tripId === state.session.id)) persist();
          } catch { state.storageFailed = true; }
        }
      } else state.capabilities = data;
      if (clearError) state.error = '';
      emit(); return data;
    }
    async function mutate(path, body, {newTrip = false, clearDraft} = {}) {
      if (state.busy) return null;
      state.busy = true; state.error = ''; emit();
      const key = JSON.stringify([state.session?.id || '', path, body]);
      const requestId = state.saved.pending[key] || uuid();
      state.saved.pending[key] = requestId;
      state.saved.uncertain ||= {};
      state.saved.uncertain[requestId] = true;
      try {
        let clientToken;
        if (newTrip) {
          state.saved.intakeTokens ||= {};
          clientToken = state.saved.intakeTokens[requestId] ||= token();
        }
        persist();
        const data = await request(path, {...body, request_id:requestId,...(newTrip ? {client_token:clientToken} : {})}, newTrip ? null : state.session);
        if (newTrip) {
          if (!data.trip?.id || !data.token) throw new Error('旅行已提交，但恢复凭证不完整。请保留当前页面。');
          const session = {id:data.trip.id,token:data.token,title:data.trip.plan?.title || body.brief || '新的旅行',createdAt:data.trip.created_at};
          state.library.sessions = [session,...state.library.sessions.filter(s => s.id !== session.id)];
          attach(session, data.goal?.id);
          if (!state.storageFailed) delete state.saved.intakeTokens[requestId];
        } else if (data.goal) state.saved.activeGoal = data.goal.id;
        delete state.saved.pending[key];
        delete state.saved.uncertain[requestId];
        if (clearDraft && data.receipt?.status !== 'rejected') delete state.saved.drafts[clearDraft];
        persist();
        try { await refresh(); } catch { state.error = '请求已接收，但进展读取失败。请刷新进展，勿重复提交。'; }
        if (data.receipt?.status === 'rejected') {
          const error = new Error(errors[data.receipt.error_code] || '这次修改未应用，当前行程已保留。请刷新进展核对最新条件。');
          error.code = data.receipt.error_code || 'REJECTED'; throw error;
        }
        return data;
      } catch (error) {
        state.error = error.message; state.errorCode = error.code;
        if (error.code) delete state.saved.uncertain[requestId];
        persist(); throw error;
      }
      finally { state.busy = false; emit(); }
    }
    return {state, restore, persist, refresh, mutate, attach, saveDraft};
  }
  function mount(doc, win) {
    const $ = id => doc.getElementById(id);
    const el = (tag, content, cls) => { const n = doc.createElement(tag); if (content) n.textContent = content; if (cls) n.className = cls; return n; };
    let mode = 'new', editingGoal = false, reading = false, timer, lastSnapshot, lastQuestions, noticeOverride = '';
    let storage;
    try { storage = win.localStorage; } catch {
      storage = {getItem:() => { throw new Error('storage unavailable'); },setItem:() => { throw new Error('storage unavailable'); }};
    }
    const client = createClient({fetch:win.fetch.bind(win),storage,uuid:() => win.crypto.randomUUID(),changed:render});
    const s = client.state;
    function goal() { return list(s.snapshot?.goals).find(g => g.id === s.saved.activeGoal); }
    function tripPath(suffix) { return `/trips/${segment(s.session.id)}${suffix}`; }
    function draftKey() { return `followup:${s.session?.id || 'new'}`; }
    function goalDraftKey() { return `goal:${mode}:${mode === 'new' ? '' : s.session?.id || ''}`; }
    function followupKind() {
      $('material-file-wrap').hidden = $('followup-kind').value !== 'materials';
      $('followup-hint').textContent = $('followup-kind').value === 'corrections'
        ? '仅纠正本趟旅行的理解，不会设为永久偏好。' : '补充后，助手将根据当前旅行继续处理。';
    }
    function restoreFollowup() {
      $('followup').value = s.saved.drafts[draftKey()] || '';
      $('followup-kind').value = s.saved.drafts[draftKey()+':kind'] || 'events'; followupKind();
    }
    function button(label, action) {
      const node = el('button', label); node.type = 'button'; node.dataset.mutation = 'true';
      node.disabled = s.busy; node.addEventListener('click', () => perform(action)); return node;
    }
    function row(title, state) {
      const node = el('article', null, 'row'), head = el('div', null, 'row-heading');
      head.append(el('h3', title), el('span', status(state), 'badge')); node.append(head); return node;
    }
    function replace(id, nodes, empty) { $(id).replaceChildren(...(nodes.length ? nodes : [el('p', empty, 'empty')])); }
    function showError(error) { s.error = error.message || String(error); render(); }
    async function perform(action) {
      try { const data = await action(); if (data) noticeOverride = '已读取最新进展。关闭后重开可继续查看。'; }
      catch (error) { showError(error); }
      render(); schedule();
    }
    function render() {
      if (!s) return;
      $('storage-warning').hidden = !s.storageFailed;
      $('error').hidden = !s.error; $('error').textContent = s.error;
      const caps = s.capabilities;
      const fixture = fixtureNotice(caps);
      $('fixture-notice').hidden = !fixture; $('fixture-notice').textContent = fixture;
      $('notice').textContent = caps?.enabled === false ? '旅行助手尚未开放。你仍可保留草稿、查看已有旅行。'
        : caps?.model_enabled === false ? '助手尚未启用，新的规划与研究不会运行。已有结果仍可查看；草稿会保留。'
        : Object.keys(s.saved.uncertain || {}).length ? '有提交结果尚未确定，草稿已保留。请先刷新进展核对，再决定是否重新提交。'
        : noticeOverride || (s.session ? '已连接这趟旅行。关闭后重开会恢复进展，不会重复提交。' : '无需上传材料，直接说出你的旅行目标。');
      $('handbook').href = './studio.html' + (s.session ? `?trip=${segment(s.session.id)}` : '');
      renderSessions();
      doc.querySelectorAll('[data-mutation], form button[type=submit], form textarea, form input, form select, #confirm-goal, #cancel-goal, #new-goal, [data-mode], #sessions').forEach(n => { n.disabled = s.busy || reading; });
      $('start').disabled = s.busy || reading || (mode !== 'new' && (!s.session || !s.snapshot?.trip));
      const current = goal();
      $('goal-form').hidden = mode !== 'new' && !!current && !editingGoal;
      $('new-goal').hidden = mode === 'new' || !current || editingGoal;
      $('goal-panel').hidden = !current; $('followup-panel').hidden = !s.session || !current || current.status !== 'active';
      if (current) {
        $('goal-title').textContent = current.objective;
        $('goal-status').textContent = current.status === 'completed' ? '你已确认达成' : status(current.status);
        $('goal-policy').textContent = current.constraints?.auto_apply === true ? '可撤销调整获准自动应用。' : '调整先形成提案，由你确认应用。';
        $('goal-search-scope').textContent = publicSearchScopeLabel(current.constraints?.public_search_entities);
        $('confirm-goal').disabled = s.busy || current.status !== 'active'; $('cancel-goal').disabled = s.busy || current.status !== 'active';
      }
      const snapshotKey = JSON.stringify([s.session?.id,s.saved.activeGoal,s.snapshot?.trip?.revision,
        list(s.snapshot?.runs).map(r => [r.id,r.status,r.result]),s.snapshot?.proposals,s.snapshot?.revisions,
        s.snapshot?.workspace,s.snapshot?.operations,s.snapshot?.sources,s.snapshot?.trip?.material_sources,s.snapshot?.trip?.materials]);
      if (snapshotKey !== lastSnapshot) { lastSnapshot = snapshotKey; renderSnapshot(); }
      renderBudget();
    }
    function renderSessions() {
      const signature = JSON.stringify(s.library.sessions.map(v => [v.id,v.title]));
      if ($('sessions').dataset.signature !== signature) {
        $('sessions').replaceChildren(new win.Option('选择旅行', ''), ...s.library.sessions.map(v => new win.Option(v.title || '未命名旅行', v.id)));
        $('sessions').dataset.signature = signature;
      }
      $('sessions').value = s.session?.id || '';
    }
    function renderBudget() {
      const current = goal(), run = list(s.snapshot?.runs).find(r => r.goal_id === current?.id);
      const limits = run?.limits || s.capabilities?.run_limits;
      const allowance = current?.constraints?.background_check_allowance;
      const future = allowance?.limits || s.capabilities?.future_run_limits;
      const nodes = [];
      const describe = (label, value) => {
        if (!value) return;
        const parts = [];
        if (Number.isFinite(value.model_calls)) parts.push(`最多 ${value.model_calls} 次分析`);
        if (Number.isFinite(value.wall_seconds)) parts.push(`最长 ${Math.ceil(value.wall_seconds / 60)} 分钟`);
        if (parts.length) nodes.push(el('p',label + '：' + parts.join('，'),'hint'));
      };
      describe('本轮处理',limits); describe('一次到期检查的独立额度',future);
      if (current) nodes.push(el('p',(allowance?.max_checks || (typeof allowance === 'number' ? allowance : 0)) > 0
        ? '本趟已授权最多一次到期检查，以实际安排记录为准。' : '本趟尚未授权到期检查。','hint'));
      $('budget-panel').hidden = !nodes.length; $('budget').replaceChildren(...nodes);
    }
    function renderSnapshot() {
      const snapshot = s.snapshot || {}, runs = list(snapshot.runs);
      const run = runs.find(r => r.goal_id === s.saved.activeGoal) || runs[0];
      $('run-status').textContent = run?.result?.error_code ? (run.result.error_code === 'MODEL_EXECUTION_DISABLED' ? '助手尚未启用' : '本轮已停止') : run ? status(run.status) : '尚未运行';
      const prior = runs.find(r => r.goal_id === s.saved.activeGoal && (r.result?.user_summary || r.result?.summary));
      const receipt = run?.result?.receipt;
      const result = receipt ? null : run?.result || prior?.result;
      const nodes = [];
      if (receipt) nodes.push(el('p',receiptSummary(receipt),'result-copy'));
      if (!run?.result && prior) nodes.push(el('p','上一轮已交付的结果；本轮进展见下方。','hint'));
      if (result?.user_summary || result?.summary) nodes.push(el('p', result.user_summary || result.summary, 'result-copy'));
      if (result?.outcome_status) nodes.push(el('p', status(result.outcome_status), 'hint'));
      if (list(result?.unresolved).length) {
        nodes.push(el('h3','仍需处理')); const ul = el('ul');
        list(result.unresolved).forEach(v => ul.append(el('li',userIssue(v) || '待进一步确认'))); nodes.push(ul);
      }
      if (run && !nodes.length) nodes.push(el('p', ['queued','running'].includes(run.status)
        ? '目标已保存。助手正在处理，实际工作和结果会在这里更新。' : '这轮尚无可读交付结果。请查看实际操作状态或补充信息。','empty'));
      replace('result', nodes, '先告诉我目标。已有旅行可以接着准备，不必重新开始。');
      renderProposals(snapshot); renderRevisions(snapshot); renderQuestions(snapshot.workspace);
      replace('work', list(snapshot.workspace?.work).map(w => row(w.title || '旅行准备事项',w.status)), '尚无已记录的工作。');
      replace('operations', list(snapshot.operations).map(op => {
        const outcome = ['rejected','invalidated','revoked'].includes(op.result?.status) ? op.result.status : op.status;
        const n = row(toolLabels[op.name] || '旅行准备操作',outcome);
        const summary = text(op.result?.user_summary) || text(op.result?.summary);
        if (summary) n.append(el('p',summary));
        if (op.result?.status === 'rejected') n.append(el('p',errors[op.result.error_code] || '条件发生变化，本次修改未应用。'));
        return n;
      }), '尚无实际操作记录。');
      renderSources(snapshot);
      const memories = list(snapshot.workspace?.memories || snapshot.workspace?.memory);
      $('memory-panel').hidden = !memories.length;
      replace('memories',memories.map(memory => {
        const n = row(memory.memory_kind === 'explicit_correction' ? '你的纠正' : '待确认的理解',memory.status);
        if (memory.status === 'active') n.querySelector('.badge').textContent = '已记录 · 仅本趟';
        if (memory.status === 'candidate') n.querySelector('.badge').textContent = '待确认';
        if (memory.content && !['invalidated','revoked'].includes(memory.status)) n.append(el('p',memory.content));
        return n;
      }), '');
      replace('jobs', list(snapshot.workspace?.jobs).map(job => {
        const n = row(job.purpose || '后续检查',job.status);
        if (job.due_at) n.append(el('p','计划检查：' + new Date(typeof job.due_at === 'number' ? job.due_at * 1000 : job.due_at).toLocaleString('zh-CN'), 'hint job-time'));
        if (readable(job.result)) n.append(el('p',readable(job.result))); return n;
      }), '尚无已安排的检查。');
    }
    function showChanges(parent, changes) {
      list(changes).forEach(change => {
        const block = el('div',null,'diff');
        const path = list(change.path), day = path.find(part => Array.isArray(part) && part[0] === 'day');
        const field = {title:'名称',time:'时间',description:'安排说明',summary:'旅行概览',days:'每日安排',items:'当天活动',preparation:'出发准备',budget:'预算',understanding:'旅行需求'}[path.at(-1)?.[1]];
        block.append(el('h3', text(change.label) || text(change.title) || (day ? `第 ${day[1]} 天 · ` : '') + (field || '行程调整')));
        for (const [key,label] of [['before','调整前'],['after','调整后']]) {
          const value = changeValue(change[key]);
          block.append(el('span',label,key), el('p',value || '内容较多，请查看完整候选行程。'));
        }
        parent.append(block);
      });
    }
    function showPlan(parent, plan) {
      if (!plan) return;
      const detail = el('details'), summary = el('summary','查看候选行程'); detail.append(summary);
      if (plan.title) detail.append(el('h3',plan.title));
      if (plan.summary) detail.append(el('p',readable(plan.summary)));
      list(plan.days).forEach(day => {
        const d = el('div',null,'row'); d.append(el('h3',day.title || `第 ${day.day} 天`));
        list(day.items || day.activities || day.stops).forEach(item => d.append(el('p', [item.time,item.title || item.name,item.description || item.summary].filter(Boolean).join(' · '))));
        detail.append(d);
      });
      if (list(plan.preparation).length) {
        detail.append(el('h3','出发前准备'));
        list(plan.preparation).forEach(item => detail.append(el('p',readable(item))));
      }
      parent.append(detail);
    }
    function renderProposals(snapshot) {
      const revisions = list(snapshot.revisions);
      replace('proposals',list(snapshot.proposals).map(p => {
        const id = p.proposal_id || p.id;
        const appliedRevision = revisions.find(r => r.proposal_id === id && r.kind === 'apply');
        const applied = !!appliedRevision;
        const undone = applied && revisions.some(r => r.undo_of === (appliedRevision.revision_id || appliedRevision.id));
        const expired = p.valid_until && Number(p.valid_until) * 1000 < Date.now();
        const stale = p.expected_revision !== snapshot.trip?.revision;
        const unavailable = p.status && !['proposed','pending'].includes(p.status);
        const n = row(p.summary || p.plan?.title || '行程调整提案', undone ? 'undone' : applied ? 'applied' : unavailable ? p.status : expired ? 'expired' : stale ? 'conflict' : p.status || 'proposed');
        showChanges(n,p.changes || p.diff); showPlan(n,p.plan);
        if (!applied && !expired && !stale && ['proposed','pending',undefined].includes(p.status)) n.append(button('确认应用这份提案', () => client.mutate(tripPath('/apply'), {proposal_id:id,expected_revision:p.expected_revision})));
        return n;
      }), '');
    }
    function renderRevisions(snapshot) {
      const revisions = list(snapshot.revisions);
      replace('revisions', revisions.map(r => {
        const id = r.revision_id || r.id, undone = revisions.some(v => v.undo_of === id);
        const n = row(r.kind === 'undo' ? '已撤销指定调整' : r.summary || '行程调整回执', undone || r.kind === 'undo' ? 'undone' : r.status || 'applied');
        showChanges(n,r.changes || r.diff);
        if (r.kind === 'apply' && !undone) {
          const undo = undoAvailability(r);
          if (undo.available) n.append(button('撤销这次调整', () => client.mutate(tripPath('/undo'), {revision_id:id,expected_revision:snapshot.trip.revision})));
          else n.append(el('p',undo.notice,'hint'));
        }
        return n;
      }), '');
    }
    function renderQuestions(workspace) {
      const inputs = list(workspace?.inputs).filter(q => q.status === 'pending');
      const signature = JSON.stringify(inputs); $('questions-panel').hidden = !inputs.length;
      if (signature === lastQuestions) return; lastQuestions = signature;
      $('questions').replaceChildren(...inputs.map(q => {
        const f = el('form',null,'question-form'); f.append(el('h3',q.question), el('p',q.reason,'hint'));
        const label = el('label','你的回答'), input = el('textarea'); input.id = 'answer-' + q.id;
        label.htmlFor = input.id; input.required = true; input.rows = 2; input.maxLength = 6000;
        const key = 'answer:' + q.id; input.value = s.saved.drafts[key] || '';
        input.addEventListener('input', () => { input.setCustomValidity(''); client.saveDraft(key,input.value); });
        const options = questionOptions(doc, q.options, input, value => client.saveDraft(key,value), () => s.busy || reading);
        if (options) f.append(options, el('p','点选填入回答，可补充后提交。','hint'));
        const submit = el('button','提交回答'); submit.type = 'submit'; submit.dataset.mutation = 'true';
        f.append(label,input,submit); f.addEventListener('submit', e => { e.preventDefault(); perform(() => client.mutate(tripPath(`/inputs/${segment(q.id)}/answer`),{answer:input.value.trim()},{clearDraft:key})); }); return f;
      }));
    }
    function renderSources(snapshot) {
      const sources = list(snapshot.sources || snapshot.trip?.material_sources || snapshot.trip?.materials);
      $('sources-panel').hidden = !sources.length;
      replace('sources', sources.map(source => {
        const n = row(source.title || source.name || (source.label === 'User material' ? '你提供的材料' : source.label) || '旅行材料', source.status);
        const withdrawn = source.revoked || source.availability === 'withdrawn' || ['retracted','revoked','invalidated','withdrawn'].includes(source.status);
        const url = safeURL(source.url || source.source_url);
        if (url && !withdrawn) { const link = el('a','查看原始来源','source-link'); link.href=url; link.target='_blank'; link.rel='noopener noreferrer'; n.append(link); }
        if (source.summary && !withdrawn) n.append(el('p',readable(source.summary)));
        if (source.message) n.append(el('p',source.message,'hint'));
        if (source.content && !withdrawn) { const d = el('details'); d.append(el('summary','查看材料正文'),el('p',text(source.content))); n.append(d); }
        if (source.id && !withdrawn) n.append(button('撤回这份材料',() => client.mutate(tripPath(`/materials/${segment(source.id)}/retract`),{})));
        return n;
      }), '');
    }
    function schedule() {
      win.clearTimeout(timer);
      if (!s.session) return;
      timer = win.setTimeout(async () => {
        if (doc.hidden || s.busy) { schedule(); return; }
        try { await client.refresh({clearError:false}); schedule(); } catch (error) { showError(new Error('暂时无法读取进展。草稿保留，请点击“刷新进展”重新连接。')); }
      }, 4000);
    }
    function setMode(next) {
      mode = next; editingGoal = false; doc.querySelectorAll('[data-mode]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.mode === mode)));
      $('session-picker').hidden = mode === 'new'; $('initial-materials').hidden = mode !== 'new';
      $('authorization').hidden = mode === 'change'; $('goal-form').hidden = mode === 'change' && !!goal();
      $('background-authorization').hidden = mode === 'change';
      $('start').textContent = mode === 'new' ? '开始准备' : '继续准备';
      $('objective').value = s.saved.drafts[goalDraftKey()] || '';
      $('public-search-entities').value = s.saved.drafts[goalDraftKey()+':public_search'] || '';
      const options = s.saved.drafts[goalDraftKey()+':options'] || {};
      $('auto-apply').checked = options.auto_apply === true;
      $('background-check').checked = options.allow_background_check === true;
      render();
      if (mode === 'change' && goal()) $('followup').focus();
    }
    async function readFile(input, target, key) {
      const file = input.files?.[0]; if (!file) return;
      reading = true; render();
      try {
        const combined = await readTextFile(file,target.value);
        target.value = combined; client.saveDraft(key,combined); noticeOverride = '文字已加入草稿，尚未提交。'; render();
      } catch (error) { showError(error); } finally { input.value = ''; reading = false; render(); }
    }
    $('goal-form').addEventListener('submit', e => {
      e.preventDefault(); const objective = $('objective').value.trim(); if (!objective) return;
      const key = goalDraftKey();
      perform(async () => {
        const public_search_entities = parsePublicSearchEntities($('public-search-entities').value);
        const data = mode === 'new' ? await client.mutate('/trips',{brief:objective,objective,
          evidence_text:$('evidence').value,auto_apply:$('auto-apply').checked,allow_background_check:$('background-check').checked,public_search_entities},{newTrip:true,clearDraft:key})
          : await client.mutate(tripPath('/goals'),{objective,auto_apply:$('auto-apply').checked,allow_background_check:$('background-check').checked,public_search_entities,expected_revision:s.snapshot.trip.revision},{clearDraft:key});
        if (data) { $('objective').value=''; client.saveDraft(key+':public_search',''); client.saveDraft(key+':options',{}); if (mode === 'new') { $('evidence').value=''; client.saveDraft('evidence',''); } restoreFollowup(); setMode('existing'); }
        return data;
      });
    });
    $('followup-form').addEventListener('submit', e => {
      e.preventDefault(); const content = $('followup').value.trim(); if (!content) return;
      const kind = $('followup-kind').value;
      const path = kind === 'events' ? `/goals/${segment(goal().id)}/events` : tripPath('/'+kind);
      const body = kind === 'events' ? {request:content} : kind === 'materials' ? {text:content} : {content};
      perform(async () => { const data = await client.mutate(path,body,{clearDraft:draftKey()}); if (data) $('followup').value=''; return data; });
    });
    $('objective').addEventListener('input', () => client.saveDraft(goalDraftKey(),$('objective').value));
    $('public-search-entities').addEventListener('input', () => client.saveDraft(goalDraftKey()+':public_search',$('public-search-entities').value));
    for (const id of ['auto-apply','background-check']) $(id).addEventListener('change', () => client.saveDraft(goalDraftKey()+':options',{
      auto_apply:$('auto-apply').checked,allow_background_check:$('background-check').checked}));
    $('evidence').addEventListener('input', () => client.saveDraft('evidence',$('evidence').value));
    $('followup').addEventListener('input', () => client.saveDraft(draftKey(),$('followup').value));
    $('followup-kind').addEventListener('change', () => { client.saveDraft(draftKey()+':kind',$('followup-kind').value); followupKind(); });
    $('initial-file').addEventListener('change', () => readFile($('initial-file'),$('evidence'),'evidence'));
    $('material-file').addEventListener('change', () => readFile($('material-file'),$('followup'),draftKey()));
    $('sessions').addEventListener('change', () => perform(async () => {
      client.attach(s.library.sessions.find(v => v.id === $('sessions').value) || null);
      restoreFollowup(); await client.refresh(); setMode(mode); return true;
    }));
    doc.querySelectorAll('[data-mode]').forEach(b => b.addEventListener('click', () => setMode(b.dataset.mode)));
    for (const action of ['confirm','cancel']) $(action+'-goal').addEventListener('click', () => perform(() => client.mutate(`/goals/${segment(goal().id)}/${action}`,{expected_goal_version:goal().version})));
    $('refresh').addEventListener('click', () => perform(() => client.refresh()));
    $('save-recovery').addEventListener('click', () => client.persist());
    $('new-goal').addEventListener('click', () => { editingGoal = true; render(); $('objective').focus(); });
    doc.addEventListener('visibilitychange', () => { if (!doc.hidden) schedule(); });
    win.addEventListener('beforeunload', e => { if (s.storageFailed) { e.preventDefault(); e.returnValue=''; } });
    const requested = new URL(win.location.href).searchParams.get('trip');
    client.restore(requested); $('evidence').value = s.saved.drafts.evidence || '';
    restoreFollowup(); setMode(s.session ? 'existing' : 'new');
    perform(() => client.refresh());
    return client;
  }
  const api = {createClient, safeURL, readable, readTextFile, fixtureNotice, receiptSummary, changeValue, undoAvailability, questionOptions, parsePublicSearchEntities, publicSearchScopeLabel, status, mount, LIBRARY, ACTIVE};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else { root.TravelAutopilot = api; if (root.document.currentScript?.dataset.mount !== 'false') mount(root.document,root); }
})(typeof window !== 'undefined' ? window : globalThis);
