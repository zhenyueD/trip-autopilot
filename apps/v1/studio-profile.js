'use strict';
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.TravelProfile = api;
})(typeof window === 'undefined' ? null : window, function () {
  const fields = ['traveler_groups', 'mobility_preferences', 'pace_preference', 'preferences', 'user_notes'];
  const arrays = new Set(['traveler_groups', 'mobility_preferences', 'preferences']);
  function create({ get, getTrip, blocked, save, appendFeedback }) {
    let identity = null, base = null, dirty = false, conflict = false, round = '', ignored = new Set();
    const status = text => { get('profile-status').textContent = text; };
    function load(trip) {
      base = { expected_revision: trip.revision, expected_profile_revision: trip.profile_revision || 0 };
      fields.forEach(field => { const value = trip.traveler_profile?.[field]; get(`profile-${field}`).value = arrays.has(field) ? (value || []).join('\n') : value || ''; });
      dirty = false; conflict = false; status('资料单独保存，不会自动修改行程。');
    }
    fields.forEach(field => get(`profile-${field}`).addEventListener('input', () => { dirty = true; status('有尚未保存的资料。'); }));
    get('profile-reload').addEventListener('click', () => { if (!blocked() && getTrip()) { load(getTrip()); update(); } });
    get('profile-rebase').addEventListener('click', () => {
      if (blocked() || !getTrip()) return;
      base = { expected_revision: getTrip().revision, expected_profile_revision: getTrip().profile_revision || 0 };
      conflict = false; status('草稿保留；请检查后点击保存，将替换已保存的偏好。'); update();
    });
    get('profile-form').addEventListener('submit', async event => {
      event.preventDefault(); if (blocked() || !base || conflict) return;
      const traveler_profile = { version: 1 };
      fields.forEach(field => { const text = get(`profile-${field}`).value; traveler_profile[field] = arrays.has(field) ? text.split('\n').map(s => s.trim()).filter(Boolean) : text; });
      dirty = true;
      try { const trip = await save({ ...base, traveler_profile }); if (trip) { load(trip); status('资料已保存；行程版本未自动修改。'); } }
      catch (error) { if (error.status === 409) conflict = true; status(`${error.message} 草稿仍保留，未自动重发。${conflict ? '请先读取并确认最新资料，再决定保留草稿或采用最新资料。' : ''}`); }
      update();
    });
    function suggestions(trip) {
      const key = JSON.stringify([trip.id, trip.plan_revision ?? trip.revision]);
      if (key !== round) { round = key; ignored = new Set(); }
      const parent = get('plan-suggestions'); parent.replaceChildren();
      const rows = (trip.plan?.suggestions || []).filter(row => row && typeof row === 'object');
      parent.hidden = !rows.some((row, index) => !ignored.has(`${row.id}:${index}`));
      const node = (tag, text) => { const el = document.createElement(tag); el.textContent = text; return el; };
      if (!parent.hidden) parent.append(node('h2', '可以考虑的调整'), node('p', '这是模型建议；采纳只填写草稿，由你确认是否提交。'));
      rows.forEach((row, index) => {
        const id = `${row.id}:${index}`; if (ignored.has(id)) return;
        const card = node('article', ''); card.className = 'plan-suggestion';
        card.append(node('h3', row.title || '旅行建议'), node('p', row.advice || ''), node('p', `理由：${row.reason || '未提供'}`));
        const adopt = node('button', '采纳到调整草稿'), ignore = node('button', '本轮忽略');
        [adopt, ignore].forEach(button => { button.type = 'button'; button.className = 'secondary-button'; button.disabled = blocked(); });
        adopt.disabled ||= typeof row.proposed_feedback !== 'string' || !row.proposed_feedback.trim();
        adopt.addEventListener('click', () => { if (!blocked()) appendFeedback(row.proposed_feedback); });
        ignore.addEventListener('click', () => { if (!blocked()) { ignored.add(id); suggestions(getTrip()); } });
        card.append(adopt, ignore); parent.append(card);
      });
    }
    function update() {
      const disabled = blocked();
      fields.forEach(field => { get(`profile-${field}`).disabled = disabled; });
      get('profile-save').disabled = disabled || conflict;
      get('profile-reload').disabled = disabled; get('profile-rebase').disabled = disabled;
      get('profile-rebase').hidden = !conflict;
      if (getTrip()) suggestions(getTrip());
    }
    return {
      accept(trip) {
        if (identity !== trip.id) { identity = trip.id; load(trip); }
        else if (!dirty) load(trip);
        else if (base && (base.expected_revision !== trip.revision || base.expected_profile_revision !== (trip.profile_revision || 0))) {
          conflict = true; status('已保存的资料发生变化，草稿仍保留。可恢复已保存的偏好，或确认用这份草稿替换。');
        }
        update();
      }, update,
      reset() { identity = null; base = null; dirty = false; conflict = false; round = ''; ignored.clear(); fields.forEach(field => { get(`profile-${field}`).value = ''; }); get('plan-suggestions').replaceChildren(); }
    };
  }
  return { create };
});
