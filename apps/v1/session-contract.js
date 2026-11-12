'use strict';
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.SessionContract = api;
})(typeof window === 'undefined' ? null : window, function () {
  const text = value => typeof value === 'string' ? value.trim() : '';
  const object = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  function buildIntake(input) {
    const source = object(input);
    const payload = {
      destination: text(source.destination), start_date: text(source.start_date), end_date: text(source.end_date),
      travelers: Number(source.travelers), composition: text(source.composition), pace: text(source.pace),
      preferences: text(source.preferences), brief: text(source.brief), links: [], documents: [], images: []
    };
    if (!payload.destination || !payload.start_date || !payload.end_date || !payload.composition || !payload.pace)
      throw new Error('请填写目的地、日期、人数、同行构成和节奏。');
    if (!Number.isInteger(payload.travelers) || payload.travelers < 1 || payload.travelers > 100)
      throw new Error('人数必须是 1 到 100 的整数。');
    if (payload.start_date > payload.end_date) throw new Error('结束日期不能早于开始日期。');
    if (!payload.brief) delete payload.brief;
    return payload;
  }
  function buildRevision(trip, message, requestId) {
    const value = text(message);
    if (!value) throw new Error('请先写下这次想调整的内容。');
    if (!trip?.id || !Number.isInteger(trip.document?.version) || !trip.document?.hash)
      throw new Error('当前手册版本尚未完整返回，请重新读取后再提交。');
    if (!text(requestId)) throw new Error('缺少本次请求标识，未发送。');
    return { message: value, expected_revision: trip.document.version, expected_hash: trip.document.hash, request_id: requestId };
  }
  function progressRows(trip) {
    const progress = object(trip?.progress), counts = object(progress.tool_counts);
    return [
      ['公开搜索', Number(counts.search) || 0, '次'], ['读取页面', Number(counts.read_page) || 0, '次'],
      ['登记证据', Number(progress.evidence_count) || 0, '条'], ['保存手册', Number(counts.save_handbook) || 0, '次'],
      ['编辑手册', Number(counts.edit_handbook) || 0, '次'], ['完成轮次', Number(progress.completed_turns) || 0, '轮']
    ];
  }
  function safeDocumentURL(value) {
    const url = text(value);
    return /^\/api\/agent\/trips\/[0-9a-f]{32}\/(handbook|pdf)\?read=[A-Za-z0-9_-]+(?:&version=\d+)?$/.test(url)
      || /^\/api\/agent\/trips\/[0-9a-f]{32}\/pdf\?version=\d+&read=[A-Za-z0-9_-]+$/.test(url) ? url : '';
  }
  function receiptSections(receipt) {
    const source = object(receipt), labels = { changes: '改了什么', judgment: '一句判断', alternatives: '备选方案', next_action: '下一步' };
    return Object.keys(labels).filter(key => source[key] != null && source[key] !== '').map(key => ({ key, title: labels[key], value: source[key] }));
  }
  function displayValue(value) {
    if (Array.isArray(value)) return value.map(displayValue).join('；');
    if (value && typeof value === 'object') return Object.entries(value).map(([key, item]) => `${key}：${displayValue(item)}`).join('；');
    return value == null || value === '' ? '未提供' : String(value);
  }
  return { buildIntake, buildRevision, progressRows, safeDocumentURL, receiptSections, displayValue };
});
