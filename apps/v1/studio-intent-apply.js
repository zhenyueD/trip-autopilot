'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const node = (tag, cls, value) => { const n = document.createElement(tag); n.className = cls || ''; if (value != null) n.textContent = value; return n; };
  const list = (id, values) => $(id).replaceChildren(...values.map(value => node('li', '', value)));
  let state = null, busy = false;
  const status = text => { $('apply-status').textContent = text; };
  const key = () => `intent-apply-choices:${state.case_id}`;
  const choices = () => [...document.querySelectorAll('#apply-questions input:checked')].map(n => ({question_id:n.name, choice_id:n.value}));
  function lock(value) {
    busy = value;
    for (const id of ['apply-case','apply-refresh','apply-undo','apply-reapply']) $(id).disabled = value;
    for (const input of document.querySelectorAll('#apply-questions input')) input.disabled = value;
    $('apply-submit').disabled = value || !state || state.budget.exhausted || !choices().length;
  }
  function renderQuestions() {
    let saved = {}; try { saved = JSON.parse(sessionStorage.getItem(key()) || '{}'); } catch (_) {}
    $('apply-questions').replaceChildren(...state.card.questions.map(q => {
      const section = node('section', 'intent-question');
      section.append(node('h2','',q.text), node('p','comparison-question-why',`为什么问：${q.why_it_matters}`), node('p','intent-default',`未选择时，仍保留当前安排。`));
      const group = node('fieldset','comparison-question-choices'); group.setAttribute('aria-label',q.text);
      const effect = node('div','intent-effect'); effect.setAttribute('aria-live','polite');
      effect.textContent = '选择后先看影响，按下修改按钮才会保存到行程。';
      for (const c of q.choices) {
        const label = node('label','comparison-answer'), input = node('input'); input.type='radio'; input.name=q.id; input.value=c.id;
        input.checked = saved[q.id] === c.id;
        if (input.checked) effect.textContent = c.plan_change;
        input.addEventListener('change', () => { saved[q.id]=c.id; effect.textContent=c.plan_change; try {sessionStorage.setItem(key(),JSON.stringify(saved));} catch (_) {} lock(false); });
        label.append(input,node('strong','',c.label)); group.append(label);
      }
      section.append(group,effect); return section;
    }));
  }
  function renderPlan() {
    const plan = state.trip.plan;
    $('apply-version').textContent = `已保存 · 版本 ${state.trip.revision}`;
    $('apply-days').replaceChildren(...plan.days.map(day => {
      const section=node('details','apply-day'), affected=state.scope_day_numbers.includes(day.day); section.open=affected;
      const summary=node('summary','',day.title); summary.append(node('small','',affected?'本次调整范围':'保留原安排')); section.append(summary);
      for (const item of day.items) {
        const row=node('div','apply-item'), detail=node('div'), title=node('h4','',item.title);
        if (state.locked_item_ids.includes(item.id)) title.append(node('span','apply-protected','保留'));
        detail.append(title,node('p','',item.why),node('p','',item.transport));
        row.append(node('div','apply-item-time',`${item.time}–${item.end_time}`),detail); section.append(row);
      }
      return section;
    }));
  }
  function renderReceipt() {
    const revision=state.revision; $('apply-receipt').hidden=!revision;
    if (!revision) return;
    const receipt=revision.receipt, undone=revision.state==='undone';
    $('apply-receipt-state').textContent=undone?'已撤销这次修改；当前行程以保存版本为准':'修改已保存，其他安排保留';
    list('apply-diff',state.change_lines || []);
    document.querySelectorAll('.apply-review-note').forEach(n=>n.remove());
    $('apply-judgment').textContent=receipt.judgment;
    const notes=state.review_notes || [], note=text=>node('p','comparison-question-why apply-review-note',`复核补充（非模型原话）：${text}`);
    for (const n of notes.filter(n=>n.target==='judgment')) $('apply-judgment').after(note(n.text));
    $('apply-alternatives').replaceChildren(...receipt.alternatives.map((a,index)=>{
      const item=node('li','',typeof a==='string'?a:`${a.label}：${a.tradeoff}`);
      for (const n of notes.filter(n=>n.target==='alternative'&&n.index===index)) item.append(note(n.text));
      return item;
    }));
    list('apply-answers',state.answer_lines || []);
    $('apply-undo').hidden=undone; $('apply-reapply').hidden=!undone;
  }
  function render() {
    $('apply-case').replaceChildren(...state.cases.map(c=>{const n=node('option','',c.title);n.value=c.id;return n;})); $('apply-case').value=state.case_id;
    $('apply-title').textContent=state.card.title; $('apply-brief').textContent=state.brief;
    $('apply-boundary').textContent=state.input_kind==='synthetic_engineering_case_not_real_booking'?'验收示例：订单和选择均为模拟；修改会真实保存到独立行程库。':'验收副本：沿用你已确认的旅行背景，初始行程与本次选择是验收构造；不覆盖原旅行。';
    list('apply-known',state.confirmed.map(f=>f.text));list('apply-unknowns',state.card.unknowns);
    const url=new URL(location.href);url.searchParams.set('case_id',state.case_id);history.replaceState(null,'',url);
    renderQuestions();renderReceipt();renderPlan();
  }
  async function load(caseId) {
    if(busy)return;lock(true);status('正在读取已保存的行程…');
    try {
      const r=await fetch(`/api/intent-apply?case_id=${encodeURIComponent(caseId||'')}`,{cache:'no-store',credentials:'same-origin'}), data=await r.json();
      if(!r.ok)throw new Error(data.error?.message_zh||'读取未完成');state=data;render();status('');
    } catch(e){status(e.message+'；已有页面保留。');}finally{lock(false);}
  }
  async function mutate(action) {
    if(busy||!state)return;
    const body={case_id:state.case_id,request_id:crypto.randomUUID(),expected_revision:state.trip.revision};
    if(action==='apply')body.selections=choices();else body.revision_id=action==='reapply'?(state.revision?.source_revision||state.revision?.revision_id):state.revision?.revision_id;
    lock(true);status(action==='apply'?'正在按选择调整；已有行程保留，完成后一起保存。':'正在更新保存的版本…');
    try {
      const r=await fetch(`/api/intent-apply/${action}`,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Comparison-Token':state.csrf_token},body:JSON.stringify(body)}),data=await r.json();
      if(!r.ok)throw new Error(data.error?.message_zh||'本次未完成');state=data;render();status(action==='undo'?'本次修改已撤销。':'已保存，可以查看变化或撤销。');$('apply-receipt').scrollIntoView({block:'start',behavior:'smooth'});
    }catch(e){status(`${e.message}。先读取最新行程；没有自动重试。`);}finally{lock(false);}
  }
  $('apply-case').addEventListener('change',()=>load($('apply-case').value));$('apply-refresh').addEventListener('click',()=>load(state?.case_id));
  $('apply-submit').addEventListener('click',()=>mutate('apply'));$('apply-undo').addEventListener('click',()=>mutate('undo'));$('apply-reapply').addEventListener('click',()=>mutate('reapply'));
  load(new URL(location.href).searchParams.get('case_id'));
})();
