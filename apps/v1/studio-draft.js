'use strict';
// Local draft storage only. Keys contain a trip ID, never its access token.
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.TravelDraft = api;
})(typeof window === 'undefined' ? null : window, function () {
  const key = id => `atom-travel-draft-v1:${id}`;
  const empty = () => ({ feedback: '', links: '', evidence: '', images: [], documents: [] });
  function validate(value) {
    if (!value || value.version !== 1) throw new Error('草稿格式无法读取');
    const draft = value.draft;
    if (!draft || ['feedback', 'links', 'evidence'].some(k => typeof draft[k] !== 'string')) throw new Error('草稿文字无法读取');
    for (const kind of ['images', 'documents']) {
      if (!Array.isArray(draft[kind]) || draft[kind].length > (kind === 'images' ? 3 : 5)
        || draft[kind].some(file => !file || typeof file.name !== 'string' || typeof file.data_url !== 'string'
          || !(kind === 'images' ? /^data:image\/(jpeg|png|webp);base64,/ : /^data:[^,]*;base64,/).test(file.data_url))) {
        throw new Error('草稿附件无法读取');
      }
    }
    return draft;
  }
  function create({ storage = () => localStorage, read, write, report }) {
    let tripId = null; let failed = false; let unreadable = false;
    function save() {
      if (!tripId) return true;
      if (unreadable) return false;
      try {
        const draft = read();
        const hasContent = draft.feedback || draft.links || draft.evidence || draft.images.length || draft.documents.length;
        if (hasContent) storage().setItem(key(tripId), JSON.stringify({ version: 1, draft }));
        else storage().removeItem(key(tripId));
        failed = false;
        report(hasContent ? '已自动保存草稿 · 仅在此浏览器' : '文字与材料会自动保存为草稿 · 仅在此浏览器', false);
        return true;
      } catch {
        failed = true;
        report('草稿未能保存：浏览器存储不可用或空间不足。当前内容仍在，请先提交或备份，暂勿刷新。', true);
        return false;
      }
    }
    function restore(id) {
      if (tripId === id) return;
      tripId = id || null; failed = false; unreadable = false;
      write(empty());
      if (!tripId) return;
      try {
        const raw = storage().getItem(key(tripId));
        if (raw) write(validate(JSON.parse(raw)));
        report(raw ? '已恢复自动保存的草稿 · 仅在此浏览器' : '文字与材料会自动保存为草稿 · 仅在此浏览器', false);
      } catch {
        failed = true;
        unreadable = true;
        report('已有草稿无法读取，未删除存储记录。请保留此页面并检查浏览器存储。', true);
      }
    }
    return { save, restore, get failed() { return failed; },
      detach() { if (!save()) return false; tripId = null; return true; },
      discard(id) {
        if (tripId === id) { tripId = null; unreadable = false; }
        try { storage().removeItem(key(id)); return true; }
        catch { report('未能清除本机草稿，请检查浏览器存储。', true); return false; }
      }
    };
  }
  return { create, validate };
});
