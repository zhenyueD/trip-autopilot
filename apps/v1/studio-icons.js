'use strict';

// 线性 icon 系统（首页重构 v2）：SF Symbols 气质，1.5px 描边、圆角端点、16/20 两档。
// 全部内联 SVG，不走 sprite 外链；emoji 不再出现在工作室界面。
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.TravelIcons = api;
})(typeof window === 'undefined' ? null : window, function () {
  const NS = 'http://www.w3.org/2000/svg';
  // viewBox 一律 24；path 用 currentColor 描边，填充只给点缀（ellipsis）。
  const ICONS = {
    pin: '<path d="M12 21s-6.8-5.2-6.8-10.7a6.8 6.8 0 1 1 13.6 0C18.8 15.8 12 21 12 21Z"/><circle cx="12" cy="10.2" r="2.4"/>',
    clock: '<circle cx="12" cy="12" r="8.6"/><path d="M12 7.2v5l3.2 2.1"/>',
    walk: '<circle cx="13" cy="5" r="1.9"/><path d="M13 8.5 10.5 11l-1 3.5M13 8.5l2 2.5 2.5.8M13 8.5l-.8 5 2.3 3 .9 4M12.2 13.5l-2.7 3-1.4 4"/>',
    car: '<path d="M5 16.5v-4l1.6-4a2 2 0 0 1 1.9-1.3h7a2 2 0 0 1 1.9 1.3l1.6 4v4"/><path d="M5 12.5h14"/><circle cx="7.6" cy="16.5" r="1.7"/><circle cx="16.4" cy="16.5" r="1.7"/>',
    food: '<path d="M7 3v7a2 2 0 0 0 2 2v9M7 3v4M10 3v4M9 10a2 2 0 0 0 2-2V3"/><path d="M17 3c-1.7 1.2-2.5 3.4-2.5 5.5 0 2 1 3.5 2.5 3.5v9"/>',
    ticket: '<path d="M4 8a2 2 0 0 0 2-2h12a2 2 0 0 0 2 2v2.5a2.5 2.5 0 0 0 0 5V18a2 2 0 0 0-2 2H6a2 2 0 0 0-2-2v-2.5a2.5 2.5 0 0 0 0-5V8Z"/><path d="M13.5 7v2m0 3v2m0 3v2" stroke-dasharray="2 3"/>',
    lock: '<rect x="5.5" y="10.5" width="13" height="9.5" rx="2.4"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/>',
    check: '<path d="M5 12.8l4.3 4.2L19 7.2"/>',
    warn: '<path d="M12 4 21 19.5H3L12 4Z"/><path d="M12 10v4"/><circle cx="12" cy="16.8" r="0.4" fill="currentColor"/>',
    question: '<circle cx="12" cy="12" r="8.6"/><path d="M9.6 9.4a2.5 2.5 0 1 1 3.3 2.4c-.8.35-.9 1-.9 1.7"/><circle cx="12" cy="16.8" r="0.4" fill="currentColor"/>',
    close: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
    chevron: '<path d="M9.5 6l6 6-6 6"/>',
    'chevron-down': '<path d="M6 9.5l6 6 6-6"/>',
    sparkle: '<path d="M12 3.5 13.9 9l5.6 1.9-5.6 1.9L12 18.5l-1.9-5.7L4.5 10.9 10.1 9 12 3.5Z"/>',
    ellipsis: '<circle cx="5.5" cy="12" r="1.3" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.3" fill="currentColor" stroke="none"/><circle cx="18.5" cy="12" r="1.3" fill="currentColor" stroke="none"/>',
    map: '<path d="M9 4.5 3.5 6.3v13.2L9 17.7l6 1.8 5.5-1.8V4.5L15 6.3l-6-1.8Z"/><path d="M9 4.5v13.2M15 6.3v13.2"/>',
    external: '<path d="M7 17 17 7M9.5 7H17v7.5"/>'
  };
  const names = () => Object.keys(ICONS);

  function svg(name, size = 20) {
    const inner = ICONS[name];
    if (!inner || typeof document === 'undefined') return null;
    const el = document.createElementNS(NS, 'svg');
    el.setAttribute('viewBox', '0 0 24 24');
    el.setAttribute('width', String(size));
    el.setAttribute('height', String(size));
    el.setAttribute('fill', 'none');
    el.setAttribute('stroke', 'currentColor');
    el.setAttribute('stroke-width', '1.5');
    el.setAttribute('stroke-linecap', 'round');
    el.setAttribute('stroke-linejoin', 'round');
    el.setAttribute('aria-hidden', 'true');
    el.classList.add('icon', `icon-${name}`);
    el.innerHTML = inner;
    return el;
  }

  return { icons: ICONS, names, svg };
});
