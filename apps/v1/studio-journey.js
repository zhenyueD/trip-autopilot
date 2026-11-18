'use strict';

// Presentation only: no generated inventory, inferred completion or external actions.
window.TravelJourney = (() => {
  const $ = id => document.getElementById(id);
  const list = value => Array.isArray(value) ? value : [];
  const rows = value => list(value).filter(item => item && typeof item === 'object' && !Array.isArray(item));
  const text = value => typeof value === 'string' || typeof value === 'number' ? String(value) : '';
  const node = (tag, className, value) => {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (value !== undefined) result.textContent = text(value);
    return result;
  };
  const statuses = { completed: '操作完成', needs_confirmation: '待确认', blocked: '能力受限',
    running: '正在处理', in_progress: '正在处理', pending: '尚未处理', failed: '未完成' };
  const details = (label, className = '') => {
    const element = node('wa-details', className);
    element.append(node('summary', '', label)); return element;
  };

  function mount() {
    const drawer = details('研究记录与方案依据', 'journey-preparation'); drawer.id = 'journey-preparation';
    drawer.firstChild.append(node('span', 'preparation-peek')); drawer.firstChild.lastChild.id = 'preparation-peek';
    const tasks = node('div', 'journey-tasks'); tasks.id = 'journey-tasks';
    const learning = node('div', 'journey-learning'); learning.id = 'journey-learning';
    const artifacts = node('section', 'journey-artifacts'); artifacts.id = 'journey-artifacts'; artifacts.hidden = true;
    const work = node('div', 'agent-work'); work.id = 'agent-work'; work.hidden = true;
    $('event-details').before(work);
    $('workspace-records').append(drawer);
    const route = node('section', 'day-route'); route.setAttribute('aria-label', '当天行动顺序与Google地图入口');
    const routeSummary = node('div'); routeSummary.id = 'route-summary';
    const routeLegs = node('ol', 'route-legs'); routeLegs.id = 'route-legs';
    route.append(routeSummary, routeLegs); $('map-pane').append(route);
    $('map-pane').setAttribute('aria-label', '当天Google地图与地点');
    $('map').setAttribute('aria-label', 'Google Maps当天地点地图');
    $('workspace-records').append($('demo-guide'));
    drawer.append(tasks, $('decision-section'), $('signature'), $('plan-assumptions'), artifacts, learning);
    // PRD 拍板 #2：reactions 是系统提问，降级为对话流里的提问卡，不占行程封面。
    ($('dock-stream') || $('feedback-form')).append($('reaction-section'));
    $('reaction-title').textContent = '也可以从这里接着想';
    $('reaction-section').querySelector('p').textContent = '点一下填入，再按你的意思改。';
    $('reaction-section').querySelector('a').remove();
    $('itinerary-title').textContent = '今天，照着走。';
    $('itinerary-title').previousElementSibling.textContent = '随身手册 / 每日指南';
    $('feedback-section').querySelector('div > p + p').hidden = true;
    $('feedback').placeholder = '比如：晚上别坐太久的车。白天那段光影我很喜欢，保留。';
  }

  function shortCopy(value, limit = 100) {
    const source = text(value).trim();
    if (source.length <= limit) return { preview: source, full: '' };
    const part = source.slice(0, limit);
    const stop = Math.max(part.lastIndexOf('。'), part.lastIndexOf('！'), part.lastIndexOf('；'));
    return { preview: `${part.slice(0, stop > limit / 2 ? stop + 1 : limit)}${stop > limit / 2 ? '' : '…'}`, full: source };
  }

  function renderImpact(plan) {
    const change = plan.revision_changes ? {} : plan.journey?.changes || {};
    const changed = list(change.changed_item_ids); const preserved = list(change.preserved_item_ids);
    const decisions = list(change.changed_decision_ids);
    const legacy = list(plan.change_summary).map(text).filter(Boolean);
    const visible = changed.length || decisions.length || legacy.length;
    $('replan-impact').hidden = !visible;
    if (!visible) { window.TravelChrome?.syncImpact?.(); return; }
    const title = node('h2', '', `已按你的要求更新 · ${changed.length + decisions.length || legacy.length || 1} 项调整`);
    title.id = 'impact-title';
    const label = node('span', 'change-edition', '本次变化');
    const reason = shortCopy(legacy[0] || change.reason, 110);
    const body = node('div'); body.append(label, title);
    if (reason.preview) body.append(node('p', '', reason.preview));
    const itemIndex = new Map(rows(plan.days).flatMap(day => rows(day.items)).map(item => [item.id, item]));
    const retained = preserved.map(id => itemIndex.get(id)?.title).filter(Boolean);
    if (retained.length) body.append(node('p', 'retained-line', `仍然保留 · ${retained.slice(0, 2).join('、')}${retained.length > 2 ? `等 ${retained.length} 段` : ''}`));
    const full = details('逐项明细', 'change-detail');
    if (reason.full) full.append(node('p', '', reason.full));
    const entries = plan.revision_changes ? [] : rows(plan.decision_links);
    entries.forEach(entry => {
      const section = node('div'); section.append(node('h3', '', entry.trigger));
      ['changes', 'preserved'].forEach(key => list(entry[key]).forEach(value => section.append(node('p', '', `${key === 'changes' ? '调整' : '保留'} · ${text(value)}`))));
      full.append(section);
    });
    if (!entries.length) [...changed.map(id => `调整 · ${text(itemIndex.get(id)?.title || id)}`),
      ...retained.map(value => `保留 · ${value}`), ...legacy].forEach(value => full.append(node('p', '', value)));
    body.append(full); $('replan-impact').replaceChildren(body);
    window.TravelChrome?.syncImpact?.();
  }

  function momentIds(day, plan) {
    const items = rows(day.items);
    if (items.length <= 3) return new Set(items.map(item => item.id));
    const scheduled = new Set(rows(plan.signature?.schedule).filter(item => Number(item.day) === Number(day.day)).map(item => item.id));
    const changed = new Set(plan.revision_changes ? rows(plan.revision_changes.items).map(row => row.item_id) : list(plan.journey?.changes?.changed_item_ids));
    const selected = items.filter(item => changed.has(item.id) || scheduled.has(item.id)).slice(0, 3);
    const pool = [items.at(-1), items[Math.floor((items.length - 1) / 2)], items[0], ...items];
    for (const item of pool) { if (selected.length >= 3) break; if (!selected.includes(item)) selected.push(item); }
    return new Set(selected.map(item => item.id));
  }

  function renderTasks(journey, helpers) {
    const tasks = rows(journey.tasks);
    const blocked = tasks.filter(task => ['blocked', 'needs_confirmation'].includes(task.status));
    const unknown = tasks.filter(task => !Object.hasOwn(statuses, task.status));
    $('preparation-peek').textContent = tasks.length
      ? blocked.length || unknown.length ? `${blocked.length + unknown.length} 项仍待落实` : '查看准备记录与边界'
      : '决定、依据与待落实事项';
    const content = [node('h3', '', '准备工作')];
    content.push(node('p', 'journey-caveat', '材料生成与来源查验不等于已预订。未收到供应方确认的事项仍待落实。'));
    tasks.forEach(task => {
      const row = details(task.title || '未命名准备事项', 'journey-task');
      row.firstChild.prepend(node('span', `work-status ${task.status === 'blocked' ? 'is-blocked' : ''}`,
        task.status === 'completed' ? task.kind === 'digital_material' ? '材料已生成' : '工具操作完成' : statuses[task.status] || '状态未说明'));
      row.append(node('p', '', task.detail || '尚未返回详情。'));
      row.append(helpers.supplyLinks(task.supply_ids)); content.push(row);
    });
    const capabilities = rows(journey.capabilities);
    if (capabilities.length) {
      const limits = details('已找到的供给能力与限制', 'capability-records');
      capabilities.forEach(capability => {
        const row = node('article'); row.append(node('h4', '', capability.what || capability.title || capability.name || capability.id || '供给能力'));
        ['description', 'detail', 'constraints', 'limitations', 'boundary'].forEach(key => {
          const values = Array.isArray(capability[key]) ? capability[key] : [capability[key]];
          values.map(text).filter(Boolean).forEach(value => row.append(node('p', '', value)));
        });
        if (capability.price?.status === 'unknown' || capability.inventory?.status === 'unknown') row.append(node('p', '', '价格与库存未确认。'));
        if (capability.booking_status === 'not_booked') row.append(node('p', '', '尚未预订；公开来源不代表供应方接受了这次安排。'));
        if (capability.source_url || capability.acquisition?.source_url) row.append(helpers.link('查阅来源 ↗', capability.source_url || capability.acquisition.source_url));
        row.append(helpers.supplyLinks(capability.supply_ids || [capability.supply_id])); limits.append(row);
      });
      content.push(limits);
    }
    $('journey-tasks').hidden = !tasks.length && !capabilities.length;
    $('journey-tasks').replaceChildren(...content);
  }

  function artifactText(artifact) {
    if (typeof artifact.content === 'string') return artifact.content;
    if (Array.isArray(artifact.content)) return artifact.content.map(part => typeof part === 'string' ? part :
      part && typeof part === 'object' ? [text(part.title), text(part.text || part.content)].filter(Boolean).join('\n') : '').filter(Boolean).join('\n\n');
    if (artifact.content && typeof artifact.content === 'object') return JSON.stringify(artifact.content, null, 2);
    return '';
  }

  function artifactSheet(artifact, plan, announce) {
    const sheet = details(artifact.title || '旅行材料', 'artifact-sheet');
    const type = { experience_guide: '体验手记', packing_list: '行李准备', inquiry_draft: '询问草稿 · 未发送', route_note: '路线便笺', cultural_guide: '文化阅读', exploration: '探索材料', packing: '行李准备', bilingual: '双语说明', guide: '随身阅读' }[artifact.kind] || '随身材料';
    sheet.firstChild.prepend(node('span', 'artifact-kind', type));
    sheet.firstChild.append(node('span', 'artifact-preview', shortCopy(artifactText(artifact), 105).preview));
    const body = node('div', 'artifact-content', artifactText(artifact));
    const itemIndex = new Map(rows(plan.days).flatMap(day => rows(day.items)).map(item => [item.id, item.title]));
    const related = list(artifact.related_item_ids).map(id => itemIndex.get(id)).filter(Boolean);
    if (related.length) sheet.append(node('p', 'artifact-related', `用在 · ${related.join('、')}`));
    sheet.append(node('p', 'artifact-related', artifact.limits || '按本次出行生成的数字材料，不代表现场服务已落实。'));
    const actions = node('div', 'artifact-actions'); const status = node('p', 'artifact-status'); status.setAttribute('role', 'status');
    const copy = node('button', 'secondary-button', '复制内容'); copy.type = 'button';
    copy.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(`${text(artifact.title)}\n\n${artifactText(artifact)}`); status.textContent = '内容已复制。'; }
      catch { status.textContent = '浏览器未允许复制，请选中文字复制，或保存为文本。'; }
      announce(status.textContent);
    });
    const save = node('button', 'text-button', '保存为文本 ↓'); save.type = 'button';
    save.addEventListener('click', () => {
      const blob = new Blob([`${text(artifact.title)}\n\n${artifactText(artifact)}\n\n数字准备材料，不代表现场服务已落实。`], { type: 'text/plain;charset=utf-8' });
      const url = URL.createObjectURL(blob); const anchor = node('a'); anchor.href = url;
      anchor.download = `${(text(artifact.title) || '旅行材料').replace(/[<>:"/\\|?*\x00-\x1f]/g, '-').slice(0, 80)}.txt`;
      anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); status.textContent = '已交给浏览器保存。';
    });
    actions.append(copy, save, status); sheet.append(body, actions); return sheet;
  }

  function artifactItems(artifact, plan) {
    const ids = new Set(rows(plan.days).flatMap(day => rows(day.items)).map(item => item.id));
    return [...new Set(list(artifact.related_item_ids).filter(id => ids.has(id)))];
  }

  function contextualArtifacts(plan, day, itemId, announce) {
    const dayIds = new Set(rows(day?.items).map(item => item.id));
    const artifacts = rows(plan?.journey?.artifacts).filter(artifact => {
      if (!artifactText(artifact).trim()) return false;
      const related = artifactItems(artifact, plan);
      return itemId !== null ? related.length === 1 && related[0] === itemId
        : related.length > 1 && related.some(id => dayIds.has(id));
    });
    if (!artifacts.length) return null;
    const section = node('section', 'context-artifacts');
    section.setAttribute('aria-label', itemId !== null ? '这段体验的随身材料' : '这一天的随身材料');
    section.append(node('h5', '', itemId !== null ? '为这段体验备好的' : '这一天，可以带着用'));
    artifacts.forEach(artifact => section.append(artifactSheet(artifact, plan, announce)));
    return section;
  }

  function renderArtifacts(journey, plan, announce) {
    const artifacts = rows(journey.artifacts).filter(artifact => artifactText(artifact).trim() && !artifactItems(artifact, plan).length);
    $('journey-artifacts').hidden = !artifacts.length;
    const heading = node('div', 'artifact-heading'); heading.append(node('h2', '', '给这趟旅行，备好的随身材料'),
      node('p', '', '按这次出行生成的数字材料 · 不代表现场服务已落实'));
    const content = artifacts.map(artifact => artifactSheet(artifact, plan, announce));
    $('journey-artifacts').replaceChildren(heading, ...content);
  }

  function renderLearning(journey, correct, carry) {
    const learning = rows(journey.learning).filter(item => text(item.text));
    $('journey-learning').hidden = !learning.length;
    const drawer = details('我会按这些理解继续准备 · 可以纠正');
    const selected = new Set();
    const transfer = node('button', 'secondary-button', '把选中的偏好带到新旅行'); transfer.type = 'button'; transfer.dataset.transfer = 'true'; transfer.disabled = true;
    learning.forEach(item => {
      const row = node('div', 'learning-row'); row.append(node('p', '', item.text), node('small', '',
        `${['confirmed', 'explicit', 'user_stated'].includes(item.status) ? '来自明确反馈' : '暂定理解，尚未确认'} · ${{ trip: '只用于此行', this_trip: '只用于此行', companion: '同行人条件', long_term: '长期偏好候选' }[item.scope] || '适用范围待确认'}${text(item.source) ? ` · ${text(item.source)}` : ''}`));
      const button = node('button', 'text-button', '纠正这一条'); button.type = 'button'; button.dataset.correction = 'true';
      button.addEventListener('click', () => correct(`关于「${text(item.text)}」，我想纠正：`));
      const label = node('label', 'learning-select'); const checkbox = node('input'); checkbox.type = 'checkbox';
      checkbox.addEventListener('change', () => { if (checkbox.checked) selected.add(text(item.text)); else selected.delete(text(item.text)); transfer.disabled = !selected.size; });
      label.append(checkbox, document.createTextNode('下次也想这样')); row.append(button, label); drawer.append(row);
    });
    transfer.addEventListener('click', () => { if (selected.size) carry([...selected]); });
    drawer.append(node('p', 'journey-caveat', '只把你勾选的文字放进新旅行草稿；不自动创建任务，不保存为通用个人档案。'), transfer);
    $('journey-learning').replaceChildren(drawer);
  }

  function renderWork(trip) {
    $('journey-preparation').hidden = !(trip.plan || trip.draft_plan);
    if (!(trip.plan || trip.draft_plan)) $('journey-artifacts').hidden = true;
    const important = !trip.plan || ['running', 'error', 'interrupted', 'needs_attention'].includes(trip.status);
    if (important) $('discovery').before($('task-state'));
    else $('workspace-records').append($('task-state'));
    // A current trip remains the main surface. Detailed live tool work belongs
    // inside its existing progress disclosure, never a second dashboard above it.
    if (trip.plan) $('event-details').querySelector('summary').after($('agent-work'));
    else $('event-details').before($('agent-work'));
    $('task-state').classList.toggle('is-running', trip.status === 'running');
    const work = trip.agent_work || {};
    const tasks = rows(work.tasks), actions = rows(work.actions), events = rows(work.events);
    const entries = tasks.length ? tasks : actions.length ? actions : events;
    $('agent-work').hidden = !entries.length;
    if (!entries.length) { $('agent-work').replaceChildren(); return; }
    const active = entries.filter(item => ['running', 'in_progress', 'blocked', 'failed'].includes(item.status));
    const focus = active.length ? active : entries.slice(-2);
    const current = node('ul', 'agent-current');
    focus.slice(0, 3).forEach(item => {
      const row = node('li'); row.append(node('span', 'work-status', statuses[item.status] || '已记录'),
        node('span', '', item.title || item.label || item.detail || item.tool || '工作记录')); current.append(row);
    });
    const history = details(`查看实际工作记录（${entries.length}）`);
    const all = node('ol'); entries.forEach(item => all.append(node('li', '', `${statuses[item.status] || '已记录'} · ${text(item.title || item.label || item.detail || item.tool || '工作记录')}`)));
    history.append(all); $('agent-work').replaceChildren(current, history);
  }

  function render(plan, helpers) {
    const journey = plan.journey || {};
    renderImpact(plan); renderTasks(journey, helpers); renderArtifacts(journey, plan, helpers.announce); renderLearning(journey, helpers.correct, helpers.carry);
    const heroGoal = $('story').querySelector('.story-intention');
    if (text(plan.summary).trim() && heroGoal) {
      const goal = node('div', 'preparation-goal');
      goal.append(node('h3', '', '准备目标'), node('p', 'journey-caveat', journey.goal));
      $('journey-tasks').prepend(goal); $('journey-tasks').hidden = false;
      heroGoal.remove();
    }
    const assumptions = [...list(journey.assumptions), ...list(plan.core_notes), ...rows(plan.understanding).filter(item => item.kind !== 'confirmed').map(item => item.text)].map(text).filter(Boolean);
    $('plan-assumptions').hidden = !assumptions.length;
    $('assumption-list').replaceChildren(...[...new Set(assumptions)].map(value => node('li', '', value)));
  }

  return { mount, render, renderWork, momentIds, shortCopy, contextualArtifacts };
})();
