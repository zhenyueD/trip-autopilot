'use strict';

// Shared composer. Revision drafts are saved locally only when configured.
window.TravelIntake = (() => {
  const $ = id => document.getElementById(id);
  const str = value => typeof value === 'string' || typeof value === 'number' ? String(value) : '';
  const records = value => Array.isArray(value) ? value.filter(x => x && typeof x === 'object' && !Array.isArray(x)) : [];
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = str(text);
    return node;
  }
  function readFile(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error('文件读取失败，请重新选择。'));
      reader.readAsDataURL(file);
    });
  }
  async function compressImage(file) {
    if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) throw new Error('只接受 JPEG、PNG 或 WebP 图片。');
    if (file.size > 15 * 1024 * 1024) throw new Error('单张原图请小于 15MB，避免浏览器处理失败。');
    const data = await readFile(file); const image = new Image();
    await new Promise((resolve, reject) => {
      image.onload = resolve; image.onerror = () => reject(new Error('无法解析这张图片。')); image.src = data;
    });
    if (!image.naturalWidth || !image.naturalHeight || image.naturalWidth * image.naturalHeight > 60000000) {
      throw new Error('图片尺寸过大或无效，请先缩小图片。');
    }
    const canvas = document.createElement('canvas'); const context = canvas.getContext('2d');
    if (!context) throw new Error('此浏览器不支持图片压缩，请使用文字材料。');
    const scale = Math.min(1, 1800 / Math.max(image.naturalWidth, image.naturalHeight));
    canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
    canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
    context.fillStyle = '#fff'; context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    let result = canvas.toDataURL('image/jpeg', 0.86);
    for (let quality = 0.76; result.length > 600 * 1024 * 4 / 3 && quality >= 0.3; quality -= 0.1) {
      result = canvas.toDataURL('image/jpeg', quality);
    }
    if (result.length > 600 * 1024 * 4 / 3) throw new Error('压缩后仍超过 600KB，请裁剪到关键信息后再试。');
    return { name: (file.name || '粘贴图片').slice(0, 180), data_url: result };
  }
  async function prepareDocument(file) {
    const extension = (file.name.match(/\.[^.]+$/)?.[0] || '').toLowerCase();
    if (!['.xlsx', '.pdf', '.eml', '.txt', '.md', '.csv', '.json', '.zip'].includes(extension)) {
      throw new Error('文件仅支持 XLSX、PDF、EML、TXT、MD、CSV、JSON 和 ZIP。');
    }
    const limit = extension === '.zip' ? 8 * 1024 * 1024 : 2.5 * 1024 * 1024;
    if (!file.size || file.size > limit) throw new Error(`单个${extension === '.zip' ? ' ZIP ' : '文件'}请小于 ${extension === '.zip' ? '8MB' : '2.5MB'}。`);
    return { name: file.name.slice(0, 180), data_url: await readFile(file) };
  }
  function extractURLs(text) {
    return (text.match(/https?:\/\/[^\s<>"'，。；！？、]+/gi) || []).map(url => url.replace(/[)\]）】]+$/, ''));
  }
  function collectLinks(explicit, context, safeURL) {
    const entries = explicit.split(/[\s，；]+/).filter(Boolean);
    if (entries.some(value => !safeURL(value.replace(/^http:/i, 'https:')))) {
      throw new Error('链接栏请每行放一条公开 HTTP / HTTPS 链接；其他文字可放进笔记。');
    }
    const links = [...new Set([...entries, ...extractURLs(context)].map(value => safeURL(value) || value))];
    if (links.length > 5) throw new Error('每次最多提交 5 条公开链接（也包含文字里的链接）。请减少后再提交。');
    return links;
  }
  function createComposer(config) {
    const queue = { images: [], documents: [], preparing: false, busy: false, vision: false, version: 0 };
    const node = name => $(config[name]);
    const status = (message, error = false) => {
      node('status').textContent = message; node('status').classList.toggle('is-error', error);
    };
    const draft = config.draftFeedback && window.TravelDraft?.create({
      read: () => ({ feedback: $(config.draftFeedback).value, links: node('links').value,
        evidence: node('evidence').value, images: queue.images, documents: queue.documents }),
      write(value) {
        queue.version += 1; queue.preparing = false;
        $(config.draftFeedback).value = value.feedback; node('links').value = value.links;
        node('evidence').value = value.evidence; queue.images = value.images; queue.documents = value.documents;
        renderImages(); renderDocuments(); controls();
      },
      report(message, error) {
        const target = $(config.draftStatus);
        target.textContent = message; target.classList.toggle('is-error', error);
      }
    });
    function saveDraft() {
      const saved = draft?.save();
      if (draft && queue.preparing && saved) $(config.draftStatus).textContent = '正在准备附件，完成后自动保存草稿。';
      return saved;
    }
    const changed = () => { config.onChange(); saveDraft(); };
    if (draft) {
      [config.draftFeedback, config.links, config.evidence].forEach(id => $(id).addEventListener('input', saveDraft));
      window.addEventListener('pagehide', () => draft.save());
      window.addEventListener('beforeunload', event => {
        if (queue.preparing || !draft.save()) { event.preventDefault(); event.returnValue = ''; }
      });
    }
    function renderImages() {
      node('list').replaceChildren(...queue.images.map((image, index) => {
        const row = el('li'); const thumb = el('img', 'queued-image');
        thumb.src = image.data_url; thumb.alt = ''; // Only locally compressed data; never a pasted URL.
        const copy = el('span', 'queued-image-copy', image.name);
        copy.append(el('small', '', `待提交 · ${Math.ceil(image.data_url.length * 0.75 / 1024)}KB`));
        const button = el('button', 'text-button', '移除'); button.type = 'button';
        button.setAttribute('aria-label', `移除图片 ${image.name}`); button.disabled = queue.busy || queue.preparing;
        button.addEventListener('click', () => {
          queue.images.splice(index, 1); renderImages(); status(`还剩 ${queue.images.length} 张待提交图片。`);
          const buttons = node('list').querySelectorAll('button');
          (buttons[Math.min(index, buttons.length - 1)] || node('choose')).focus();
          changed();
        });
        row.append(thumb, copy, button); return row;
      }));
    }
    function renderDocuments() {
      node('documentList').replaceChildren(...queue.documents.map((document, index) => {
        const row = el('li');
        const copy = el('span', 'queued-image-copy', document.name);
        copy.append(el('small', '', `待提交读取 · ${Math.ceil(document.data_url.length * 0.75 / 1024)}KB`));
        const button = el('button', 'text-button', '移除'); button.type = 'button';
        button.setAttribute('aria-label', `移除文件 ${document.name}`); button.disabled = queue.busy || queue.preparing;
        button.addEventListener('click', () => {
          queue.documents.splice(index, 1); renderDocuments(); status(`还剩 ${queue.documents.length} 个待提交文件。`);
          const buttons = node('documentList').querySelectorAll('button');
          (buttons[Math.min(index, buttons.length - 1)] || node('documentChoose')).focus();
          changed();
        });
        row.append(copy, button); return row;
      }));
    }
    function controls() {
      node('file').disabled = queue.busy || queue.preparing || !queue.vision;
      node('choose').disabled = node('file').disabled;
      node('documentFile').disabled = queue.busy || queue.preparing;
      node('documentChoose').disabled = node('documentFile').disabled;
      node('evidence').disabled = queue.busy; node('links').disabled = queue.busy;
      node('zone').setAttribute('aria-disabled', String(node('file').disabled));
      node('zone').setAttribute('aria-busy', String(queue.preparing));
      node('list').querySelectorAll('button').forEach(button => { button.disabled = queue.busy || queue.preparing; });
      node('documentList').querySelectorAll('button').forEach(button => { button.disabled = queue.busy || queue.preparing; });
    }
    async function addFiles(files) {
      if (!files.length) return;
      if (queue.busy || queue.preparing) { status('当前正在处理，请稍后再添加。', true); return; }
      const images = files.filter(file => ['image/jpeg', 'image/png', 'image/webp'].includes(file.type));
      const documents = files.filter(file => !images.includes(file));
      if (images.length && !queue.vision) { status('视觉能力尚不可用；可先添加表格、PDF、邮件或文本。', true); return; }
      if (images.length + queue.images.length > 3) { status('最多添加 3 张图片；请先移除一些。', true); return; }
      if (documents.length + queue.documents.length > 5) { status('每次最多添加 5 个文件；请先移除一些。', true); return; }
      const version = queue.version;
      queue.preparing = true; controls(); changed(); status('正在本地准备材料，尚未上传…');
      try {
        const preparedImages = []; const preparedDocuments = [];
        for (const file of images) preparedImages.push(await compressImage(file));
        for (const file of documents) preparedDocuments.push(await prepareDocument(file));
        if (version !== queue.version) return;
        queue.images.push(...preparedImages); queue.documents.push(...preparedDocuments);
        status(`已准备 ${queue.documents.length} 个文件、${queue.images.length} 张图片，提交时才会上传。`);
      } catch (error) { if (version === queue.version) status(`${error.message} 本批材料未加入，已有材料保留。`, true); }
      finally { if (version === queue.version) { queue.preparing = false; renderImages(); renderDocuments(); controls(); changed(); } }
    }
    node('choose').addEventListener('click', () => node('file').click());
    node('file').addEventListener('change', () => { const files = [...node('file').files]; node('file').value = ''; addFiles(files); });
    node('documentChoose').addEventListener('click', () => node('documentFile').click());
    node('documentFile').addEventListener('change', () => { const files = [...node('documentFile').files]; node('documentFile').value = ''; addFiles(files); });
    node('form').addEventListener('paste', event => {
      const files = [...(event.clipboardData?.items || [])].filter(item => item.kind === 'file').map(item => item.getAsFile()).filter(Boolean);
      if (files.length) { event.preventDefault(); $(config.details)?.setAttribute('open', ''); addFiles(files); }
    });
    node('zone').addEventListener('keydown', event => {
      if (event.target === node('zone') && (event.key === 'Enter' || event.key === ' ')) {
        event.preventDefault(); if (!node('file').disabled) node('file').click();
      }
    });
    node('zone').addEventListener('dragover', event => {
      event.preventDefault(); node('zone').classList.add('is-dragging');
      if (event.dataTransfer) event.dataTransfer.dropEffect = node('file').disabled ? 'none' : 'copy';
    });
    node('zone').addEventListener('dragleave', event => {
      if (!node('zone').contains(event.relatedTarget)) node('zone').classList.remove('is-dragging');
    });
    node('zone').addEventListener('drop', event => {
      event.preventDefault(); node('zone').classList.remove('is-dragging');
      const files = [...(event.dataTransfer?.files || [])];
      if (files.length) addFiles(files);
      else status('这里接收图片、表格、PDF、邮件和文本文件。', true);
    });
    return {
      saveDraft,
      restoreDraft: id => draft?.restore(id),
      detachDraft() {
        if (queue.preparing) { status('材料还在读取，请等待读取完成后切换旅行。', true); return false; }
        return draft ? draft.detach() : true;
      },
      discardDraft(id) { queue.version += 1; queue.preparing = false; return draft ? draft.discard(id) : true; },
      get preparing() { return queue.preparing; },
      update({ busy, cap }) {
        queue.busy = busy; queue.vision = Boolean(cap?.vision_available); controls();
        node('vision').hidden = queue.vision;
        node('vision').textContent = cap ? '当前没有可用视觉模型；图片入口暂不可用，可粘贴文字或链接。' : '正在确认图片能力；文字和链接可先填写。';
        node('help').textContent = `最多 3 张 JPEG / PNG / WebP · 每张压缩至 600KB 内${queue.vision ? ` · 由 ${str(cap.vision_model) || '已配置视觉模型'} 处理` : ''}`;
      },
      payload(context = '') {
        const evidence_text = node('evidence').value.trim();
        return { evidence_text, links: collectLinks(node('links').value.trim(), `${context}\n${evidence_text}`, config.safeURL),
          images: [...queue.images], documents: [...queue.documents] };
      },
      hasMaterials(payload) { return Boolean(payload.evidence_text || payload.links.length || payload.images.length || payload.documents.length); },
      reset(message = '') {
        queue.version += 1; queue.preparing = false;
        queue.images = []; queue.documents = []; node('evidence').value = ''; node('links').value = ''; node('file').value = ''; node('documentFile').value = '';
        renderImages(); renderDocuments(); controls(); status(message); draft?.save();
      },
      status,
    };
  }
  const kindLabels = { user_provided: '你提供的文字', image_extracted: '图片提取 · 待核对',
    document_extracted: '文件提取 · 待核对', source_claim: '网页说法 · 未独立核验', hypothesis: '偏好猜测 · 待你确认' };
  function correctionButton(text, config, trip, label = '纠正这点') {
    const button = el('button', 'text-button intake-correct', label); button.type = 'button';
    button.dataset.correction = 'true'; button.disabled = trip.status === 'running';
    button.addEventListener('click', () => config.correct(text)); return button;
  }
  function renderReceipts(materials, config, trip) {
    const names = { user_brief: '你的想法', user_text: '文字材料', user_feedback: '调整文字', web_link: '公开链接',
      image_extracted: '图片材料', document_extracted: '文件材料', legacy_material: '历史材料' };
    const statuses = { pending: '等待读取', read: '已收到文字', image_extracted: '已提取 · 待核对', blocked: '无法读取', unavailable: '未读到内容' };
    const notes = new Map(records(trip.intake?.source_notes).map(note => [note.source_id, note]));
    const outcomes = { used: '如何用于安排', context_only: '仅作背景', needs_clarification: '还需要确认', unavailable: '未用于当前理解' };
    const nodes = materials.map((material, index) => {
      const row = el('li', 'material-receipt'); row.id = `material-${str(material.id)}`;
      const text = el('div'); const title = el('strong', '', material.label || `${names[material.kind] || '材料'} ${index + 1}`);
      const status = material.status === 'read' && material.kind === 'web_link' ? '读到网页文本'
        : statuses[material.status] || '处理状态待确认';
      text.append(title, el('span', 'receipt-state', `${names[material.kind] || '材料'} · ${status}`));
      if (material.message) text.append(el('p', '', material.message));
      if (material.url) text.append(config.link('打开原链接 ↗', material.url, 'receipt-link'));
      if (['blocked', 'unavailable'].includes(material.status)) {
        text.append(el('p', 'receipt-fallback', '可补充截图或摘录；其他有效材料会继续处理。'));
      }
      const note = notes.get(material.id);
      if (note?.explanation) {
        const usage = el('div', 'material-use-note');
        usage.append(el('span', '', outcomes[note.outcome] || '如何处理这份材料'), el('p', '', note.explanation));
        text.append(usage);
      }
      row.append(el('span', 'receipt-number', String(index + 1).padStart(2, '0')), text); return row;
    });
    $('material-receipts').replaceChildren(...nodes);
    if (!nodes.length) $('material-receipts').append(el('li', 'empty-copy', '服务尚未返回材料收据，不代表已经读完。'));
  }
  function signalNode(signal, config, trip, materialIds) {
    const row = el('li', 'intake-signal');
    row.id = `signal-${str(signal.id)}`;
    const label = kindLabels[signal.kind] || kindLabels.hypothesis;
    row.append(el('span', `signal-kind ${signal.kind === 'hypothesis' ? 'is-hypothesis' : ''}`, label), el('p', 'signal-text', signal.text));
    if (signal.implication) row.append(el('p', 'signal-implication', `对安排的影响：${str(signal.implication)}`));
    const evidence = records(signal.evidence);
    if (evidence.length) {
      const sources = el('wa-details', 'signal-evidence'); const summary = el('summary', '', `查看依据 · ${evidence.length} 处`);
      sources.append(summary);
      evidence.forEach(item => {
        const quote = el('blockquote', '', item.quote); const label = el('span', 'evidence-label', item.label || '材料原文');
        if (materialIds.has(item.source_id)) {
          const anchor = el('a', '', '对应材料 ↑'); anchor.href = `#material-${encodeURIComponent(str(item.source_id))}`;
          label.append(anchor);
        }
        sources.append(label, quote);
      });
      row.append(sources);
    } else if (signal.kind === 'hypothesis') row.append(el('p', 'field-hint', '这是推测，不是你已确认的偏好。'));
    row.append(correctionButton(`关于「${str(signal.text)}」，我想纠正：`, config, trip)); return row;
  }
  function renderQuestions(intake, target, config, trip) {
    const conflicts = records(intake.conflicts);
    if (conflicts.length) {
      const section = el('section', 'intake-conflicts'); section.append(el('h3', '', '材料里有些地方还对不上'));
      conflicts.forEach(conflict => {
        const row = el('article'); row.append(el('h4', '', conflict.summary), el('p', '', conflict.question));
        row.append(correctionButton(`关于「${str(conflict.summary)}」：`, config, trip, '补充答案')); section.append(row);
      }); target.append(section);
    }
    const questions = Array.isArray(intake.questions) ? intake.questions : [];
    if (questions.length) {
      const details = el('wa-details', 'intake-questions'); details.append(el('summary', '', '有空时，再补充这些'));
      const list = el('ul'); questions.forEach(question => list.append(el('li', '', typeof question === 'object' ? question?.question || question?.text : question)));
      details.append(list); target.append(details);
    }
    if (Array.isArray(intake.warnings)) intake.warnings.forEach(warning => target.append(el('p', 'intake-warning', warning)));
  }
  function renderDateWindow(value) {
    let note = $('intake-date-window');
    if (!note) {
      note = el('p', 'field-hint'); note.id = 'intake-date-window';
      $('intake-overview').after(note);
    }
    const valid = value && typeof value === 'object' && !Array.isArray(value)
      && /^\d{4}-\d{2}-\d{2}$/.test(str(value.start)) && /^\d{4}-\d{2}-\d{2}$/.test(str(value.end))
      && Number.isInteger(value.duration_days) && value.duration_days > 0;
    note.hidden = !valid; note.textContent = '';
    if (!valid) return;
    const range = `${value.start} 至 ${value.end}`; const days = value.duration_days;
    const descriptions = {
      lodging_only: `按住宿资料暂排 ${range}，含入住/退房共 ${days} 天（日历天数）；不代表航班已确定。`,
      provisional: `暂定 ${range}，共 ${days} 个日历日；日期尚待确认。`,
      trip: `旅行日期窗口 ${range}，共 ${days} 个日历日；不代表航班、住宿已预订。`,
    };
    note.textContent = descriptions[value.basis] || `日期窗口待核对：${range}，共 ${days} 个日历日。`;
    if (str(value.assumption)) note.append(document.createTextNode(` 假设：${str(value.assumption)}`));
  }
  function renderIntake(trip, config) {
    const materials = records(trip.materials); const intake = trip.intake;
    $('intake-panel').hidden = !intake && !materials.length;
    $('legacy-understanding').hidden = Boolean(intake);
    if ($('intake-panel').hidden) return;
    const fingerprint = JSON.stringify([materials, intake, Boolean(trip.plan), trip.status,
      [...(config.unavailableSources || [])].sort(), [...(config.validatedSources || [])].sort()]);
    if ($('intake-panel').dataset.fingerprint === fingerprint) return;
    $('intake-panel').dataset.fingerprint = fingerprint;
    if (trip.plan && $('intake-panel').dataset.hasPlan !== 'true') $('intake-details').open = false;
    $('intake-panel').dataset.hasPlan = String(Boolean(trip.plan));
    const conflictCount = records(intake?.conflicts).length;
    $('intake-summary').textContent = `${materials.length} 份材料${conflictCount ? ` · ${conflictCount} 处待对齐` : ''} · 查看来源、理解与默认方向`;
    $('intake-overview').textContent = str(intake?.summary);
    renderDateWindow(intake?.date_window);
    $('intake-state').textContent = trip.status === 'running'
      ? '已返回的理解先在这里；具体安排仍在准备。'
      : '这份理解与当前版本一起保留，随时可纠正。';
    renderReceipts(materials, config, trip);
    const target = $('intake-content'); target.replaceChildren();
    if (!intake) { target.append(el('p', 'empty-copy', '材料已收到，等待模型返回有来源的理解。')); return; }
    target.append(el('h3', '', '我目前这样理解你'));
    const signals = el('ul', 'intake-signals'); const materialIds = new Set(materials.map(item => item.id));
    records(intake.signals).forEach(signal => signals.append(signalNode(signal, config, trip, materialIds)));
    target.append(signals);
    if (!signals.children.length) target.append(el('p', 'empty-copy', '尚未返回可展示的理解，不会把空白补成已确认信息。'));
    renderQuestions(intake, target, config, trip);
    const defaults = records(intake.defaults);
    if (defaults.length) {
      const section = el('section', 'intake-defaults'); section.append(el('h3', '', '如果你不补充，我先这样安排'));
      defaults.forEach(item => {
        const row = el('article'); row.append(el('h4', '', item.choice), el('p', '', item.reason));
        const references = el('div', 'default-references');
        const signalMap = new Map(records(intake.signals).map(signal => [signal.id, signal]));
        (Array.isArray(item.signal_ids) ? item.signal_ids : []).forEach(id => {
          const signal = signalMap.get(id); if (!signal) return;
          const anchor = el('a', '', `依据：${str(signal.text)}`); anchor.href = `#signal-${encodeURIComponent(str(id))}`;
          references.append(anchor);
        });
        row.append(references);
        row.append(correctionButton(`默认方向「${str(item.choice)}」，我希望改成：`, config, trip, '调整方向')); section.append(row);
      }); target.append(section);
    }
    if (trip.status === 'running') target.append(el('p', 'field-hint', '本轮研究结束后可纠正或补充；生成中的理解不代表你已经确认。'));
  }
  return { createComposer, renderIntake };
})();
