'use strict';

((root, factory) => {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.TravelRevisionProgress = api;
})(typeof window === 'object' ? window : null, () => {
  const HEARTBEAT_AFTER_MS = 30000;
  function fallbackLabel(trip) {
    if (trip.job?.kind === 'generate') return '正在生成旅行手册';
    if (trip.job?.kind === 'resume') return '正在继续准备旅行手册';
    if (trip.job?.kind === 'revise') return '正在处理调整';
    if (trip.job?.kind === 'artifacts') return '正在准备导出文件';
    if (trip.job_kind === 'prepare') return '正在查漏补缺';
    return trip.plan ? '正在处理调整' : '正在生成旅行方案';
  }

  function attemptStart(trip, now) {
    const firstAttempt = trip.revision === 1 && trip.job_kind !== 'prepare';
    // created_at belongs only to the first attempt; retained events and their
    // display labels cannot establish the start of a later revision.
    const value = trip.capabilities?.single_session ? trip.job?.started_at
      : trip.revision_started_at ?? (firstAttempt ? trip.created_at : '');
    const timestamp = typeof value === 'string' ? Date.parse(value) : NaN;
    return Number.isFinite(timestamp) && timestamp <= now ? timestamp : null;
  }

  function createElapsed() {
    let context = '', startedAt = null, confirmed = false;
    function reset() { context = ''; startedAt = null; confirmed = false; }
    function label(trip) {
      if (trip?.status !== 'running') { reset(); return ''; }
      const now = Date.now();
      const nextContext = JSON.stringify([trip.id, trip.revision, trip.job_kind,
        trip.job?.id, trip.job?.kind, trip.job?.started_at]);
      if (context !== nextContext) {
        reset(); context = nextContext; startedAt = now;
      }
      if (!confirmed) {
        const timestamp = attemptStart(trip, now);
        if (timestamp !== null) { startedAt = timestamp; confirmed = true; }
      }
      // Retain a verified start if the API's rolling event window drops it.
      // Without start evidence, report only time actually observed on this page.
      const seconds = Math.max(0, Math.floor((now - startedAt) / 1000));
      return `${confirmed ? '本轮' : '本页观察'}已用时 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
    }
    return { label, reset };
  }

  function create({ container }) {
    let context = '', event = '', progressAt = 0, timer = null, version = 0;

    function reset() {
      clearTimeout(timer); timer = null; version += 1;
      context = ''; event = ''; progressAt = 0;
      container.hidden = true; container.textContent = '';
    }

    function render(trip, generation) {
      const stage = trip?.revision_stage;
      if (trip?.status !== 'running' || stage?.status === 'failed') { reset(); return; }
      const nextContext = JSON.stringify([trip.id, trip.revision, trip.job_kind,
        trip.job?.id, trip.job?.kind, trip.job?.started_at, generation]);
      if (context !== nextContext) { reset(); context = nextContext; progressAt = Date.now(); }
      const hasStage = stage && ['started', 'completed'].includes(stage.status);
      // Durations can change on every poll; only a real stage transition advances the clock.
      const nextEvent = hasStage ? JSON.stringify([stage.id, stage.phase, stage.status, stage.id ? null : stage.at]) : '';
      if (nextEvent && event !== nextEvent) {
        const timestamp = Date.parse(stage.at);
        progressAt = Number.isFinite(timestamp) ? Math.min(Date.now(), timestamp) : Date.now();
        event = nextEvent;
      }
      const label = hasStage && stage.status === 'started' && typeof stage.label === 'string' && stage.label.trim()
        ? stage.label : fallbackLabel(trip);
      clearTimeout(timer); timer = null;
      const currentVersion = ++version;
      const remaining = HEARTBEAT_AFTER_MS - (Date.now() - progressAt);
      container.hidden = false;
      container.textContent = remaining <= 0 ? `${label} · 仍在处理，请稍候` : label;
      if (remaining > 0) timer = setTimeout(() => {
        if (currentVersion !== version) return;
        timer = null; container.textContent = `${label} · 仍在处理，请稍候`;
      }, remaining);
    }

    return { render, reset };
  }

  return { create, createElapsed };
});
