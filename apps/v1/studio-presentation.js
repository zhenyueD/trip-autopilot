'use strict';
(function (root, factory) {
  const api = factory(typeof module === 'object' && module.exports ? require('./studio-preparation.js') : root.TravelPreparation);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.TravelPresentation = api;
})(typeof window === 'undefined' ? null : window, function (preparation) {
  const rows = value => Array.isArray(value) ? value.filter(row => row && typeof row === 'object') : [];
  const labels = { added: '本轮新增', modified: '本轮修改', removed: '本轮删除' };
  const fields = { title: '安排', time: '开始', end_time: '结束', why: '原因', transport: '交通', preparation: '准备', action_guide: '操作指引', details: '材料细节', day: '日期', position: '当天顺序', experience: '体验', supply_id: '供应资料' };
  const changesForDay = (plan, day) => rows(plan?.revision_changes?.items).filter(row => Number(row.day) === Number(day) || Number(row.before_day) === Number(day));
  const itemChange = (plan, day, id) => changesForDay(plan, day).find(row => row.item_id === id && Number(row.day) === Number(day));
  const removedForDay = (plan, day) => changesForDay(plan, day).filter(row => row.kind === 'removed' || row.before_day != null && Number(row.before_day) === Number(day) && Number(row.day) !== Number(day));
  function itemDetails(plan, item, sharedUnavailable = new Set()) {
    const related = [...rows(plan?.preparation), ...rows(plan?.preparation_pack?.actions)]
      .filter(action => Array.isArray(action.related_item_ids) && action.related_item_ids.includes(item.id));
    const unavailable = preparation.unavailableSourceURLs(preparation.planSources(plan), sharedUnavailable);
    const projected = preparation.projectDetails(related.flatMap(action => rows(action.details)), unavailable);
    const seen = new Set();
    return projected.filter(row => { const key = JSON.stringify(row); if (seen.has(key)) return false; seen.add(key); return true; });
  }
  function node(tag, className, text) {
    const element = document.createElement(tag); element.className = className || '';
    if (text != null) element.textContent = String(text); return element;
  }
  function readable(value) {
    if (value == null || value === '') return '无';
    if (typeof value !== 'object') return String(value);
    if (Array.isArray(value)) return value.map(readable).join('；');
    return Object.entries(value).map(([key, entry]) => `${fields[key] || key}：${readable(entry)}`).join('；');
  }
  function renderChange(parent, change) {
    if (!change) return;
    const detail = node('wa-details', `revision-comparison change-${change.kind}`);
    detail.append(node('summary', '', `${labels[change.kind] || '本轮更新'} · 查看改前 / 改后`));
    const columns = node('div', 'revision-comparison-columns');
    ['before', 'after'].forEach(side => {
      const column = node('div'); column.append(node('strong', '', side === 'before' ? '改前' : '改后'));
      const snapshot = change[side];
      if (!snapshot) column.append(node('p', '', side === 'before' ? '无此安排' : '已移除此安排'));
      else {
        const keys = change.fields?.length ? change.fields : ['title', 'time', 'end_time', 'transport', 'why'];
        [...new Set(['title', ...keys])].forEach(key => {
          const value = ['day', 'position'].includes(key) ? change[`${side}_${key}`] ?? snapshot[key] : snapshot[key];
          column.append(node('p', '', `${fields[key] || key}：${readable(value)}`));
        });
      }
      columns.append(column);
    });
    detail.append(columns); parent.append(detail);
  }
  function renderInline(parent, details) { preparation.renderDetailSummary(parent, details); }
  function renderMessage(parent, result, confirm, options = {}) {
    parent.replaceChildren(); parent.hidden = false;
    if (result.kind === 'clarify') {
      parent.append(node('p', '', result.question));
      [['question', '只问原因'], ['revision', '修改行程']].forEach(([intent, label]) => {
        const button = node('button', 'secondary-button', label); button.type = 'button';
        button.addEventListener('click', () => confirm(intent)); parent.append(button);
      });
    } else {
      parent.append(node('p', 'message-answer', result.answer?.text || '当前材料不足以回答。'));
      rows(result.answer?.judgments).forEach(judgment => {
        if (!['建议', '经验判断'].includes(judgment.label) || typeof judgment.text !== 'string') return;
        const section = node('aside', `message-judgment ${judgment.label === '经验判断' ? 'judgment-experience' : 'judgment-advice'}`);
        section.append(node('strong', '', `${judgment.label} · 非已核实事实`), node('p', '', judgment.text));
        if (judgment.reason) section.append(node('p', 'judgment-reason', `依据：${judgment.reason}`));
        parent.append(section);
      });
      const timeline = rows(result.answer?.timeline);
      const citations = rows(result.answer?.citations).filter(source => !(timeline.length && source.status === 'historical_revision'));
      const evidence = node('wa-details', 'message-evidence');
      const summary = node('summary', '', '查看依据与相关变化');
      summary.setAttribute('slot', 'summary'); evidence.append(summary);
      if (timeline.length) {
        const metadata = node('ul', 'message-timeline');
        timeline.forEach(round => metadata.append(node('li', '', `版本 ${round.base_revision ?? '未知'} → ${round.revision ?? '未知'} · ${Array.isArray(round.changed_days) && round.changed_days.length ? round.changed_days.map(day => `第 ${day} 天`).join('、') : '未记录日期变动'}`)));
        evidence.append(metadata);
      }
      citations.forEach(source => {
        const quote = node('blockquote', '', source.quote); const label = node('span', '', source.label || source.source_id);
        const url = preparation.safeURL(source.url);
        if (url) label.append(options.link ? options.link('查看来源 ↗', url)
          : node('span', '', `来源地址（不可点击）：${url}`));
        quote.append(label); evidence.append(quote);
      });
      if (timeline.length || citations.length) parent.append(evidence);
    }
    if (result.material_notice) parent.append(node('p', '', result.material_notice));
  }
  return { changesForDay, itemChange, removedForDay, itemDetails, renderChange, renderInline, renderMessage };
});
