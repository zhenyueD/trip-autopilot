'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const proof = require('./studio-operation-proof.js');
globalThis.crypto ||= require('node:crypto').webcrypto;
const tripId = 'd'.repeat(32);
const storage = () => { const map = new Map(); return { getItem: k => map.get(k) ?? null,
  setItem: (k, v) => map.set(k, String(v)), removeItem: k => map.delete(k), map }; };
const input = { feedback: '爸爸连续步行上限改为20分钟', expected_revision: 3, expected_hash: 'a'.repeat(64), documents: [] };
const bind = (store, body = input) => proof.bind({ tripId, kind: 'message', input: body, storage: store });

test('unknown replay retains original request, version, hash and complete payload', () => {
  const store = storage(); const first = bind(store);
  const replay = bind(store, { ...input, expected_revision: 4, expected_hash: 'b'.repeat(64) });
  assert.equal(replay.request_id, first.request_id);
  assert.deepEqual(replay.input, input);
  assert.equal(replay.replay, true);
  assert.deepEqual(proof.status({ tripId, storage: store }).input, input);
});

test('changed input cannot replace an unknown operation; matching confirmation releases it', () => {
  const store = storage(); const first = bind(store);
  assert.throws(() => bind(store, { ...input, feedback: '另一个修改' }), /未确认|核查/);
  assert.equal(proof.confirm({ tripId, storage: store, requestId: 'f'.repeat(32) }), false);
  assert.equal(proof.status({ tripId, storage: store }).request_id, first.request_id);
  assert.equal(proof.confirm({ tripId, storage: store, requestId: first.request_id }), true);
  assert.notEqual(bind(store).request_id, first.request_id);
});

test('disabled reads, QuotaExceededError and silent writes block before send', () => {
  for (const mode of ['read', 'quota', 'silent']) {
    const store = storage();
    if (mode === 'read') store.getItem = () => { throw Object.assign(new Error('disabled'), { name: 'SecurityError' }); };
    else store.setItem = () => { if (mode === 'quota') throw Object.assign(new Error('full'), { name: 'QuotaExceededError' }); };
    assert.throws(() => bind(store), /尚未发送|核查/, mode);
  }
});

test('lost or corrupt persisted proof never silently creates a replacement identity', () => {
  const store = storage(); const first = bind(store);
  store.map.clear();
  assert.throws(() => bind(store), /核查|丢失/);
  store.map.set(`atom-travel-studio-pending-op-${tripId}`, '{broken');
  assert.throws(() => bind(store), /核查|损坏/);
  assert.match(first.request_id, /^[a-f0-9]{32}$/);
});

test('fresh module after reload reads complete durable payload without sending', () => {
  const store = storage(); const first = bind(store);
  delete require.cache[require.resolve('./studio-operation-proof.js')];
  const fresh = require('./studio-operation-proof.js');
  const pending = fresh.status({ tripId, storage: store });
  assert.equal(pending.request_id, first.request_id);
  assert.deepEqual(pending.input, input);
  assert.equal(fresh.bind({ tripId, kind: 'message', input, storage: store }).request_id, first.request_id);
});

test('legacy proof without original payload blocks replay rather than guessing it', () => {
  const store = storage();
  store.map.set(`atom-travel-studio-pending-op-${tripId}`, JSON.stringify({ request_id: 'a'.repeat(32), kind: 'message', fingerprint: proof.digest({ kind: 'message', input }) }));
  assert.throws(() => bind(store), /核查|原始/);
});

test('failed confirm removal retains proof and reports recovery requirement', () => {
  const store = storage(); const first = bind(store);
  store.removeItem = () => {};
  assert.throws(() => proof.confirm({ tripId, storage: store, requestId: first.request_id }), /核查|清除/);
  assert.equal(proof.status({ tripId, storage: store }).request_id, first.request_id);
});
