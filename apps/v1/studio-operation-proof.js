'use strict';

// Durable identity and original payload for one unconfirmed mutation per trip.
// Storage failure blocks sending. Only a matching receipt releases an identity.
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.TravelOperationProof = api;
})(typeof window === 'undefined' ? null : window, function () {
  const key = tripId => `atom-travel-studio-pending-op-${tripId}`;
  const remembered = new WeakMap();
  const clone = value => JSON.parse(JSON.stringify(value));
  const canonical = value => Array.isArray(value) ? value.map(canonical)
    : value && typeof value === 'object'
      ? Object.fromEntries(Object.keys(value).sort().map(field => [field, canonical(value[field])])) : value;
  // Exact canonical equality is the replay gate, not a short hash collision.
  const digest = input => JSON.stringify(canonical(input));
  const intent = input => {
    const { expected_revision, expected_hash, request_id, ...rest } = input;
    return rest;
  };
  const blocked = message => Object.assign(new Error(message), { operationBlocked: true });
  function storageOf(storage) {
    try { return storage || globalThis.localStorage; }
    catch { throw blocked('无法读取本机提交凭证，本次操作尚未发送。请恢复存储后核查任务。'); }
  }
  function memory(store) {
    if (!remembered.has(store)) remembered.set(store, new Map());
    return remembered.get(store);
  }
  function versionBinding(trip) {
    if (!trip?.capabilities?.single_session) return {};
    return { ...(Number.isInteger(trip.revision) ? { expected_revision: trip.revision } : {}),
      ...(trip.document?.hash ? { expected_hash: trip.document.hash } : {}) };
  }
  function read(store, tripId) {
    let raw;
    try { raw = store.getItem(key(tripId)); }
    catch { throw blocked('无法读取本机提交凭证，本次操作尚未发送。请恢复存储后核查任务。'); }
    if (!raw) {
      if (memory(store).has(tripId)) throw blocked('未确认操作的凭证已丢失，请先核查任务；不会重新生成请求编号。');
      return null;
    }
    let pending;
    try { pending = JSON.parse(raw); } catch { throw blocked('本机提交凭证已损坏，请先核查任务，本次操作尚未发送。'); }
    if (!pending || !/^[a-f0-9]{32}$/.test(pending.request_id || '') || !pending.input
        || typeof pending.input !== 'object' || typeof pending.kind !== 'string')
      throw blocked('提交凭证缺少原始载荷，请先核查任务，本次操作尚未发送。');
    memory(store).set(tripId, pending.request_id);
    return pending;
  }
  function bind({ tripId, kind, input, storage, random } = {}) {
    const store = storageOf(storage), pending = read(store, tripId);
    if (pending) {
      if (pending.kind !== kind || digest(intent(pending.input)) !== digest(intent(input)))
        throw blocked('上一操作尚未确认，请先核查结果或恢复原输入；新内容尚未发送。');
      return { ...clone(pending), replay: true };
    }
    const fill = random || (bytes => globalThis.crypto.getRandomValues(bytes));
    const request_id = Array.from(fill(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
    const next = { kind, input: clone(input), request_id, created_at: new Date().toISOString() };
    const serialized = JSON.stringify(next);
    try {
      store.setItem(key(tripId), serialized);
      if (store.getItem(key(tripId)) !== serialized) throw new Error('read-back mismatch');
    } catch { throw blocked('浏览器未能可靠保存提交凭证，本次操作尚未发送。请恢复存储后核查任务。'); }
    memory(store).set(tripId, request_id);
    return { ...clone(next), replay: false };
  }
  function confirm({ tripId, storage, requestId } = {}) {
    const store = storageOf(storage), pending = read(store, tripId);
    if (!pending || pending.request_id !== requestId) return false;
    try {
      store.removeItem(key(tripId));
      if (store.getItem(key(tripId)) !== null) throw new Error('remove read-back mismatch');
    } catch { throw blocked('结果已返回，但未能清除本机提交凭证。请先核查任务，暂勿重复提交。'); }
    memory(store).delete(tripId);
    return true;
  }
  const status = ({ tripId, storage } = {}) => read(storageOf(storage), tripId);
  return { bind, confirm, status, versionBinding, digest };
});
