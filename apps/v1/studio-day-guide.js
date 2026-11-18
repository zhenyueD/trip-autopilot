'use strict';

// A compact action face and a complete reading layer share the original item.
window.TravelDayGuide = (() => {
  const strings = value => Array.isArray(value) ? value.filter(item => typeof item === 'string' && item.trim()) : [];
  const text = value => typeof value === 'string' ? value : '';
  function node(tag, className, value) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value !== undefined) element.textContent = value;
    return element;
  }
  function group(parent, label, values) {
    const entries = strings(values);
    if (!entries.length) return;
    const chinese = entries.map(window.TravelExperience.chineseText).filter(Boolean);
    if (chinese.length) {
      parent.append(node('h5', '', label));
      const list = node('ul'); chinese.forEach(value => list.append(node('li', '', value))); parent.append(list);
    }
    window.TravelExperience.appendOriginal(parent, `${label}（未翻译）`, entries.filter(value => !window.TravelExperience.chineseText(value)));
  }
  function detailCopy(parent, label, value) {
    if (!text(value).trim()) return;
    if (window.TravelExperience.chineseText(value)) parent.append(node('p', 'item-detail', `${label} · ${text(value)}`));
    else window.TravelExperience.appendOriginal(parent, `${label}（未翻译）`, value);
  }
  function append(body, item, supply, helpers) {
    const guide = item.action_guide || {};
    const chinese = window.TravelExperience.chineseText;
    const action = [...strings(guide.steps), ...strings(item.experience?.what_to_do), text(item.why)].map(chinese).find(Boolean);
    const short = value => window.TravelJourney.shortCopy(value, 100).preview;
    if (action) body.append(node('p', 'handbook-action', short(action)));
    else if (strings(guide.steps).length || strings(item.experience?.what_to_do).length || text(item.why)) {
      body.append(node('p', 'field-hint', '行动说明尚未提供中文版本；可展开查看原文。'));
    }
    if (chinese(item.transport)) body.append(node('p', 'handbook-logistics', `怎么去 · ${short(item.transport)}`));
    if (chinese(guide.meeting_point)) body.append(node('p', 'handbook-entry', `到这里 · ${text(guide.meeting_point)}`));
    const detail = node('wa-details', 'item-detail-drawer');
    detail.append(node('summary', '', '打开行动指南 · 玩法、准备与地点详情'));
    group(detail, '照着做', guide.steps);
    group(detail, '随身带上', guide.bring);
    detailCopy(detail, '预约提醒', guide.booking_note);
    detailCopy(detail, '交通', item.transport);
    detailCopy(detail, '准备', item.preparation);
    detailCopy(detail, '为你这样安排', item.why);
    if (!chinese(guide.meeting_point)) window.TravelExperience.appendOriginal(detail, '集合地点／当地名称（未翻译）', guide.meeting_point);
    window.TravelExperience.appendExperience(detail, item, supply, helpers);
    const photo = detail.querySelector('.place-photo');
    if (photo) { photo.classList.add('handbook-item-photo'); body.append(photo); }
    if (supply && Number.isFinite(supply.lat) && Number.isFinite(supply.lng)) {
      detail.append(helpers.navigationLink('在 Google Maps 查看地点 ↗', `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(`${supply.lat},${supply.lng}`)}`));
      detail.append(node('small', 'field-hint', '来源坐标是地点锚点，不保证为入口；导航与实时路况请以地图服务为准。'));
    }
    body.append(detail);
    return detail;
  }
  return { append };
})();
