'use strict';

// 首页重构 v2 骨架（SPEC-HOMEPAGE-V2-2026-10-12）：毛玻璃导航 / 通知横幅 / Hero / 天卡片横滚。
// 只做 DOM 归位与呈现，不改数据与提交流程；缺槽位（Node 测试环境）时全部静默跳过。
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.TravelChrome = api;
})(typeof window === 'undefined' ? null : window, function () {
  const host = typeof window === 'undefined' ? null : window;
  const $ = id => document.getElementById(id);
  const text = value => typeof value === 'string' || typeof value === 'number' ? String(value).trim() : '';
  const list = value => Array.isArray(value) ? value : [];
  const rows = value => list(value).filter(item => item && typeof item === 'object' && !Array.isArray(item));
  const node = (tag, className, value) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value !== undefined) element.textContent = text(value);
    return element;
  };
  const icon = (name, size = 20) => (typeof window !== 'undefined' && host?.TravelIcons?.svg(name, size)) || null;

  let mounted = false;
  let callbacks = {};
  let bannerDismissedKey = ''; // 用户关掉的是哪一条横幅；新修订出现时重新出示
  let stripDay = 0;

  // ---------- 导航：tabs 进毛玻璃 masthead，低频入口收 ⋯ ----------
  function mountNav() {
    const masthead = document.querySelector('.masthead');
    const nav = $('handbook-nav');
    if (!masthead || !nav) return;
    let slot = $('chrome-tabs-slot');
    if (!slot) {
      slot = node('div', 'chrome-tabs-slot');
      slot.id = 'chrome-tabs-slot';
      const context = masthead.querySelector('.header-context');
      if (context) masthead.insertBefore(slot, context);
      else masthead.append(slot);
    }
    slot.append(nav);
    // ⋯ 溢出菜单：新的旅行 / 最近旅行
    const overflow = node('wa-details', 'chrome-overflow');
    overflow.id = 'chrome-overflow';
    const summary = node('summary', 'chrome-overflow-button');
    summary.setAttribute('aria-label', '更多操作');
    const dots = icon('ellipsis', 20);
    if (dots) summary.append(dots);
    else summary.textContent = '⋯';
    const menu = node('div', 'chrome-overflow-menu');
    ['new-trip', 'recent-toggle'].forEach(id => { const control = $(id); if (control) menu.append(control); });
    overflow.append(summary, menu);
    const actions = nav.querySelector('.handbook-actions');
    if (actions) nav.append(actions); // actions 保持在 nav 末尾（右侧）
    masthead.append(overflow);
    document.addEventListener('click', event => {
      if (overflow.open && !overflow.contains(event.target)) overflow.open = false;
    });
  }

  // ---------- 通知横幅：回执的 transient 形态（可关闭）；坞里是 persistent 形态 ----------
  function mountBanner() {
    if ($('chrome-notice')) return;
    const banner = node('wa-callout', 'chrome-notice');
    banner.id = 'chrome-notice';
    banner.hidden = true;
    banner.setAttribute('variant', 'brand');
    banner.setAttribute('role', 'status');
    const copy = node('p', 'chrome-notice-text');
    copy.id = 'chrome-notice-text';
    const open = node('button', 'chrome-notice-open', '查看具体变化');
    open.id = 'chrome-notice-open';
    open.type = 'button';
    open.setAttribute('aria-expanded', 'false');
    open.setAttribute('aria-controls', 'chrome-impact-panel');
    const close = node('button', 'chrome-notice-close');
    close.id = 'chrome-notice-close';
    close.type = 'button';
    close.setAttribute('aria-label', '关闭这条通知');
    const x = icon('close', 16);
    if (x) close.append(x);
    else close.textContent = '×';
    banner.append(copy, open, close);
    const panel = node('div', 'chrome-impact-panel');
    panel.id = 'chrome-impact-panel';
    panel.hidden = true;
    const masthead = document.querySelector('.masthead');
    if (masthead) { masthead.after(banner); banner.after(panel); }
    open.addEventListener('click', () => {
      panel.hidden = !panel.hidden;
      open.setAttribute('aria-expanded', String(!panel.hidden));
      // 横幅链终点：展开即定位到回执卡并高亮 1.2s，随后高亮消失。
      if (!panel.hidden) {
        panel.scrollIntoView?.({ behavior: 'smooth', block: 'nearest' });
        panel.classList.add('is-highlighted');
        setTimeout(() => panel.classList.remove('is-highlighted'), 1200);
      }
    });
    close.addEventListener('click', () => {
      banner.hidden = true;
      panel.hidden = true;
      open.setAttribute('aria-expanded', 'false');
      bannerDismissedKey = banner.dataset.impactKey || '';
      host?.TravelDock?.evaluate?.(); // 横幅退场后，坞里的常驻回执可以出现
    });
  }

  // 把 journey 渲染好的 #replan-impact 内容接进横幅/下拉，原模块不再作为大卡片出现。
  function syncImpact() {
    const impact = $('replan-impact');
    const banner = $('chrome-notice');
    if (!impact || !banner) return;
    const panel = $('chrome-impact-panel');
    if (panel && impact.parentElement !== panel) panel.append(impact);
    const heading = impact.querySelector('h2');
    const preview = impact.querySelector('p');
    const key = `${text(heading?.textContent)}|${text(preview?.textContent)}`;
    if (impact.hidden || !key.trim() || key === '|') { banner.hidden = true; return; }
    banner.dataset.impactKey = key;
    const target = $('chrome-notice-text');
    // 横幅只说一句话（杀产品黑话）；理由与明细留在「查看具体变化」下拉里。
    if (target) target.textContent = text(heading?.textContent) || text(preview?.textContent);
    impact.hidden = false; // 移入下拉面板后保持可读；面板本身控制显隐
    if (key !== bannerDismissedKey) banner.hidden = false;
    host?.TravelDock?.evaluate?.(); // 同屏不说两遍：横幅在，坞里细条收
  }

  function bannerVisible() {
    const banner = $('chrome-notice');
    return Boolean(banner && !banner.hidden);
  }

  // ---------- Hero + 天卡片 ----------
  function mountHero() {
    const workspace = $('workspace');
    if (!workspace || $('chrome-hero')) return;
    const hero = node('section', 'chrome-hero');
    hero.id = 'chrome-hero';
    hero.setAttribute('aria-label', '这趟旅行');
    const eyebrow = node('p', 'chrome-hero-eyebrow');
    eyebrow.id = 'chrome-hero-eyebrow';
    const title = node('h1', 'chrome-hero-title', '你的旅行安排');
    title.id = 'chrome-hero-title';
    const name = node('p', 'chrome-hero-trip');
    name.id = 'chrome-hero-trip';
    hero.append(eyebrow, title, name);
    workspace.prepend(hero);
    const strip = node('section', 'chrome-day-strip');
    strip.id = 'chrome-day-strip';
    strip.setAttribute('aria-label', '选择某一天');
    strip.hidden = true;
    hero.after(strip);
  }

  function dayImageURL(day, suppliesById) {
    for (const item of rows(day?.items)) {
      const url = text(suppliesById?.get?.(item.supply_id)?.image_url);
      if (url) return url;
    }
    return '';
  }

  // 天卡片：照片出血 + 底部渐变蒙层 + 白字压图；无图灰渐变占位（不伪造）。
  function dayCard(day, index, options = {}) {
    const card = node('button', 'day-card');
    card.classList.add('no-image');
    card.type = 'button';
    card.dataset.dayIndex = String(index);
    const media = node('span', 'day-card-media');
    if (options.imageURL) {
      const img = node('img', 'day-card-img');
      img.src = options.imageURL;
      img.alt = '';
      img.loading = 'lazy';
      img.decoding = 'async';
      img.referrerPolicy = 'no-referrer';
      img.addEventListener('load', () => card.classList.remove('no-image'), { once: true });
      img.addEventListener('error', () => { img.remove(); card.classList.add('no-image'); }, { once: true });
      media.append(img);
    }
    media.append(node('span', 'day-card-scrim'));
    const body = node('span', 'day-card-body');
    body.append(node('span', 'day-card-day', `第 ${text(day?.day) || index + 1} 天`),
      node('span', 'day-card-city', text(day?.title) || text(day?.city) || '当天安排'));
    const badge = node('span', 'day-card-badge');
    const check = icon('check', 16);
    if (check) badge.append(check);
    card.append(media, body, badge);
    if (options.selected) card.classList.add('selected');
    if (options.hasChange) card.classList.add('has-change');
    card.setAttribute('aria-current', String(Boolean(options.selected)));
    card.setAttribute('aria-label', `第 ${text(day?.day) || index + 1} 天 · ${text(day?.title) || '当天安排'}`);
    if (options.onSelect) card.addEventListener('click', () => options.onSelect(index));
    return card;
  }

  // D 天卡横滑：wa-carousel 承载天卡。--scroll-hint 露下一张、拖拽+侧箭头、
  // wa-slide-change 驱动选中态；重渲染后 goToSlide 瞬时回到选中卡（选中项可见）。
  function dayCarousel(days, options = {}) {
    const carousel = node('wa-carousel', 'day-carousel');
    carousel.setAttribute('mouse-dragging', '');
    carousel.setAttribute('navigation', '');
    carousel.setAttribute('aria-label', '选择某一天');
    let current = options.selected;
    const syncSelected = next => {
      current = next;
      [...(carousel.children || [])].forEach((item, at) => {
        const card = item.children?.[0] || item;
        card.classList?.toggle?.('selected', at === next);
        card.setAttribute?.('aria-current', String(at === next));
      });
    };
    rows(days).forEach((day, index) => {
      const item = node('wa-carousel-item');
      item.append(dayCard(day, index, {
        selected: index === current,
        hasChange: options.hasChange?.(day, index),
        imageURL: options.imageURL?.(day, index),
        onSelect: next => { syncSelected(next); options.onSelect?.(next); }
      }));
      carousel.append(item);
    });
    // 拖拽/方向键停下即选中（与点击同一条链），防抖：与当前一致不回抛。
    // 关键闸门：slide-change 只在用户亲手驱动（触摸/拖拽/方向键/侧箭头）时采信。
    // 多卡同屏时 activeSlide = 首张可见卡，重建后的自动定位也会发 slide-change，
    // 不设闸门会把刚选的卡打回（实测：点第 5 天被 auto-position 的虚报改成第 3 天）。
    let userDriven = false;
    const markDriven = () => { userDriven = true; };
    carousel.addEventListener('pointerdown', markDriven, true); // 触摸/拖拽起点
    carousel.addEventListener('mousedown', markDriven, true);   // 兼容不产 pointer 事件的环境
    carousel.addEventListener('touchstart', markDriven, true);
    carousel.addEventListener('click', event => { if (event.target === carousel) markDriven(); }, true); // shadow 侧箭头 retarget 到 host
    carousel.addEventListener('keydown', event => {
      if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) { markDriven(); return; }
      // End/Home 语义归选择器：多卡同屏下组件的 goToSlide 末页首卡 ≠ 最后一天
      if (event.key === 'End' || event.key === 'Home') {
        event.preventDefault?.(); event.stopPropagation?.();
        const index = event.key === 'End' ? rows(days).length - 1 : 0;
        if (index !== current) { syncSelected(index); options.onSelect?.(index); }
      }
    }, true);
    carousel.addEventListener('wa-slide-change', event => {
      const driven = userDriven;
      userDriven = false; // 一次驱动只配一次落定，残留标记会放走虚报
      const index = Number(event?.detail?.index);
      if (!driven || !Number.isInteger(index) || index === current) return;
      syncSelected(index);
      options.onSelect?.(index);
    });
    if (Number.isInteger(current) && current > 0 && typeof requestAnimationFrame === 'function') {
      requestAnimationFrame(() => carousel.goToSlide?.(current, 'auto'));
    }
    return carousel;
  }

  function renderStrip(plan) {
    const strip = $('chrome-day-strip');
    if (!strip) return;
    const days = rows(plan?.days);
    strip.hidden = !days.length;
    if (!days.length) { strip.replaceChildren(); return; }
    const suppliesById = new Map(rows(plan?.supplies).map(supply => [supply.id, supply]));
    strip.replaceChildren(dayCarousel(days, {
      selected: stripDay,
      imageURL: day => dayImageURL(day, suppliesById),
      onSelect: next => {
        stripDay = next;
        callbacks.onDay?.(next);
        // 选天链（P0-14 #4）：总览里 strip 驱动下方当天区块并平滑滚动，meta 行由 delivery 同步。
        const overview = $('handbook-overview');
        const stage = $('delivery-selected-day');
        if (overview && !overview.hidden && host?.TravelDelivery?.selectDay) {
          host.TravelDelivery.selectDay(overview, next);
          stage?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
        } else {
          host?.TravelHandbook?.selectView?.('daily');
        }
      }
    }));
  }

  function render(plan, options = {}) {
    if (!mounted) return;
    if (Number.isInteger(options.day)) stripDay = options.day;
    const days = rows(plan?.days);
    const count = days.reduce((sum, day) => sum + rows(day.items).length, 0);
    const eyebrow = $('chrome-hero-eyebrow');
    if (eyebrow) eyebrow.textContent = days.length ? `${days.length} 天 · ${count} 段安排` : '行程仍在补充';
    const title = $('chrome-hero-title');
    if (title) title.textContent = text(plan?.title) || '你的旅行安排';
    const name = $('chrome-hero-trip');
    if (name) name.hidden = true;
    renderStrip(plan);
    syncImpact();
  }

  // Hero 与总览天卡片只住在「全程总览」；每日指南的天选择就是 #day-nav 卡片本身。
  function syncView(view) {
    const hero = $('chrome-hero');
    const strip = $('chrome-day-strip');
    if (hero) hero.hidden = view !== 'overview';
    if (strip) strip.hidden = view !== 'overview' || !strip.children.length;
    // 总览里 Hero 已是视觉重心：旧 story / delivery 的重复标题由 CSS 退场（内容保留）。
    const workspace = $('workspace');
    if (workspace) workspace.classList.toggle('chrome-overview', view === 'overview');
  }

  function mount(options = {}) {
    callbacks = options;
    if (mounted || typeof document === 'undefined') return;
    mountNav();
    mountBanner();
    mountHero();
    // 展开行链（P0-14 #6）：iOS 设置行手风琴，同层一次只展开一项。
    // wa-details 不发 toggle，改听 wa-show；原生 details（落地页）继续走 toggle。
    document.addEventListener?.('toggle', event => closeSiblings(event.target), true);
    document.addEventListener?.('wa-show', event => closeSiblings(event.target), true);
    mounted = true;
  }

  // 手风琴：target 展开时关掉同层其他展开行（不动嵌套父子）。
  function closeSiblings(target) {
    if (!target || target.open !== true || typeof target.closest !== 'function') return;
    const scope = target.closest('#timeline, .delivery-stage');
    if (!scope || typeof scope.querySelectorAll !== 'function') return;
    scope.querySelectorAll('details[open], wa-details[open]').forEach(other => {
      if (other !== target && !other.contains(target) && !target.contains(other)) other.open = false;
    });
  }

  return { mount, render, syncView, syncImpact, bannerVisible, dayCard, dayCarousel, dayImageURL, closeSiblings };
});
