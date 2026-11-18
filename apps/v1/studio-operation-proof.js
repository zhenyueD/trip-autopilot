'use strict';

// Operation identity for single-session mutations (revise / message / resume /
// artifacts retry). One persisted pending operation per trip: the same undetermined
// submission keeps its request_id for replay, a changed input mints a new id, and a
// confirmed result clears the pending proof. Never auto-resends by itself.
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.TravelOperationProof = api;
})(typeof window === 'undefined' ? null : window, function () {
  const key = tripId => `atom-travel-studio-pending-op-${tripId}`;
  const hex = bytes => Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  const canonical = value => Array.isArray(value) ? value.map(canonical)
    : value && typeof value === 'object'
      ? Object.fromEntries(Object.keys(value).sort().map(field => [field, canonical(value[field])])) : value;
  const digest = input => {
    const json = JSON.stringify(canonical(input));
    // FNV-1a 64-bit — deterministic, dependency-free; collision-safe for replay gating.
    let hash = 0xcbf29ce484222325n;
    for (let index = 0; index < json.length; index++) {
      hash ^= BigInt(json.charCodeAt(index));
      hash = (hash * 0x100000001b3n) & 0xffffffffffffffffn;
    }
    return hash.toString(16).padStart(16, '0');
  };
  const storageOf = storage => storage || globalThis.localStorage;

  function versionBinding(trip) {
    if (!trip?.capabilities?.single_session || !trip.document) return {};
    const binding = {};
    if (Number.isInteger(trip.revision)) binding.expected_revision = trip.revision;
    if (typeof trip.document.hash === 'string' && trip.document.hash) binding.expected_hash = trip.document.hash;
    return binding;
  }

  function read(storage, tripId) {
    try {
      const pending = JSON.parse(storageOf(storage).getItem(key(tripId)));
      return pending && typeof pending === 'object' && /^[a-f0-9]{32}$/.test(pending.request_id || '') ? pending : null;
    } catch (_) { return null; }
  }

  // Same input as the persisted undetermined operation → reuse its request_id
  // (replay / status read). Any other state → a fresh operation identity.
  function bind({ tripId, kind, input, storage, random } = {}) {
    const store = storageOf(storage);
    const pending = read(store, tripId);
    const fingerprint = digest({ kind, input });
    if (pending && pending.kind === kind && pending.fingerprint === fingerprint) return { ...pending, replay: true };
    const next = { kind, fingerprint, request_id: '', created_at: new Date().toISOString() };
    const fill = random || (bytes => globalThis.crypto.getRandomValues(bytes));
    next.request_id = hex(fill(new Uint8Array(16)));
    try { store.setItem(key(tripId), JSON.stringify(next)); } catch (_) { /* Storage full: submit without persistence. */ }
    return { ...next, replay: false };
  }

  function confirm({ tripId, storage } = {}) {
    try { storageOf(storage).removeItem(key(tripId)); } catch (_) { /* Already gone. */ }
  }

  function status({ tripId, storage } = {}) {
    return read(storageOf(storage), tripId);
  }

  return { bind, confirm, status, versionBinding, digest };
});
