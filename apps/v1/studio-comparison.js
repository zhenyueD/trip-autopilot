'use strict';

// A bounded Studio view. Server state owns the decision; input remains local until submitted.
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.TravelComparison = api;
})(typeof window === 'undefined' ? null : window, function () {
  const text = value => typeof value === 'string' ? value : '';
  const list = value => Array.isArray(value) ? value : [];
  const requestId = () => globalThis.crypto?.randomUUID?.()
    || `comparison-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  function safeURL(value) {
    try {
      const url = new URL(value);
      return url.protocol === 'https:' && !url.username && !url.password ? url.href : '';
    } catch (_) { return ''; }
  }
  function createController({ fetch: transport, changed = () => {} }) {
    let state = null, busy = '', loading = false, message = '', error = false, needsRefresh = false;
    let draft = { feedback: '', answerId: '' };
    const drafts = new Map();
    const snapshot = () => ({ state, busy, loading, message, error, needsRefresh, draft: { ...draft } });
    const notify = () => changed(snapshot());
    const blocked = () => busy || loading || needsRefresh || ['generating', 'adjusting'].includes(state?.status);
    async function read(path, method = 'GET', body) {
      const response = await transport(path, { method, cache: 'no-store', credentials: 'same-origin',
        headers: { 'Accept': 'application/json', ...(body ? { 'Content-Type': 'application/json',
          'X-Comparison-Token': text(state?.csrf_token) } : {}) },
        ...(body ? { body: JSON.stringify(body) } : {}) });
      const data = await response.json();
      if (!response.ok) throw Object.assign(new Error('comparison request failed'), { status: response.status, data });
      return data;
    }
    async function load(caseId = '') {
      if (busy || loading) return;
      loading = true; message = state?.card ? '正在读取最新比较…' : '正在取回你的比较…'; error = false; notify();
      try {
        const next = await read(`/api/comparison${caseId ? `?case_id=${encodeURIComponent(caseId)}` : ''}`);
        if (state?.case_id !== next.case_id) {
          if (state?.case_id) drafts.set(state.case_id, { ...draft });
          draft = drafts.get(next.case_id) || { feedback: '', answerId: '' };
        }
        state = next; needsRefresh = false;
        message = ['generating', 'adjusting'].includes(state.status)
          ? '比较仍在准备中。稍后点“读取最新结果”查看。' : '';
      } catch (_) { message = '暂时没能读取比较。请重试，已有内容会保留。'; error = true; }
      finally { loading = false; notify(); }
    }
    function fail(requestError) {
      const code = requestError.data?.error?.code;
      error = true;
      needsRefresh = !requestError.status || ['VERSION_CONFLICT', 'REQUEST_IN_PROGRESS'].includes(code);
      message = code === 'VERSION_CONFLICT' ? '比较已在别处更新。本次没有覆盖；请读取最新结果后再选择。'
        : code === 'REQUEST_IN_PROGRESS' ? '已有一次操作正在处理。请读取最新结果，避免重复提交。'
          : code === 'BUDGET_EXHAUSTED' ? '本轮调整次数已用完。当前比较和已保存选择仍可查看。'
            : !requestError.status ? '尚未确认这次操作是否保存。请先读取最新结果；当前内容和输入已保留。'
              : '这次没有完成。当前比较和输入已保留，你可以稍后重试。';
    }
    async function mutate(action, body = {}) {
      if (blocked()) return false;
      busy = action; error = false;
      message = action === 'select' ? '正在保存选择…' : action === 'adjust'
        ? '正在按你的想法重新比较，原卡会一直保留。' : '正在为你比较两个选择…'; notify();
      try {
        const next = await read(`/api/comparison/${action}`, 'POST', { request_id: requestId(),
          ...(state?.case_id ? { case_id: state.case_id } : {}), ...body });
        state = next; needsRefresh = false;
        if (action === 'adjust') draft = { feedback: '', answerId: '' };
        message = action === 'select' ? '选择已保存。你随时可以改选。'
          : action === 'adjust' ? '比较已按这次偏好更新，请看看新的取舍。' : '比较已准备好。';
        return true;
      } catch (requestError) { fail(requestError); return false; }
      finally { busy = ''; notify(); }
    }
    function select(optionId) {
      if (!list(state?.card?.options).some(option => option.id === optionId)) return false;
      return mutate('select', { expected_version: state.version, option_id: optionId });
    }
    function adjust() {
      const choice = list(state?.card?.question?.choices).find(item => item.id === draft.answerId);
      const feedback = [choice ? `关于“${text(state.card.question.text)}”，我的选择是：${text(choice.label)}（${text(choice.meaning)}）。` : '',
        draft.feedback.trim()].filter(Boolean).join('\n');
      if (!feedback || !state?.card) { message = '选一个答案，或写下你想调整的地方。'; error = true; notify(); return false; }
      return mutate('adjust', { expected_version: state.version, feedback,
        ...(choice ? { answer_id: choice.id } : {}) });
    }
    function setDraft(value) { draft = { ...draft, ...value }; }
    async function traffic() {
      if (blocked()) return false;
      busy = 'traffic'; message = '正在查询这段路线…'; error = false; notify();
      try {
        const result = await read('/api/comparison/traffic', 'POST', { request_id: requestId(),
          ...(state?.case_id ? { case_id: state.case_id } : {}) });
        state = { ...state, route: result.route || result };
        message = state.route.status === 'ready' ? '这段路线参考已更新。' : '这段路线尚未查到可用结果。';
      } catch (requestError) { fail(requestError); }
      finally { busy = ''; notify(); }
    }
    return { snapshot, load, select, adjust, setDraft, traffic, generate: () => mutate('generate') };
  }

  function mount({ container, document: doc = globalThis.document, fetch: transport = globalThis.fetch.bind(globalThis),
    location: locationRef = globalThis.location, history: historyRef = globalThis.history }) {
    if (!container) return null;
    doc.body.classList.add('comparison-mode'); container.hidden = false;
    ['onboarding', 'workspace', 'recent-trips', 'notice', 'storage-warning'].forEach(id => {
      const element = doc.getElementById(id); if (element) element.hidden = true;
    });
    const context = doc.querySelector('.header-context > span');
    if (context) context.textContent = '把选择想清楚';
    const create = (tag, className, value) => {
      const node = doc.createElement(tag); if (className) node.className = className;
      if (value !== undefined) node.textContent = text(value); return node;
    };
    const button = (label, className, action) => {
      const node = create('button', className, label); node.type = 'button';
      if (action) node.addEventListener('click', action); return node;
    };
    const view = createShell(create, button);
    container.replaceChildren(...view.children);
    let cardKey = '', casesKey = '', optionButtons = [], answerInputs = [], feedbackInput = null, expiryTimer = null;
    let controller;
    function paint(snapshot) {
      const { state, loading, busy, message, error, needsRefresh, draft } = snapshot;
      const card = state?.card, locked = Boolean(loading || busy || needsRefresh || ['generating', 'adjusting'].includes(state?.status));
      view.status.textContent = message; view.status.hidden = !message;
      view.status.setAttribute('role', error ? 'alert' : 'status');
      view.status.dataset.kind = error ? 'error' : 'info';
      view.refresh.hidden = !error && !needsRefresh && !['generating', 'adjusting'].includes(state?.status);
      view.refresh.disabled = Boolean(loading || busy);
      view.empty.hidden = Boolean(card); view.content.hidden = !card;
      const cases = list(state?.cases), nextCasesKey = JSON.stringify(cases);
      view.casePicker.hidden = cases.length < 2;
      if (nextCasesKey !== casesKey) {
        casesKey = nextCasesKey;
        view.caseSelect.replaceChildren(...cases.map(item => {
          const option = create('option', '', item.title); option.value = item.id; return option;
        }));
      }
      view.caseSelect.disabled = Boolean(loading || busy); view.caseSelect.value = state?.case_id || '';
      const currentCase = cases.find(item => item.id === state?.case_id);
      view.emptyContext.textContent = text(currentCase?.title) || '你的下一项旅行选择';
      view.traffic.hidden = currentCase?.traffic_supported !== true && state?.traffic_supported !== true;
      if (state?.case_id && locationRef?.href && historyRef?.replaceState) {
        const url = new URL(locationRef.href); url.searchParams.set('case_id', state.case_id);
        historyRef.replaceState(null, '', url);
      }
      view.generate.disabled = locked || !state; view.generate.textContent = busy === 'generate' ? '正在比较…' : '生成这张比较卡';
      const nextKey = `${state?.case_id || ''}:${JSON.stringify(card)}`;
      if (card && nextKey !== cardKey) {
        cardKey = nextKey;
        optionButtons = renderCard(card, view, create, button, controller);
        ({ answerInputs, feedbackInput } = renderQuestion(card.question, view, create, controller, draft));
      }
      optionButtons.forEach(control => {
        const selected = state?.selection?.option_id === control.dataset.optionId;
        control.textContent = selected ? '已选这个' : state?.selection ? '改选这个' : '选这个';
        control.setAttribute('aria-pressed', String(selected)); control.disabled = locked || selected;
      });
      answerInputs.forEach(input => { input.disabled = locked; input.checked = input.value === draft.answerId; });
      if (feedbackInput) { feedbackInput.disabled = locked; feedbackInput.value = draft.feedback; }
      view.adjust.disabled = locked; view.adjust.textContent = busy === 'adjust' ? '正在调整比较…' : '按这些想法调整比较';
      const selected = list(card?.options).find(option => option.id === state?.selection?.option_id);
      view.receipt.hidden = !selected;
      view.receiptTitle.textContent = selected ? `已记下你的选择：${text(selected.name)}` : '';
      renderTraffic(state?.route, view, create, locked);
      if (expiryTimer) clearTimeout(expiryTimer);
      const expiresIn = new Date(state?.route?.expires_at).getTime() - Date.now();
      if (state?.route?.status === 'ready' && expiresIn > 0) {
        expiryTimer = setTimeout(() => paint(controller.snapshot()), Math.min(expiresIn + 20, 2147483647));
        expiryTimer.unref?.();
      }
    }
    controller = createController({ fetch: transport, changed: paint });
    view.generate.addEventListener('click', () => controller.generate());
    view.refresh.addEventListener('click', () => controller.load(controller.snapshot().state?.case_id));
    view.caseSelect.addEventListener('change', () => controller.load(view.caseSelect.value));
    view.trafficButton.addEventListener('click', () => controller.traffic());
    view.form.addEventListener('submit', event => { event.preventDefault(); controller.adjust(); });
    view.adjust.addEventListener('click', () => controller.adjust());
    const initialCase = locationRef?.href ? new URL(locationRef.href).searchParams.get('case_id') || '' : '';
    controller.load(initialCase); return controller;
  }

  function createShell(create, button) {
    const casePicker = create('div', 'comparison-case-picker');
    const caseLabel = create('label', '', '这次想比较'); caseLabel.htmlFor = 'comparison-case';
    const caseSelect = create('select'); caseSelect.id = 'comparison-case';
    casePicker.append(caseLabel, caseSelect); casePicker.hidden = true;
    const status = create('p', 'comparison-status'); status.setAttribute('aria-live', 'polite');
    const refresh = button('读取最新结果', 'text-button'); refresh.hidden = true;
    const statusRow = create('div', 'comparison-status-row'); statusRow.append(status, refresh);
    const empty = create('section', 'comparison-empty');
    const generate = button('生成这张比较卡', 'primary-button'); generate.dataset.action = 'generate';
    const emptyContext = create('p', 'comparison-context', '你的下一项旅行选择');
    empty.append(emptyContext, create('h1', '', '两个选择，\n先看清取舍。'),
      create('p', 'comparison-empty-copy', '结合这次旅行的同行人与偏好，把两个方案放在一起比较，再做决定。'), generate);
    const content = create('div', 'comparison-content'); content.hidden = true;
    const hero = create('header', 'comparison-hero'), options = create('div', 'comparison-options');
    const receipt = create('aside', 'comparison-receipt'); receipt.setAttribute('aria-live', 'polite');
    const receiptTitle = create('strong');
    receipt.append(receiptTitle, create('p', '', '这是一项旅行偏好，尚未预订。想换一个，直接点上方“改选这个”。')); receipt.hidden = true;
    const form = create('form', 'comparison-adjust'), question = create('div');
    const adjust = button('按这些想法调整比较', 'primary-button'); adjust.dataset.action = 'adjust';
    const adjustFooter = create('div', 'comparison-adjust-footer');
    adjustFooter.append(create('p', '', '重新比较两个选择。新版本准备好前，这份比较一直在。'), adjust);
    form.append(question, adjustFooter);
    const traffic = create('section', 'comparison-traffic');
    const trafficHeading = create('div', 'comparison-subheading');
    trafficHeading.append(create('h2', '', '这段交通怎么走'), create('span', 'comparison-attribution', 'Google Maps'));
    const trafficBody = create('div', 'comparison-traffic-body');
    const trafficButton = button('查看这段路线参考', 'secondary-button');
    traffic.append(trafficHeading, trafficBody, trafficButton);
    const evidence = create('details', 'comparison-evidence');
    content.append(hero, options, receipt, form, traffic, evidence);
    return { children: [casePicker, statusRow, empty, content], casePicker, caseSelect, emptyContext, status, refresh, empty, generate, content, hero, options,
      receipt, receiptTitle, form, question, adjust, traffic, trafficBody, trafficButton, evidence };
  }

  function renderCard(card, view, create, button, controller) {
    const recommendation = card.recommendation || {};
    const recommended = list(card.options).find(option => option.id === recommendation.option_id);
    const judgment = create('div', 'comparison-judgment');
    judgment.append(create('p', 'comparison-recommendation-label', recommended ? `更推荐 · ${text(recommended.name)}` : '先说判断'),
      create('p', 'comparison-reason', recommendation.reason));
    const tradeoff = create('p', 'comparison-tradeoff');
    tradeoff.append(create('strong', '', '要接受的取舍 '), create('span', '', recommendation.tradeoff)); judgment.append(tradeoff);
    view.hero.replaceChildren(create('p', 'comparison-context', card.framing), create('h1', '', card.title), judgment);
    const buttons = [], cards = list(card.options).map((option, index) => {
      const panel = create('article', 'comparison-option'); panel.dataset.option = text(option.id);
      const heading = create('div', 'comparison-option-heading');
      heading.append(create('span', 'comparison-option-number', String(index + 1).padStart(2, '0')),
        create('span', 'comparison-option-note', option.id === recommendation.option_id ? '更贴合这次旅行' : '另一种体验'));
      const details = create('dl', 'comparison-facts');
      [['适合你们，如果', option.good_for], ['需要留意', option.watch_out]].forEach(([label, value]) => {
        details.append(create('dt', '', label), create('dd', '', value));
      });
      const rhythm = create('ol', 'comparison-rhythm');
      list(option.rhythm).forEach(step => rhythm.append(create('li', '', step)));
      const choose = button('选这个', 'secondary-button comparison-choose', () => controller.select(option.id));
      choose.dataset.optionId = option.id; choose.setAttribute('aria-label', `选择${text(option.name)}`); buttons.push(choose);
      panel.append(heading, create('h2', '', option.name), create('p', 'comparison-highlight', option.highlight), details,
        create('h3', 'comparison-rhythm-title', '大致节奏'), rhythm, choose); return panel;
    });
    view.options.replaceChildren(...cards);
    const evidence = [create('summary', '', '待确认的事与参考来源')];
    if (list(card.unknowns).length) {
      const unknowns = create('ul'); list(card.unknowns).forEach(value => unknowns.append(create('li', '', value)));
      evidence.push(create('h3', '', '还需要确认'), unknowns);
    }
    const sources = create('ul', 'comparison-sources');
    list(card.sources).forEach(source => {
      const item = create('li'), url = safeURL(source.url);
      const link = create(url ? 'a' : 'span', '', source.title);
      if (url) { link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; }
      item.append(link); sources.append(item);
    });
    evidence.push(create('h3', '', '参考来源'), sources); view.evidence.replaceChildren(...evidence); return buttons;
  }

  function renderQuestion(question, view, create, controller, draft) {
    const title = create('h2', '', text(question?.text) || '还有什么想调整？');
    title.id = 'comparison-question-title';
    const fieldset = create('fieldset', 'comparison-question-choices');
    fieldset.setAttribute('aria-labelledby', title.id);
    const answerInputs = list(question?.choices).map(choice => {
      const label = create('label', 'comparison-answer');
      const input = create('input'); input.type = 'radio'; input.name = 'comparison-answer'; input.value = choice.id;
      input.checked = draft.answerId === choice.id;
      input.addEventListener('change', () => { if (input.checked) controller.setDraft({ answerId: choice.id }); });
      const copy = create('span'); copy.append(create('strong', '', choice.label), create('small', '', choice.meaning));
      label.append(input, copy); fieldset.append(label); return input;
    });
    const label = create('label', 'comparison-feedback-label', '也可以补充你们的想法'); label.htmlFor = 'comparison-feedback';
    const feedbackInput = create('textarea'); feedbackInput.id = 'comparison-feedback'; feedbackInput.rows = 2;
    feedbackInput.maxLength = 2000; feedbackInput.placeholder = '比如：希望晚饭后早点回去，走累了随时能停下来。';
    feedbackInput.value = draft.feedback;
    feedbackInput.addEventListener('input', () => controller.setDraft({ feedback: feedbackInput.value }));
    view.question.replaceChildren(title, create('p', 'comparison-question-why', question?.why_it_matters), fieldset, label, feedbackInput);
    return { answerInputs, feedbackInput };
  }

  function renderTraffic(route, view, create, locked) {
    const ready = route?.status === 'ready' && (!route.expires_at || new Date(route.expires_at).getTime() > Date.now());
    const note = create('p', 'comparison-traffic-note', '仅供上述起终点之间的驾车参考，不含其他接送路段，也不是接驳班次或实时路况。');
    const content = [];
    if (ready) {
      content.push(create('p', 'comparison-route-label', `${text(route.origin)} → ${text(route.destination)}`));
      const metrics = [];
      if (Number.isFinite(route.distance_meters)) metrics.push(`${(route.distance_meters / 1000).toFixed(1)} 公里`);
      if (Number.isFinite(route.duration_seconds)) metrics.push(`约 ${Math.ceil(route.duration_seconds / 60)} 分钟`);
      content.push(create('p', 'comparison-route-metrics', metrics.join(' · ') || '尚无可用的距离与时间'));
    } else {
      const description = route?.status === 'awaiting_confirmation' ? '这段路线的起终点仍需确认，暂不能给出可靠的交通时间。'
        : route?.status === 'expired' || route?.status === 'ready' ? '上次的路线参考已过期，距离与时间暂不展示。'
          : !route || ['unchecked', 'not_requested'].includes(route.status) ? '尚未查询这段路线。起终点未确认时，暂不计算交通时间。'
            : '这段路线暂未查到可用结果，距离与时间仍待确认。';
      content.push(create('p', '', description));
    }
    content.push(note); view.trafficBody.replaceChildren(...content);
    view.trafficButton.hidden = ready; view.trafficButton.disabled = locked;
  }
  return { createController, mount, safeURL };
});
