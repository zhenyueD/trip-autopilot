'use strict';

// Single-session delivery receipt: the backend stores the real revision outcome
// in document.receipt (changes / judgment / alternatives / next_action) plus a
// unified diff in document.diff. This renders them after a completed revision;
// it never invents a receipt and hides entirely before the first revision.
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.TravelDeliveryReceipt = api;
})(typeof window === 'undefined' ? null : window, function () {
  const text = value => typeof value === 'string' || typeof value === 'number' ? String(value).trim() : '';
  const rows = value => Array.isArray(value) ? value.filter(item => item && typeof item === 'object' && !Array.isArray(item)) : [];
  const labels = { summary: '概述', kind: '判定', reason: '理由', message: '说明', field: '字段', value: '内容', status: '状态' };

  function readable(value, depth = 0) {
    if (value == null || value === '') return '无';
    if (typeof value !== 'object') return text(value);
    if (Array.isArray(value)) return value.length ? value.map(item => readable(item, depth + 1)).join('；') : '无';
    return Object.entries(value).map(([field, entry]) => `${labels[field] || field}：${readable(entry, depth + 1)}`).join('；') || '无';
  }

  function render(container, trip, doc = globalThis.document) {
    if (!container || !doc) return false;
    const document_ = trip?.document;
    const receipt = document_?.receipt;
    if (!trip?.capabilities?.single_session || !receipt || !Number.isInteger(document_.version) || document_.version <= 1) {
      container.hidden = true; container.replaceChildren(); return false;
    }
    const create = (tag, className, content) => {
      const node = doc.createElement(tag);
      if (className) node.className = className;
      if (content !== undefined) node.textContent = content;
      return node;
    };
    const heading = create('div', 'receipt-heading');
    heading.append(create('p', 'folio', '修订回执'), create('h2', '', `这次调整的结果 · 版本 v${document_.version}`));
    const sections = [['改了什么', receipt.changes], ['判断依据', receipt.judgment],
      ['其他可选做法', receipt.alternatives], ['下一步', receipt.next_action]]
      .filter(([, value]) => value != null && value !== '');
    const body = create('div', 'receipt-body');
    if (!sections.length) body.append(create('p', '', '这次修订已完成，服务端未返回分项回执。'));
    for (const [title, value] of sections) {
      const section = create('section', 'receipt-section');
      section.append(create('h3', '', title), create('p', '', readable(value)));
      body.append(section);
    }
    const diffText = text(trip.diff ?? document_.diff);
    if (diffText) {
      const details = create('details', 'receipt-diff');
      details.append(create('summary', '', '查看改前 / 改后（完整差异）'), create('pre', '', diffText));
      body.append(details);
    }
    container.replaceChildren(heading, body);
    container.hidden = false;
    return true;
  }

  return { render, readable };
});
