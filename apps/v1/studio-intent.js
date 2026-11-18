'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let state = null, busy = false;
  const node = (tag, cls, value) => { const n = document.createElement(tag); n.className = cls || ''; if (value != null) n.textContent = value; return n; };
  const list = (target, values) => target.replaceChildren(...values.map(value => node('li', '', value)));
  const status = text => { $('intent-status').textContent = text; };
  function lock(value) {
    busy = value; $('intent-case').disabled = value; $('intent-refresh').disabled = value;
    $('intent-generate').disabled = value || !state || Boolean(state.card) || state.budget.exhausted;
  }
  function render() {
    const current = state.cases.find(c => c.id === state.case_id);
    $('intent-case').replaceChildren(...state.cases.map(c => { const n = node('option', '', c.title); n.value = c.id; return n; }));
    $('intent-case').value = state.case_id;
    $('intent-brief').textContent = current.brief;
    list($('intent-known'), current.trip_context.confirmed.map(f => f.text));
    const url = new URL(location.href); url.searchParams.set('case_id', state.case_id); history.replaceState(null, '', url);
    const card = state.card; $('intent-result').hidden = !card; $('intent-generate').hidden = Boolean(card);
    $('intent-title').textContent = card?.title || '先安排起来，再问关键的几句。';
    if (!card) return;
    $('intent-framing').textContent = card.framing;
    list($('intent-plan'), card.provisional_plan); list($('intent-assumptions'), card.assumptions);
    list($('intent-unknowns'), card.unknowns); $('intent-unknown-section').hidden = !card.unknowns.length;
    $('intent-questions').replaceChildren(...card.questions.map((q, index) => {
      const section = node('section', 'intent-question');
      section.append(node('p', 'comparison-context', `只问关键的 ${index + 1}/${card.questions.length}`), node('h2', '', q.text),
        node('p', 'comparison-question-why', `为什么问：${q.why_it_matters}`), node('p', 'intent-default', `未回答时，暂按：${q.default_assumption}`));
      const group = node('fieldset', 'comparison-question-choices'); group.setAttribute('aria-label', q.text);
      const effect = node('div', 'intent-effect'); effect.setAttribute('aria-live', 'polite');
      const savedKey = `intent-preview:${state.case_id}:${state.version}:${q.id}`;
      let selected = null;
      try { selected = sessionStorage.getItem(savedKey); } catch (_) { /* Selection preview is optional. */ }
      function show(choice, explicit) { effect.replaceChildren(node('strong', '', explicit ? '如果这样选，安排会改为' : '暂按这个方向推进'), node('p', '', choice.plan_change)); }
      for (const choice of q.choices) {
        const label = node('label', 'comparison-answer'), input = node('input'); input.type = 'radio'; input.name = q.id; input.value = choice.id;
        input.checked = selected === choice.id;
        input.addEventListener('change', () => { show(choice, true); try { sessionStorage.setItem(savedKey, choice.id); } catch (_) {} });
        label.append(input, node('strong', '', choice.label)); group.append(label);
      }
      const choice = q.choices.find(c => c.id === selected) || q.choices.find(c => c.id === q.default_choice_id);
      show(choice, Boolean(selected)); section.append(group, effect); return section;
    }));
  }
  async function load(caseId) {
    if (busy) return; lock(true); status('正在取回安排…');
    try {
      const response = await fetch(`/api/intent${caseId ? `?case_id=${encodeURIComponent(caseId)}` : ''}`, {cache:'no-store', credentials:'same-origin'});
      const data = await response.json(); if (!response.ok) throw new Error(data.error?.message_zh || '读取失败');
      state = data; render(); status(['generating','adjusting'].includes(state.status) ? '仍在准备，稍后读取已有结果即可，不必重复提交。' : state.last_error ? '上次没有完成；原结果保留。' : '');
    } catch (_) { status('暂时没能读取，已有内容保留。'); }
    finally { lock(false); }
  }
  $('intent-case').addEventListener('change', () => load($('intent-case').value));
  $('intent-refresh').addEventListener('click', () => load(state?.case_id));
  $('intent-generate').addEventListener('click', async () => {
    if (busy || !state || state.card) return; lock(true); status('正在想清取舍；会先给安排，不用等你回答。');
    try {
      const response = await fetch('/api/intent/generate', {method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Comparison-Token':state.csrf_token},body:JSON.stringify({case_id:state.case_id,request_id:crypto.randomUUID()})});
      const data = await response.json(); if (!response.ok) throw new Error(data.error?.message_zh || '本次未完成');
      state = data; render(); status('先按这版推进。点选只是预览调整，不会改动已订安排。');
    } catch (error) { status(`${error.message}。请先读取已有结果；没有自动重试。`); }
    finally { lock(false); }
  });
  load(new URL(location.href).searchParams.get('case_id'));
})();
