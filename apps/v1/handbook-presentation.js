(function attachHandbookPresentation(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.HandbookPresentation = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function handbookPresentationFactory() {
  'use strict';

  const OVERVIEW_SELECTOR = '[data-hp-overview]';

  function element(doc, tag, className, text) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function cleanText(value) {
    return String(value || '').replace(/\s+/g, ' ').trim();
  }

  function cloneWithoutIds(node) {
    const clone = node.cloneNode(true);
    if (clone.removeAttribute) clone.removeAttribute('id');
    clone.querySelectorAll?.('[id]').forEach((child) => child.removeAttribute('id'));
    return clone;
  }

  function heading(doc, eyebrow, title, copy) {
    const header = element(doc, 'header', 'hp-section-heading');
    header.append(element(doc, 'p', 'hp-eyebrow', eyebrow));
    header.append(element(doc, 'h2', '', title));
    if (copy) header.append(element(doc, 'p', 'hp-section-copy', copy));
    return header;
  }

  function notice(doc, text) {
    return element(doc, 'p', 'hp-notice', text);
  }

  function sourceDecision(doc, target) {
    const source = doc.querySelector('.trip-decision');
    if (source) {
      const clone = cloneWithoutIds(source);
      const children = Array.from(clone.children);
      children.slice(6).forEach((child) => child.remove());
      clone.classList.add('hp-source-decision');
      target.append(clone);
      if (children.length > 6) target.append(element(doc, 'p', 'hp-source-note', '第一眼仅摘引原文前段；完整判断与证据保留在深入层。'));
      return;
    }
    const title = Array.from(doc.querySelectorAll('.page h2')).find((node) => /^为什么.*设计/.test(cleanText(node.textContent)));
    const list = title?.nextElementSibling;
    if (title && list?.matches('ul')) {
      const clone = element(doc, 'section', 'hp-source-decision');
      clone.append(cloneWithoutIds(title), cloneWithoutIds(list));
      target.append(clone);
      return;
    }
    target.append(notice(doc, '原始手册没有独立的“为什么这么设计”段落；请进入完整手册查看现有判断。'));
  }

  function sourceMap(doc, target) {
    const trueMap = doc.querySelector('.trip-map, .handbook-map, [data-handbook-map], [data-map="true"], figure.map');
    if (trueMap) {
      const clone = cloneWithoutIds(trueMap);
      clone.classList.add('hp-map-original');
      target.append(clone);
      target.append(element(doc, 'p', 'hp-source-note', '地图来自原始手册；前端不新增坐标或路线。'));
      return { trueMap: true, routeSchematics: 0 };
    }
    const route = doc.querySelector('.daily-route');
    if (route) {
      const clone = cloneWithoutIds(route);
      clone.classList.add('hp-map-schematic');
      target.append(clone);
      target.append(notice(doc, '原文未提供真地图。这里只保留原文路线示意，非导航地图；其余每日路线见完整手册。'));
      return { trueMap: false, routeSchematics: 1 };
    }
    target.append(notice(doc, '原始手册未提供真地图或路线示意；不根据地名猜坐标。'));
    return { trueMap: false, routeSchematics: 0 };
  }

  function daySummary(day, index) {
    const title = cleanText(day.querySelector('.day-theme, h3, h2')?.textContent) || `第 ${index + 1} 天`;
    const date = cleanText(day.querySelector('.day-date')?.textContent);
    const timeline = Array.from(day.querySelectorAll('.daily-timeline li'));
    const items = timeline.length ? timeline : Array.from(day.querySelectorAll('ol li'));
    const labels = items.map((item) => cleanText(item.textContent)).filter(Boolean);
    return {
      title,
      date,
      count: labels.length,
      first: labels[0] || '原文未提供时间轴条目',
      last: labels.length > 1 ? labels[labels.length - 1] : '',
    };
  }

  function sourceDailyTable(doc, target) {
    const table = Array.from(doc.querySelectorAll('.page table')).find((candidate) => {
      const headers = Array.from(candidate.querySelectorAll('tr:first-child th')).map((cell) => cleanText(cell.textContent));
      return headers.includes('天') && headers.includes('日期') && headers.includes('这一天');
    });
    if (!table) return null;
    const sourceRows = Array.from(table.querySelectorAll('tr'));
    const list = element(doc, 'div', 'hp-day-list hp-day-list-source');
    list.setAttribute('role', 'table');
    list.setAttribute('aria-label', '每日总表，逐格来自原始手册');
    const summaries = [];
    sourceRows.forEach((sourceRow, rowIndex) => {
      const cells = Array.from(sourceRow.children).map((cell) => cleanText(cell.textContent));
      const row = element(doc, 'div', `hp-day-row hp-day-row-source${rowIndex === 0 ? ' hp-day-head' : ''}`);
      row.setAttribute('role', 'row');
      row.style.setProperty('--hp-day-columns', String(cells.length));
      cells.forEach((value, cellIndex) => {
        const cell = element(doc, 'span', cellIndex === 0 ? 'hp-day-count' : 'hp-day-source-cell', value);
        cell.setAttribute('role', rowIndex === 0 ? 'columnheader' : 'cell');
        row.append(cell);
      });
      list.append(row);
      if (rowIndex > 0) summaries.push({ title: cells[0] || `第 ${rowIndex} 天`, date: cells[1] || '', count: 0 });
    });
    target.append(list);
    return summaries;
  }

  function dailyTable(doc, target) {
    const days = Array.from(doc.querySelectorAll('.itinerary-day'));
    if (!days.length) {
      const sourceSummaries = sourceDailyTable(doc, target);
      if (sourceSummaries) return sourceSummaries;
      target.append(notice(doc, '原始 HTML 没有可提取的逐日结构；不生成每日总表。'));
      return [];
    }
    const list = element(doc, 'div', 'hp-day-list');
    list.setAttribute('role', 'table');
    list.setAttribute('aria-label', '每日总表，内容来自原始手册');
    const head = element(doc, 'div', 'hp-day-row hp-day-head');
    head.setAttribute('role', 'row');
    ['日期与主题', '第一段', '最后一段', '安排'].forEach((label) => {
      const cell = element(doc, 'span', '', label);
      cell.setAttribute('role', 'columnheader');
      head.append(cell);
    });
    list.append(head);
    const summaries = days.map(daySummary);
    summaries.forEach((summary, index) => {
      const row = element(doc, 'div', 'hp-day-row');
      row.setAttribute('role', 'row');
      const titleCell = element(doc, 'span', 'hp-day-title');
      titleCell.setAttribute('role', 'cell');
      const button = element(doc, 'button', 'hp-day-link', summary.title);
      button.type = 'button';
      button.dataset.hpDay = String(index);
      titleCell.append(button);
      if (summary.date) titleCell.append(element(doc, 'small', '', summary.date));
      row.append(titleCell);
      [summary.first, summary.last || '—', `${summary.count} 项`].forEach((value, cellIndex) => {
        const cell = element(doc, 'span', cellIndex < 2 ? 'hp-day-stop' : 'hp-day-count', value);
        cell.setAttribute('role', 'cell');
        row.append(cell);
      });
      list.append(row);
    });
    target.append(list);
    return summaries;
  }

  function photoWall(doc, target) {
    const seen = new Set();
    const images = Array.from(doc.querySelectorAll('.destination-card img, .cell img, .handbook img')).filter((image) => {
      const src = image.getAttribute('src') || '';
      if (!src || seen.has(src) || image.closest('.map, .trip-map, .handbook-map, [data-handbook-map], [data-map="true"]')) return false;
      seen.add(src);
      return true;
    });
    if (!images.length) {
      target.append(notice(doc, '原始手册未提供照片，不补示意图或 stock 图。'));
      return 0;
    }
    const wall = element(doc, 'div', 'hp-photo-wall');
    images.forEach((image) => {
      const figure = element(doc, 'figure', 'hp-photo');
      const clone = image.cloneNode(false);
      clone.removeAttribute('id');
      clone.loading = 'lazy';
      clone.alt = image.alt || '原始手册照片';
      figure.append(clone);
      const container = image.closest('.destination-card, .cell, figure');
      const caption = cleanText(container?.querySelector('h3, h4, .t, figcaption')?.textContent || image.alt);
      if (caption) figure.append(element(doc, 'figcaption', '', caption));
      const credit = cleanText(container?.querySelector('.c')?.textContent);
      if (credit) figure.append(element(doc, 'small', 'hp-photo-credit', credit));
      wall.append(figure);
    });
    target.append(wall);
    return images.length;
  }

  function sourceSeason(doc, target) {
    const table = Array.from(doc.querySelectorAll('.page table')).find((candidate) => {
      const first = cleanText(candidate.querySelector('tr:first-child th')?.textContent);
      const rows = candidate.querySelectorAll('tr').length - 1;
      return first === '什么时候去' && rows >= 2 && rows <= 3;
    });
    if (!table) return 0;
    const title = Array.from(table.parentElement?.querySelectorAll('h2') || []).find((node) => cleanText(node.textContent) === '什么时候去');
    const section = element(doc, 'section', 'hp-season-section');
    section.append(heading(doc, '原文时令判断', '什么时候去', '时段与理由逐格摘自原始手册；没有来源时不补写。'));
    if (!title) section.querySelector('.hp-section-copy').textContent = '原始表格保留；未找到独立的原文季节标题。';
    const clone = cloneWithoutIds(table);
    clone.classList.add('hp-season');
    section.append(clone);
    target.append(section);
    return table.querySelectorAll('tr').length - 1;
  }

  function sourceFinal(doc, target) {
    const pages = Array.from(doc.querySelectorAll('body > .page'));
    if (pages.length === 3) {
      const clone = cloneWithoutIds(pages[2]);
      clone.className = 'hp-source-extra';
      clone.querySelectorAll('.pageno').forEach((node) => node.remove());
      target.append(clone);
      return;
    }
    target.append(heading(doc, '03 · 接着看完整手册', '决定去了，再核对每个细节', '逐日时间、价格、待办、修订回执与核查记录都保留在原始手册中。'));
  }

  function navigation(doc) {
    const nav = element(doc, 'nav', 'hp-page-nav');
    nav.setAttribute('aria-label', '第一眼三页');
    [['1', '设计与路线'], ['2', '照片与时节'], ['3', '安排与深入']].forEach(([page, label]) => {
      const button = element(doc, 'button', '', `${page} · ${label}`);
      button.type = 'button';
      button.dataset.hpGoto = page;
      nav.append(button);
    });
    return nav;
  }

  function ensureStyles(doc) {
    if (!doc.head.querySelector('meta[name="viewport"]')) {
      const viewport = doc.createElement('meta');
      viewport.name = 'viewport';
      viewport.content = 'width=device-width, initial-scale=1';
      doc.head.append(viewport);
    }
    const links = [
      { key: 'hp-tokens', href: '/design-tokens.css' },
      { key: 'hp-presentation', href: '/handbook-presentation.css' },
    ];
    links.forEach(({ key, href }) => {
      if (doc.head.querySelector(`link[data-${key}]`)) return;
      const link = doc.createElement('link');
      link.rel = 'stylesheet';
      link.href = href;
      link.setAttribute(`data-${key}`, '');
      doc.head.append(link);
    });
  }

  function setMode(doc, mode) {
    const next = mode === 'deep' ? 'deep' : 'overview';
    doc.body.dataset.hpMode = next;
    doc.querySelectorAll('[data-hp-mode-button]').forEach((button) => {
      button.setAttribute('aria-pressed', String(button.dataset.hpModeButton === next));
    });
    if (doc.defaultView?.CustomEvent) doc.dispatchEvent(new doc.defaultView.CustomEvent('handbook-mode-change', { detail: next }));
    return next;
  }

  function mount(doc, mode = 'overview') {
    if (!doc?.body || !doc.head) throw new Error('手册文档尚未就绪。');
    const handbook = doc.querySelector('article.handbook');
    const pagedOriginals = handbook ? [] : Array.from(doc.querySelectorAll('body > .page'));
    if (!handbook && pagedOriginals.length !== 3) return { ok: false, reason: 'missing-handbook' };
    ensureStyles(doc);
    const previous = doc.querySelector(OVERVIEW_SELECTOR);
    if (previous) previous.remove();
    const overview = element(doc, 'div', 'hp-overview');
    overview.dataset.hpOverview = '';
    overview.setAttribute('aria-label', '旅行手册第一眼层');

    const title = cleanText(handbook?.querySelector('h1')?.textContent || pagedOriginals[0]?.querySelector('h1')?.textContent || doc.title || '旅行手册');
    const intro = element(doc, 'section', 'hp-page hp-page-decision hp-page-route');
    intro.dataset.hpPage = '1';
    intro.append(element(doc, 'p', 'hp-eyebrow', '01 · 设计、地图与每日'));
    intro.append(element(doc, 'h1', 'hp-title', title));
    intro.append(element(doc, 'p', 'hp-intro', '先看取舍，再看路线。以下内容只重组原始 HTML，不新增事实。'));
    sourceDecision(doc, intro);
    intro.append(heading(doc, '原文路线与节奏', '把每天放在同一张桌上', '真地图只来自原文；每日总表只提取原有标题、时间轴或原有表格。'));
    const mapStats = sourceMap(doc, intro);
    const days = dailyTable(doc, intro);

    const photoPage = element(doc, 'section', 'hp-page hp-page-photos');
    photoPage.dataset.hpPage = '2';
    photoPage.append(heading(doc, '02 · 旅途的照片墙', '让目的先有画面', '只展示原始手册已有照片与其原始来源。'));
    const photos = photoWall(doc, photoPage);
    const seasons = sourceSeason(doc, photoPage);

    const finalPage = element(doc, 'section', 'hp-page hp-page-final');
    finalPage.dataset.hpPage = '3';
    sourceFinal(doc, finalPage);
    const deepButton = element(doc, 'button', 'hp-deep-button', '进入完整手册，查看全部安排与证据 →');
    deepButton.type = 'button';
    deepButton.dataset.hpModeButton = 'deep';
    finalPage.append(deepButton);

    overview.append(navigation(doc), intro, photoPage, finalPage);
    const insertionTarget = handbook || pagedOriginals[0];
    insertionTarget.parentNode.insertBefore(overview, insertionTarget);
    pagedOriginals.forEach((page) => page.classList.add('hp-source-original'));
    doc.documentElement.dataset.hpPages = '3';
    doc.documentElement.dataset.hpDays = String(days.length);
    doc.documentElement.dataset.hpPhotos = String(photos);
    doc.documentElement.dataset.hpSeasons = String(seasons);
    doc.documentElement.dataset.hpTrueMap = String(mapStats.trueMap);
    setMode(doc, mode);
    return { ok: true, pages: 3, days: days.length, photos, seasons, ...mapStats };
  }

  function bind(doc) {
    if (doc.__hpBound) return;
    doc.__hpBound = true;
    doc.addEventListener('click', (event) => {
      const goto = event.target.closest?.('[data-hp-goto]');
      if (goto) {
        doc.querySelector(`[data-hp-page="${goto.dataset.hpGoto}"]`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        return;
      }
      const modeButton = event.target.closest?.('[data-hp-mode-button]');
      if (modeButton) {
        setMode(doc, modeButton.dataset.hpModeButton);
        return;
      }
      const dayButton = event.target.closest?.('[data-hp-day]');
      if (dayButton) {
        setMode(doc, 'deep');
        doc.querySelectorAll('.itinerary-day')[Number(dayButton.dataset.hpDay)]?.scrollIntoView({ block: 'start' });
        return;
      }
      const recordLink = event.target.closest?.('a[href^="#rec"]');
      if (recordLink) {
        setMode(doc, 'deep');
        const target = doc.getElementById(recordLink.getAttribute('href').slice(1));
        let details = target?.matches('details') ? target : target?.closest('details');
        while (details) { details.open = true; details = details.parentElement?.closest('details'); }
      }
    });
  }

  return { mount, setMode, bind, cleanText };
});
