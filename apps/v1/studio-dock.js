'use strict';
// 对话坞（PRD-工作室前端交互重构 v0.2 · P0）：系统的一个声音。
// 回执 / 建议 / 问答答复汇入同一条对话流，输入框常驻底部；
// 事件进展降级为一行状态条；档案抽屉收编材料、来源、研究记录与偏好。
// 只做 DOM 归位与状态呈现，不改变任何数据与提交流程。
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.TravelDock = api;
})(typeof window === 'undefined' ? null : window, function () {
  const $ = id => document.getElementById(id);
  let mounted = false;
  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function retitle(details, text) {
    const summary = details && typeof details.querySelector === 'function' ? details.querySelector('summary') : null;
    if (summary) summary.textContent = text;
  }
  function mount() {
    if (mounted || !$('feedback-section') || !$('workspace-records')) return;
    const dock = node('section', 'chat-dock');
    dock.id = 'chat-dock';
    dock.setAttribute('aria-label', '与旅行顾问的对话');
    dock.hidden = true;
    const status = node('div', 'dock-status');
    status.id = 'dock-status';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    status.hidden = true;
    const statusText = node('span', 'dock-status-text');
    statusText.id = 'dock-status-text';
    status.append(statusText);
    if ($('revision-stage')) status.append($('revision-stage'));
    const stream = node('div', 'dock-stream');
    stream.id = 'dock-stream';
    stream.setAttribute('aria-label', '对话记录');
    ['revision-receipt', 'message-response', 'plan-suggestions'].forEach(id => {
      if ($(id)) stream.append($(id));
    });
    // 待指派材料收进补充材料区，保持输入行清爽（PRD：补材料入口收进 + 号位）。
    // 先取引用：feedback-section 移入未挂树的 dock 后，getElementById 找不到其子孙。
    const pending = $('pending-materials');
    const revisionMaterialsDetails = $('revision-materials');
    dock.append(status, stream, $('feedback-section'));
    if (pending && revisionMaterialsDetails) revisionMaterialsDetails.append(pending);
    document.body.append(dock);
    // 悬浮坞的高度随建议、材料与窄屏换行变化，正文避让实际高度。
    if (typeof ResizeObserver !== 'undefined') {
      new ResizeObserver(() => {
        const height = dock.getBoundingClientRect().height;
        const bottom = parseFloat(getComputedStyle(dock).bottom) || 0;
        document.documentElement.style.setProperty('--dock-clearance', `${Math.ceil(height + bottom + 24)}px`);
      }).observe(dock);
    }
    // v2 指令面板形态：默认紧凑（回执细条 + 输入行）；有待答内容时向上展开。
    const receiptLine = node('div', 'dock-receipt-line');
    receiptLine.id = 'dock-receipt-line';
    receiptLine.hidden = true;
    receiptLine.setAttribute('role', 'button');
    receiptLine.tabIndex = 0;
    receiptLine.setAttribute('aria-controls', 'dock-stream');
    receiptLine.setAttribute('aria-expanded', 'false');
    const toggle = node('button', 'dock-toggle');
    toggle.id = 'dock-toggle';
    toggle.type = 'button';
    toggle.setAttribute('aria-label', '展开或收起对话记录');
    toggle.setAttribute('aria-expanded', 'false');
    const chevron = typeof window !== 'undefined' && window.TravelIcons?.svg('chevron', 16);
    toggle.append(chevron || '›');
    dock.append(receiptLine, toggle); // DOM 追加在尾部，视觉顺序由 CSS order 决定（兼容测试桩）
    // ✦ 描线 icon 进输入框左侧（不是 Unicode 符号）。
    const field = $('feedback');
    if (field) {
      const wrap = node('div', 'dock-input-wrap');
      field.before(wrap);
      wrap.append(field);
      const spark = typeof window !== 'undefined' && window.TravelIcons?.svg('sparkle', 20);
      if (spark) wrap.prepend(spark);
    }
    if (typeof MutationObserver !== 'undefined') {
      new MutationObserver(() => evaluate()).observe(stream,
        { childList: true, subtree: true, attributes: true, attributeFilter: ['hidden'] });
    }
    const toggleStream = () => {
      manualOverride = !dock.classList.contains('is-expanded');
      evaluate();
    };
    toggle.addEventListener('click', toggleStream);
    receiptLine.addEventListener('click', toggleStream);
    receiptLine.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toggleStream(); }
    });
    // P0-0：整个坞可收成悬浮 pill（56px 毛玻璃圆钮），状态进 sessionStorage。
    const collapse = node('button', 'dock-collapse');
    collapse.id = 'dock-collapse';
    collapse.type = 'button';
    collapse.setAttribute('aria-label', '收起对话坞为悬浮按钮');
    const down = typeof window !== 'undefined' && window.TravelIcons?.svg('chevron-down', 16);
    collapse.append(down || '⌄');
    collapse.addEventListener('click', () => { setCollapsed(true); evaluate(); });
    dock.append(collapse);
    const pill = node('button', 'dock-pill');
    pill.id = 'dock-pill';
    pill.type = 'button';
    pill.hidden = true;
    pill.setAttribute('aria-label', '展开对话坞');
    const pillSpark = typeof window !== 'undefined' && window.TravelIcons?.svg('sparkle', 20);
    pill.append(pillSpark || '✦');
    const badge = node('span', 'dock-pill-badge');
    badge.id = 'dock-pill-badge';
    badge.hidden = true;
    pill.append(badge);
    // 有待查看内容时，一次点击直接可达；无内容时只打开输入区。
    pill.addEventListener('click', () => { setCollapsed(false); manualOverride = true; evaluate(); });
    pill.addEventListener?.('animationend', () => pill.classList.remove('is-breathing'));
    document.body.append(pill);
    try {
      const saved = storage()?.getItem('dock-collapsed');
      collapsePreference = saved === '1' ? true : saved === '0' ? false : null;
    } catch { collapsePreference = null; }
    setCollapsed(collapsePreference !== false, false);
    dock.addEventListener('input', evaluate);
    dock.addEventListener('change', evaluate);
    dock.addEventListener('focusin', evaluate);
    dock.addEventListener('focusout', () => setTimeout(evaluate, 0));
    dock.addEventListener('submit', () => { sessionActivity = true; }, true);
    // 档案抽屉：材料与来源（原有内容）+ 偏好表单 + 任务进展 + 问答记录。
    const records = $('workspace-records');
    records.classList.add('archive-drawer');
    retitle(records, '档案 · 材料、来源、研究记录与偏好');
    if ($('traveler-profile')) {
      $('traveler-profile').classList.add('archive-item');
      records.append($('traveler-profile'));
    }
    if ($('event-details')) {
      $('event-details').classList.add('archive-item');
      retitle($('event-details'), '任务进展记录');
      records.append($('event-details'));
    }
    const qa = node('wa-details', 'archive-item qa-history');
    qa.id = 'qa-history';
    qa.hidden = true;
    qa.append(node('summary', '', '本次打开的问答记录'));
    const list = node('div', 'qa-history-list');
    list.id = 'qa-history-list';
    qa.append(node('p', 'field-hint', '只保留本次打开期间的问答，刷新后清空；行程与材料不受影响。'), list);
    records.append(qa);
    mounted = true;
  }
  function show(value) {
    const dock = $('chat-dock');
    if (dock) dock.hidden = !value;
    if (!value) { working = false; sessionActivity = false; newResponse = false; lastResponseKey = ''; }
    if (document.body && document.body.classList) document.body.classList.toggle('has-chat-dock', Boolean(value));
    evaluate();
  }
  let manualOverride = null;
  let collapsed = false;
  let collapsePreference = null;
  let working = false, sessionActivity = false, newResponse = false;
  let lastResponseKey = '';
  let lastKey = '';
  const storage = () => {
    try { return typeof sessionStorage === 'undefined' ? null : sessionStorage; } catch { return null; }
  };
  function setCollapsed(value, persist = true) {
    collapsed = Boolean(value);
    if (persist) collapsePreference = collapsed;
    const dock = $('chat-dock');
    const pill = $('dock-pill');
    if (dock) dock.classList.toggle('is-collapsed', collapsed);
    const hidePill = !collapsed || Boolean(dock?.hidden);
    if (pill && pill.hidden !== hidePill) pill.hidden = hidePill;
    if (document.body?.classList) document.body.classList.toggle('dock-is-collapsed', collapsed);
    if (persist) { try { storage()?.setItem('dock-collapsed', collapsed ? '1' : '0'); } catch {} }
  }
  const hasContent = element => Boolean(element && !element.hidden && element.textContent?.trim());
  function hasDraft() {
    return ['feedback', 'revision-links', 'revision-evidence'].some(id => Boolean($(id)?.value?.trim()))
      || ['revision-image-list', 'revision-document-list'].some(id => Boolean($(id)?.children?.length));
  }
  function evaluate() {
    const dock = $('chat-dock');
    const stream = $('dock-stream');
    if (!dock || !stream) return;
    const host = typeof window === 'undefined' ? null : window;
    // 同屏不说两遍：横幅（transient）在场时回执由它表达，坞不展开、不抢话；横幅关闭后坞补位。
    const bannerUp = Boolean(host?.TravelChrome?.bannerVisible?.());
    // 先归位回执本体显隐（按文字而非 hidden 判断，否则横幅关后回执永远醒不过来），再算待答。
    const receipt = $('revision-receipt');
    const receiptHasText = Boolean(receipt && String(receipt.textContent || '').trim());
    if (receipt) {
      const shouldHide = bannerUp || !receiptHasText;
      if (receipt.hidden !== shouldHide) receipt.hidden = shouldHide;
    }
    const has = id => hasContent($(id)) && !(bannerUp && id === 'revision-receipt');
    // 值得展开的是 transient 答复（回执、问答答复）；常驻建议/提问卡安静待在收起钮后面，不抢首屏。
    const loud = ['revision-receipt', 'message-response'].some(has);
    const quiet = ['plan-suggestions', 'reaction-section'].some(has);
    const pending = loud || quiet;
    const responseKey = ['revision-receipt', 'message-response']
      .map(id => hasContent($(id)) ? $(id).textContent : '').join('\n');
    // 首次恢复的历史内容不算新消息；只有本会话提交/处理之后到达的新答复才自动打开。
    if (loud && sessionActivity && responseKey !== lastResponseKey) {
      newResponse = true; sessionActivity = false;
    }
    if (!pending) newResponse = false;
    lastResponseKey = responseKey;
    const editing = hasDraft() || Boolean(dock.contains?.(document.activeElement));
    setCollapsed(collapsePreference === null ? !(editing || working || newResponse) : collapsePreference, false);
    // 三态：null 自动（loud 才展开）/ true 用户手动展开 / false 用户手动收起。
    const expanded = !collapsed && pending && (manualOverride === true || (loud && manualOverride !== false));
    if (!pending) manualOverride = null; // 新内容到达时恢复自动展开
    dock.classList.toggle('is-expanded', expanded);
    // 收起态：pill 上有未读 badge；内容指纹变化时呼吸一次（不自动弹开打断阅读）。
    const pill = $('dock-pill');
    const badge = $('dock-pill-badge');
    if (pill) {
      const label = pending ? '展开对话坞，有待查看的回答或建议' : working ? '展开对话坞，旅行正在处理中' : '展开对话坞';
      pill.setAttribute('aria-label', label); pill.title = label;
    }
    if (pill && badge) {
      const unread = collapsed && (pending || working);
      if (badge.hidden !== !unread) badge.hidden = !unread;
      const key = ['revision-receipt', 'message-response', 'plan-suggestions', 'reaction-section']
        .map(id => { const el = $(id); return hasContent(el) ? (el.textContent || '').length : 0; }).join('/');
      if (collapsed && pending && key !== lastKey) pill.classList.add('is-breathing');
      lastKey = key;
    }
    // 只在值变化时写 hidden：同值赋值仍会触发 MutationObserver（attributes），造成无限回环。
    if (stream.hidden !== !expanded) stream.hidden = !expanded;
    const toggle = $('dock-toggle');
    if (toggle) {
      if (toggle.hidden !== !pending) toggle.hidden = !pending;
      if (toggle.getAttribute('aria-expanded') !== String(expanded)) toggle.setAttribute('aria-expanded', String(expanded));
    }
    // 回执细条：横幅在场时不复读；展开时全文已在流里，细条收起。
    const line = $('dock-receipt-line');
    if (!line) return;
    const visibleCopy = (() => {
      if (hasContent(receipt) && !bannerUp) {
        const heading = receipt.querySelector('h2, h3, strong');
        const copy = (heading?.textContent || receipt.textContent || '').trim();
        return copy.length > 80 ? `${copy.slice(0, 80)}…` : copy;
      }
      if (quiet) {
        // 「有 N 条建议 ›」：建议卡与提问卡里的可点项计数（P0-10 #14）。
        const count = ['plan-suggestions', 'reaction-section'].reduce((sum, id) => {
          const element = $(id);
          if (!hasContent(element) || typeof element.querySelectorAll !== 'function') return sum;
          return sum + element.querySelectorAll('button').length;
        }, 0);
        return count ? `有 ${count} 条建议 ›` : '还有建议或提问，点开看看。';
      }
      return '';
    })();
    const visible = Boolean(visibleCopy) && !expanded;
    line.setAttribute('aria-expanded', String(expanded));
    if (line.hidden !== !visible) line.hidden = !visible;
    if (visible) line.textContent = visibleCopy;
  }
  function setStatus(text) {
    const status = $('dock-status');
    if (!status) return;
    status.hidden = !text;
    const target = $('dock-status-text') || status;
    target.textContent = text || '';
    working = Boolean(text);
    if (working) sessionActivity = true;
    const dock = $('chat-dock');
    if (dock) dock.classList.toggle('is-working', Boolean(text));
    // agent 工作时坞若处于收起态：pill 呼吸一次 + 亮 badge，不弹开。
    if (collapsed && text) {
      const pill = $('dock-pill');
      const badge = $('dock-pill-badge');
      if (pill) pill.classList.add('is-breathing');
      if (badge) badge.hidden = false;
    }
    evaluate();
  }
  function scrollToLatest() {
    const stream = $('dock-stream');
    if (stream) stream.scrollTop = 0;
  }
  return { mount, show, setStatus, scrollToLatest, evaluate, setCollapsed, isCollapsed: () => collapsed };
});
