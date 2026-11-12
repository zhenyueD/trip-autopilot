'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const contract = require('./session-contract.js');

test('intake maps every 7392 field without inventing optional brief', () => {
  const result = contract.buildIntake({ destination: ' 南法与北意 ', start_date: '2027-05-10', end_date: '2027-05-18', travelers: '4', composition: '夫妻与父母', pace: '舒缓', preferences: '不自驾', brief: '' });
  assert.deepEqual(result, { destination: '南法与北意', start_date: '2027-05-10', end_date: '2027-05-18', travelers: 4, composition: '夫妻与父母', pace: '舒缓', preferences: '不自驾', links: [], documents: [], images: [] });
});
test('intake rejects reversed dates and non-integer travelers', () => {
  assert.throws(() => contract.buildIntake({ destination: '杭州', start_date: '2027-05-11', end_date: '2027-05-10', travelers: 2, composition: '成人', pace: '适中' }), /结束日期/);
  assert.throws(() => contract.buildIntake({ destination: '杭州', start_date: '2027-05-10', end_date: '2027-05-11', travelers: 2.5, composition: '成人', pace: '适中' }), /整数/);
});
test('revision binds original version, hash and request id', () => {
  const trip = { id: 'a'.repeat(32), document: { version: 3, hash: 'abc' } };
  assert.deepEqual(contract.buildRevision(trip, '第三天慢一点', 'request-1'), { message: '第三天慢一点', expected_revision: 3, expected_hash: 'abc', request_id: 'request-1' });
  assert.throws(() => contract.buildRevision({ id: trip.id }, '第三天慢一点', 'request-2'), /版本/);
});
test('document URLs accept only 7392 capability routes', () => {
  const id = 'b'.repeat(32);
  assert.ok(contract.safeDocumentURL(`/api/agent/trips/${id}/handbook?read=abc_DEF-123`));
  assert.ok(contract.safeDocumentURL(`/api/agent/trips/${id}/pdf?version=2&read=abc_DEF-123`));
  assert.equal(contract.safeDocumentURL('https://example.com/steal'), '');
  assert.equal(contract.safeDocumentURL(`/api/agent/trips/${id}/handbook?read=x&next=/delete`), '');
});
test('progress and receipt preserve returned values', () => {
  const rows = contract.progressRows({ progress: { tool_counts: { search: 2 }, evidence_count: 5 } });
  assert.deepEqual(rows[0], ['公开搜索', 2, '次']); assert.deepEqual(rows[2], ['登记证据', 5, '条']);
  assert.deepEqual(contract.receiptSections({ changes: ['改第 3 天'], judgment: '保留休息日' }).map(row => row.title), ['改了什么', '一句判断']);
  assert.equal(contract.displayValue(['一', '二']), '一；二');
});
