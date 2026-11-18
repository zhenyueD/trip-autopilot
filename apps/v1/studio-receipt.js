'use strict';

(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.TravelRevisionReceipt = api;
})(typeof window === 'undefined' ? null : window, function () {
  const text = value => typeof value === 'string' || typeof value === 'number' ? String(value) : '';
  const rows = value => Array.isArray(value) ? value.filter(row => row && typeof row === 'object' && !Array.isArray(row)) : [];
  const labels = { title: '安排', time: '开始时间', end_time: '结束时间', why: '原因', transport: '交通',
    preparation: '准备事项', action_guide: '操作指引', details: '材料细节', day: '日期', date: '日期', position: '顺序',
    experience: '体验', city: '城市', lodging: '住宿', rationale: '安排理由', item_order: '当天顺序',
    budget: '预算', decisions: '旅行选择', questions: '待确认事项', understanding: '需求理解',
    user_commitments: '已确认安排', commitment_schedule: '已确认时间', signature: '体验构想',
    sequence: '体验顺序', tradeoff: '取舍', journey: '旅行准备', artifacts: '旅行资料', summary: '概述',
    description: '说明', value: '内容', field: '类别', price: '费用', source_refs: '来源', quote: '原文',
    task: '事项', choice: '选择', reason: '理由', status: '状态', instructions: '步骤', when: '时间',
    completion_condition: '完成条件', fallback: '备选办法', assumptions: '适用前提', supplies: '地点资料', routes: '路线' };
  function node(tag, className, value) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value !== undefined) element.textContent = text(value);
    return element;
  }
  function currentReceipt(trip) {
    const receipt = trip?.revision_receipt;
    const kind = receipt?.judgment?.kind;
    if (trip?.status !== 'ready' || trip.error || !trip.plan || receipt?.version !== 1
      || receipt.trip_id !== trip.id || receipt.revision !== trip.revision
      || (kind !== 'suggestion' && kind !== 'confirmation') || !receipt.changes
      || (kind === 'suggestion' && !receipt.next_action)
      || (kind === 'confirmation' && receipt.next_action != null)
      || !Array.isArray(receipt.alternatives) || !Array.isArray(receipt.mechanical_diff)) return null;
    return receipt;
  }
  function sources(receipt) {
    return [...rows(receipt?.judgment?.citations).flatMap(citation => rows(citation.source_refs)),
      ...rows(receipt?.alternatives).flatMap(alternative => [
        ...rows(alternative.source), ...rows(alternative.details).flatMap(detail => rows(detail.source_refs))])];
  }
  function sourceLine(parent, source, link) {
    const refs = Array.isArray(source) ? rows(source) : source && typeof source === 'object' ? [source]
      : typeof source === 'string' ? [{ source_url: source }] : [];
    if (!refs.length) { parent.append(node('p', '', '来源：尚未查清')); return; }
    refs.forEach(ref => {
      const line = node('p', 'receipt-source', `来源：${text(ref.attribution || ref.label) || '来源资料'}`);
      const url = ref.source_url || ref.url;
      if (url && link && ref.link_available !== false && ref.direct_read !== false) {
        const sourceLink = link('查看来源', url); line.append(document.createTextNode(' · '), sourceLink);
        if (sourceLink.tagName?.toLowerCase() !== 'a') line.append(document.createTextNode(' · 来源未验证或不可用（不可点击）'));
      } else line.append(document.createTextNode(' · 尚无可查看链接'));
      parent.append(line);
      if (ref.quote) parent.append(node('blockquote', '', ref.quote));
    });
  }
  function readable(value) {
    if (value == null) return '无';
    if (typeof value !== 'object') return text(value);
    if (Array.isArray(value)) return value.length ? value.map(readable).join('；') : '无';
    return Object.entries(value).map(([key, entry]) => `${labels[key] || '内容'}：${readable(entry)}`).join('；') || '无';
  }
  function identityValues(diff, path, field, current) {
    const whole = diff.find(row => row.path === path);
    if (whole) return [whole.before?.[field], whole.after?.[field]];
    const leaf = diff.find(row => row.path === `${path}/${field}`);
    return leaf ? [leaf.before, leaf.after] : [current?.[field], current?.[field]];
  }
  function diffLabel(row, trip, receipt) {
    const path = text(row.path).split('/').filter(Boolean);
    // writer_critic.mechanical_diff uses physical JSON array indices, not day/item IDs.
    const hasDay = path[0] === 'days' && /^\d+$/.test(path[1]);
    const currentDay = hasDay ? trip.plan?.days?.[Number(path[1])] : null;
    const days = hasDay ? identityValues(receipt.mechanical_diff, `/days/${path[1]}`, 'day', currentDay) : [];
    const day = [...new Set(days.filter(value => Number.isInteger(value) && value > 0))].map(value => `第 ${value} 天`).join(' → ');
    const hasItem = hasDay && path[2] === 'items' && /^\d+$/.test(path[3]);
    const titles = hasItem ? identityValues(receipt.mechanical_diff, `/days/${path[1]}/items/${path[3]}`, 'title', currentDay?.items?.[Number(path[3])]) : [];
    const item = [...new Set(titles.map(text).filter(Boolean))].join(' → ');
    return [day, item, labels[path.at(-1)] || (hasItem ? '安排内容' : '方案内容')].filter(Boolean).join(' · ');
  }
  function renderChanges(parent, receipt, trip) {
    const section = node('section', 'receipt-changes'); section.dataset.receiptSection = 'changes';
    section.append(node('h3', '', '改了什么'), node('p', '', receipt.changes.summary));
    const details = node('wa-details', 'receipt-diff'); details.append(node('summary', '', '查看改前 / 改后'));
    rows(receipt.mechanical_diff).forEach(row => {
      const change = node('article', 'receipt-diff-row'); change.append(node('h4', '', diffLabel(row, trip, receipt)));
      const columns = node('div', 'receipt-diff-columns');
      ['before', 'after'].forEach(side => {
        const column = node('div'); column.append(node('strong', '', side === 'before' ? '改前' : '改后'), node('p', '', readable(row[side])));
        columns.append(column);
      });
      change.append(columns); details.append(change);
    });
    if (!receipt.mechanical_diff.length) details.append(node('p', '', '本轮没有已记录的方案内容变化。'));
    section.append(details); parent.append(section);
  }
  function renderJudgment(parent, receipt, link) {
    const confirmation = receipt.judgment.kind === 'confirmation';
    const section = node('aside', 'receipt-judgment'); section.dataset.receiptSection = 'judgment';
    section.append(node('h3', '', confirmation ? '执行确认' : '为什么这样改'),
      node('span', 'receipt-suggestion-label', confirmation ? '确认 · 按你的决定执行' : '建议 · 非已核实事实'),
      node('p', '', receipt.judgment.text));
    const citations = rows(receipt.judgment.citations);
    if (citations.length) {
      const details = node('wa-details', 'receipt-citations'); details.append(node('summary', '', '查看判断依据'));
      citations.forEach(citation => { if (citation.value != null) details.append(node('p', '', `引用内容：${text(citation.value)} · 未核实`)); sourceLine(details, citation.source_refs, link); });
      section.append(details);
    }
    parent.append(section);
  }
  function renderAlternatives(parent, receipt, link) {
    const section = node('wa-details', 'receipt-alternatives'); section.dataset.receiptSection = 'alternatives';
    const summary = node('summary', '', receipt.alternatives.length ? `查看 ${receipt.alternatives.length} 个替代方案` : '查看替代方案状态');
    summary.setAttribute('slot', 'summary');
    section.append(summary);
    rows(receipt.alternatives).forEach(alternative => {
      const card = node('article', 'receipt-alternative');
      card.append(node('h4', '', alternative.title || '备选安排'), node('span', 'receipt-suggestion-label', '建议'));
      if (alternative.advice) card.append(node('p', '', alternative.advice));
      ['price', 'time'].forEach(field => card.append(node('p', '', `${field === 'price' ? '价格' : '时间'}：${alternative[field] == null || alternative[field] === '' ? '尚未查清' : `${text(alternative[field])} · 未核实`}`)));
      sourceLine(card, alternative.source, link);
      card.append(node('p', '', `核验时间：${text(alternative.verified_at) || '尚未核验'}`)); section.append(card);
    });
    if (!receipt.alternatives.length) section.append(node('p', '',
      receipt.judgment?.kind === 'confirmation' ? '按你的决定执行，未追加替代建议。' : '替代安排、价格、时间与来源尚未查清；尚未核验。'));
    parent.append(section);
  }
  function actionKey(trip, receipt) { return JSON.stringify([trip.id, trip.revision, receipt.binding?.candidate_sha256, receipt.next_action]); }
  function actionFeedback(trip, action) {
    if (action?.type !== 'revise_plan' || typeof action.feedback !== 'string' || !action.feedback.trim()) return '';
    const days = action.selected_days, existing = new Set(rows(trip.plan?.days).map(day => day.day));
    if (!Array.isArray(days) || !days.length || new Set(days).size !== days.length
      || days.some(day => !Number.isInteger(day) || day <= 0 || !existing.has(day))) return '';
    return `请仅调整${days.map(day => `第 ${day} 天`).join('、')}的相关安排，保留其他日期的无关安排。\n${action.feedback.trim()}`;
  }
  function create({ container, getTrip, blocked = () => false, submit, link }) {
    let version = 0, currentButton = null;
    const submitted = new Set(), dismissed = new Set();
    function reset() { version += 1; if (currentButton) currentButton.disabled = true; currentButton = null; container.hidden = true; container.replaceChildren(); }
    function dismiss() {
      const trip = getTrip(), receipt = currentReceipt(trip);
      if (receipt) dismissed.add(actionKey(trip, receipt));
      reset();
    }
    function render() {
      reset(); const trip = getTrip(), receipt = currentReceipt(trip); if (!receipt) return;
      const generation = version, key = actionKey(trip, receipt), action = receipt.next_action?.action;
      const feedback = actionFeedback(trip, action);
      if (blocked() || dismissed.has(key)) return;
      container.hidden = false; container.append(node('h2', '', '这次调整，已经更新'));
      renderChanges(container, receipt, trip); renderJudgment(container, receipt, link); renderAlternatives(container, receipt, link);
      if (!receipt.next_action) return;
      const section = node('section', 'receipt-next-action'); section.dataset.receiptSection = 'next_action';
      section.append(node('h3', '', '下一步'));
      const button = node('button', 'secondary-button', receipt.next_action.label || '继续调整'); button.type = 'button';
      currentButton = button;
      button.disabled = blocked() || submitted.has(key) || !feedback;
      button.addEventListener('click', async () => {
        const current = getTrip(), currentValue = currentReceipt(current);
        if (button.disabled || version !== generation || blocked() || !currentValue || actionKey(current, currentValue) !== key
          || actionFeedback(current, currentValue.next_action.action) !== feedback || submitted.has(key)) return;
        submitted.add(key); button.disabled = true;
        const accepted = await submit({ feedback, expected_revision: trip.revision, trip_id: trip.id, confirmed_intent: 'revision' });
        if (accepted === false) submitted.delete(key);
        render();
      });
      section.append(button, node('p', 'field-hint', '点击后继续调整方案，不会预订或付款。')); container.append(section);
    }
    return { render, reset, dismiss };
  }
  return { create, currentReceipt, sources };
});
