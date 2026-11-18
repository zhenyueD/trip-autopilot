'use strict';
// Full production bootstrap/event handlers, synthetic DOM and transport only.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const preparation = require('./studio-preparation.js');
class Element {
  constructor() { this.children = []; this.dataset = {}; this.value = ''; this.hidden = false; this.listeners = {}; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return (this.text || '') + this.children.map(c => c.textContent || '').join(''); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this.text = ''; }
  setAttribute(name, value) { this[name] = value; }
  removeAttribute(name) { delete this[name]; }
  get classList() { return { toggle() {}, add() {}, remove() {} }; }
  addEventListener(type, listener) { this.listeners[type] = listener; }
  querySelectorAll() { return []; }
  querySelector() { return null; }
  before() {}
  focus() {}
}
async function boot({ post, status = 'needs_attention', singleSession = false, fresh = false,
                      savedStorage, storageErrorKey, tripFields = {}, plannerAvailable = false, draftSaved = true } = {}) {
  const nodes = new Map(), events = {}, requests = [], timers = new Map(); let timerId = 0;
  const get = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const session = { id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', token: 'synthetic-action-fixture-only' };
  let trip = { id: session.id, revision: 1, status, plan: null, events: [], locked_item_ids: [],
    ...(singleSession ? {capabilities: {single_session: true}} : {}), ...tripFields };
  const storage = savedStorage || new Map(fresh ? [] : [['atom-travel-studio-library-v2', JSON.stringify({ sessions: [session], activeId: session.id })]]);
  const window = {
    addEventListener(type, listener) { events[type] = listener; },
    TravelPreparation: { ...preparation, updateControls() {} },
    TravelJourney: { mount() {}, renderWork() {} }, TravelHandbook: { mount() {} },
    TravelOperationProof: require('./studio-operation-proof.js'),
    TravelIntake: { renderIntake() {}, createComposer() { return { update() {}, reset() {}, preparing: false,
      saveDraft() { return draftSaved; },
      payload() { return { evidence_text: '', images: [], links: [], documents: [] }; },
      hasMaterials() { return false; }, status(message) { get('material-status').textContent = message; } }; } },
    TravelExperience: { renderDiscovery() {} }, TravelPlaceMedia: { reset() {} },
    TravelGoogleMaps: { reset() {}, configure() {} },
    // This fixture exercises the legacy handlers with the maincore feature off.
    TravelMaincore: { mount() { return { known: true, enabled: false, busy: false,
      client: { state: { saved: { drafts: {} } }, saveDraft() {} },
      select() {}, render() {}, pending() { return []; }, async capabilities() {} }; } }
  };
  const context = { window, URL, AbortController, TypeError, TextEncoder, crypto: require('node:crypto').webcrypto,
    location: { href: 'https://fixture.example/studio.html' },
    localStorage: { getItem: key => storage.get(key) || null, removeItem: key => storage.delete(key),
      setItem: (key, value) => { if (key === storageErrorKey) throw new Error('synthetic storage failure'); storage.set(key, value); } },
    document: { getElementById: get, createElement: () => new Element(), createTextNode: text => ({ textContent: text }),
      querySelectorAll: () => [], querySelector: () => null }, requestAnimationFrame() {},
    setTimeout(callback) { timers.set(++timerId, callback); return timerId; }, clearTimeout(id) { timers.delete(id); },
    fetch: async (path, options) => {
      requests.push({ path, method: options.method, body: options.body && JSON.parse(options.body), headers: options.headers });
      if (options.method === 'POST') return post(path, options);
      let data;
      if (path === '/api/agent/capabilities') data = { planner_available: plannerAvailable || singleSession, single_session: singleSession };
      else if (path === '/api/agent/maps-config') data = { configured: false };
      else { assert.equal(path, `/api/agent/trips/${session.id}`); data = { trip }; }
      return { ok: true, json: async () => data };
    }
  };
  vm.runInNewContext(fs.readFileSync(`${__dirname}/studio.js`, 'utf8'), context, { filename: 'studio.js' });
  await new Promise(resolve => setImmediate(resolve));
  return { get, requests, events, timers, session, storage, localStorage: context.localStorage,
    trip: () => trip, setTrip(value) { trip = value; } };
}
const flush = () => new Promise(resolve => setImmediate(resolve));

test('retry whose transport failed keeps unconfirmed warning after online refresh, with no automatic resend', async () => {
  const ui = await boot({ post: async () => { throw new TypeError('synthetic offline'); } });
  assert.equal(ui.get('retry').hidden, false);
  await ui.get('retry').listeners.click();
  assert.match(ui.get('notice-text').textContent, /不会自动重复提交/);
  ui.events.online(); await flush();
  assert.equal(ui.requests.filter(r => r.method === 'POST').length, 1);
  assert.match(ui.get('notice-text').textContent, /提交未确认|不会自动重复提交/);
});

test('blocked retry handler explicitly says no request was sent', async () => {
  const ui = await boot({ status: 'running', post: async () => assert.fail('must not send') });
  // Dispatch the registered handler to cover stale/queued events, not a disabled native click.
  await ui.get('retry').listeners.click();
  assert.equal(ui.requests.filter(r => r.method === 'POST').length, 0);
  assert.match(ui.get('notice-text').textContent, /未发送.*上一轮/);
});

test('a later successful explicit retry clears prior warning and emits the actual browser body', async () => {
  let attempts = 0;
  const ui = await boot({ post: async () => {
    if (++attempts === 1) throw new TypeError('synthetic offline');
    const trip = { id: ui.session.id, revision: 2, status: 'running', plan: null, events: [], locked_item_ids: [] };
    ui.setTrip(trip);
    return { ok: true, json: async () => ({ trip }) };
  } });
  await ui.get('retry').listeners.click();
  await ui.get('retry').listeners.click();
  assert.match(ui.get('notice-text').textContent, /已提交继续研究请求/);
  ui.events.online(); await flush();
  assert.equal(ui.get('notice-text').textContent, '');
  assert.equal(ui.requests.filter(r => r.method === 'POST').length, 2);
  assert.deepEqual(ui.requests.find(r => r.method === 'POST').body, { expected_revision: 1 });
});

test('HTTP failure before persistence remains explicit and revise input survives read-only reconnect', async () => {
  const ui = await boot({ post: async () => ({ ok: false, status: 500,
    headers: { get() { return null; } }, json: async () => ({ error: { message_zh: '合成落盘失败' } }) }) });
  ui.get('feedback').value = '第三天下午晚点一小时出发';
  await ui.get('feedback-form').listeners.submit({ preventDefault() {} });
  ui.events.online(); await flush();
  assert.match(ui.get('notice-text').textContent, /合成落盘失败.*提交未确认/);
  assert.equal(ui.get('feedback').value, '第三天下午晚点一小时出发');
  assert.equal(ui.requests.filter(r => r.method === 'POST').length, 1);
});

test('capture production retry and revise handlers request bodies for offline API replay', async () => {
  const captures = {};
  for (const action of ['retry', 'revise', 'revise_full']) {
    const ui = await boot({ post: async () => { throw new TypeError('capture only'); } });
    if (action === 'retry') await ui.get('retry').listeners.click();
    else {
      ui.get('feedback').value = action === 'revise_full' ? '行程太赶了' : '第三天下午晚点一小时出发';
      await ui.get('feedback-form').listeners.submit({ preventDefault() {} });
    }
    const request = ui.requests.find(r => r.method === 'POST');
    assert.ok(request.path.endsWith('/' + (action === 'retry' ? 'retry' : 'message'))); captures[action] = request.body;
  }
  assert.deepEqual(captures.revise, { feedback: '第三天下午晚点一小时出发', evidence_text: '',
    images: [], links: [], documents: [], expected_revision: 1, locked_item_ids: [] });
  if (process.env.ACTION_CAPTURE_PATH) fs.writeFileSync(process.env.ACTION_CAPTURE_PATH, JSON.stringify(captures, null, 2));
});

test('retry without restored trip is explicitly blocked instead of throwing before mutate', async () => {
  const ui = await boot({ post: async () => assert.fail('must not send') });
  ui.get('new-trip').listeners.click();
  await ui.get('retry').listeners.click();
  assert.match(ui.get('notice-text').textContent, /未发送.*旅行尚未恢复/);
  assert.equal(ui.requests.filter(r => r.method === 'POST').length, 0);
});

test('409 rejection remains distinct from unknown receipt and preserves feedback', async () => {
  const ui = await boot({ post: async () => ({ ok: false, status: 409,
    headers: { get() { return null; } }, json: async () => ({ error: { message_zh: '版本冲突' } }) }) });
  ui.get('feedback').value = '第三天下午晚点一小时出发';
  await ui.get('feedback-form').listeners.submit({ preventDefault() {} });
  ui.events.online(); await flush();
  assert.match(ui.get('notice-text').textContent, /未被接受/);
  assert.doesNotMatch(ui.get('notice-text').textContent, /已提交/);
  assert.equal(ui.get('feedback').value, '第三天下午晚点一小时出发');
});

const CREATE_PROOF = 'atom-travel-studio-create-proof-v1';
const submitCreate = ui => ui.get('brief-form').listeners.submit({ preventDefault() {} });
const acceptedCreate = (ui, options) => ({ ok: true, json: async () => ({
  trip: { id: ui.session.id, revision: 0, status: 'running', capabilities: {single_session: true} },
  token: JSON.parse(options.body).client_token
}) });

test('single-session create persists ownership before POST and reuses it after response loss', async () => {
  let attempts = 0;
  const ui = await boot({fresh: true, singleSession: true, post: async (path, options) => {
    const body = JSON.parse(options.body), proof = JSON.parse(ui.storage.get(CREATE_PROOF));
    assert.equal(path, '/api/agent/trips');
    assert.equal(proof.request_id, body.request_id); assert.equal(proof.client_token, body.client_token);
    assert.equal(options.headers.Authorization, undefined);
    if (++attempts === 1) throw new TypeError('response lost after server accepted');
    return acceptedCreate(ui, options);
  }});
  ui.get('brief').value = '苏州三天'; await submitCreate(ui);
  const pending = JSON.parse(ui.storage.get(CREATE_PROOF));
  assert.match(pending.request_id, /^[a-f0-9]{32}$/); assert.match(pending.client_token, /^[a-f0-9]{64}$/);
  assert.equal(ui.get('new-trip').hidden, false);
  await submitCreate(ui);
  const posts = ui.requests.filter(r => r.method === 'POST');
  assert.equal(posts.length, 2); assert.deepEqual(posts[0].body, posts[1].body);
  assert.equal(ui.storage.has(CREATE_PROOF), false);
  const library = JSON.parse(ui.storage.get('atom-travel-studio-library-v2'));
  assert.equal(library.sessions[0].token, pending.client_token);
  assert.equal(ui.get('notice-text').textContent.includes(pending.client_token), false);
});

test('browser reload preserves pending ownership and never auto-replays create', async () => {
  const first = await boot({fresh: true, singleSession: true, post: async () => { throw new TypeError('lost'); }});
  first.get('brief').value = '相同需求'; await submitCreate(first);
  const proof = first.storage.get(CREATE_PROOF);
  const second = await boot({fresh: true, singleSession: true, savedStorage: first.storage,
    post: async (path, options) => acceptedCreate(second, options)});
  assert.equal(second.requests.some(r => r.method === 'POST'), false);
  assert.equal(second.get('new-trip').hidden, false);
  second.get('brief').value = '相同需求'; await submitCreate(second);
  assert.equal(second.requests.find(r => r.method === 'POST').body.client_token, JSON.parse(proof).client_token);
});

test('changed input cannot receive old ownership; explicit new-trip creates fresh proof', async () => {
  const ui = await boot({fresh: true, singleSession: true, post: async () => { throw new TypeError('lost'); }});
  ui.get('brief').value = '原需求'; await submitCreate(ui);
  const original = ui.storage.get(CREATE_PROOF);
  ui.get('brief').value = '不同需求'; await submitCreate(ui);
  assert.equal(ui.requests.filter(r => r.method === 'POST').length, 1);
  assert.equal(ui.storage.get(CREATE_PROOF), original);
  assert.match(ui.get('notice-text').textContent, /恢复原输入.*新旅行/);
  ui.get('new-trip').listeners.click();
  assert.equal(ui.storage.has(CREATE_PROOF), false);
  await submitCreate(ui);
  const posts = ui.requests.filter(r => r.method === 'POST');
  assert.equal(posts.length, 2);
  assert.equal(posts[1].body.brief, '不同需求');
  assert.notEqual(posts[1].body.request_id, posts[0].body.request_id);
  assert.notEqual(posts[1].body.client_token, posts[0].body.client_token);
});

test('failed durable proof storage blocks POST; legacy provider retains original payload', async () => {
  const blocked = await boot({fresh: true, singleSession: true, storageErrorKey: CREATE_PROOF,
    post: async () => assert.fail('must not send without durable proof')});
  blocked.get('brief').value = '本机存储不可用'; await submitCreate(blocked);
  assert.equal(blocked.requests.filter(r => r.method === 'POST').length, 0);
  assert.match(blocked.get('notice-text').textContent, /本次需求尚未发送/);
  const legacy = await boot({fresh: true, plannerAvailable: true, post: async () => { throw new TypeError('legacy loss'); }});
  legacy.get('brief').value = '旧服务'; await submitCreate(legacy);
  assert.deepEqual(legacy.requests.find(r => r.method === 'POST').body,
    {brief: '旧服务', evidence_text: '', images: [], links: [], documents: []});
  assert.equal(legacy.storage.has(CREATE_PROOF), false);
});

test('failed draft persistence blocks dispatch even when proof storage would work', async () => {
  const ui = await boot({fresh: true, singleSession: true, draftSaved: false,
    post: async () => assert.fail('must not send without recoverable input')});
  ui.get('brief').value = '输入未落盘'; await submitCreate(ui);
  assert.equal(ui.requests.some(request => request.method === 'POST'), false);
  assert.equal(ui.storage.has(CREATE_PROOF), false);
  assert.equal(ui.get('brief').value, '输入未落盘');
  assert.match(ui.get('notice-text').textContent, /原输入未能保存.*尚未发送/);
});

test('incomplete or mismatched response preserves proof and original input', async () => {
  for (const responseToken of [undefined, 'wrong-response-token']) {
    const ui = await boot({fresh: true, singleSession: true, post: async () => ({ok: true,
      json: async () => ({trip: {id: 'a'.repeat(32)}, token: responseToken})})});
    ui.get('brief').value = '保留原输入'; await submitCreate(ui);
    assert.equal(ui.storage.has(CREATE_PROOF), true);
    assert.equal(ui.get('brief').value, '保留原输入');
    assert.equal(ui.storage.has('atom-travel-studio-library-v2'), false);
  }
});

test('confirmed response retains proof if durable session storage fails', async () => {
  const ui = await boot({fresh: true, singleSession: true, storageErrorKey: 'atom-travel-studio-library-v2',
    post: async (path, options) => acceptedCreate(ui, options)});
  ui.get('brief').value = '已接收但会话未落盘'; await submitCreate(ui);
  assert.equal(ui.storage.has(CREATE_PROOF), true);
  assert.equal(ui.storage.has('atom-travel-studio-library-v2'), false);
  assert.equal(ui.get('workspace').hidden, false);
  assert.equal(ui.get('brief').value, '已接收但会话未落盘');
});

test('single-session retry reuses visible control for artifacts or checkpoint with write auth and revision', async () => {
  for (const [status, fields, suffix, label] of [
    ['partial', {document: {version: 1}, delivery: {status: 'partial', retry_url: '/api/agent/trips/' + 'a'.repeat(32) + '/artifacts/retry'}}, '/artifacts/retry', '重试导出文件'],
    ['failed', {generation_status: 'interrupted', document: {version: 1}, delivery: {status: 'partial'}}, '/resume', '继续未完成的准备'],
    ['failed', {generation_status: 'completed', document: {version: 1}, delivery: {status: 'partial', retry_url: '/api/agent/trips/' + 'a'.repeat(32) + '/artifacts/retry'}}, '/artifacts/retry', '重试导出文件'],
    ['failed', {revision: 0}, '/resume', '继续未完成的准备']]) {
    const ui = await boot({singleSession: true, status, tripFields: fields,
      post: async () => { throw new TypeError('captured recovery'); }});
    assert.equal(ui.get('retry').hidden, false); assert.equal(ui.get('retry').textContent, label);
    await ui.get('retry').listeners.click();
    const sent = ui.requests.find(r => r.method === 'POST');
    assert.equal(sent.path, `/api/agent/trips/${ui.session.id}${suffix}`);
    assert.equal(sent.headers.Authorization, `Bearer ${ui.session.token}`);
    assert.equal(sent.body.expected_revision, fields.revision ?? 1);
    assert.equal(ui.requests.filter(r => r.method === 'POST').length, 1);
  }
});

test('saved reader link is shown only for this trip on the same origin', async () => {
  const path = '/api/agent/trips/' + 'a'.repeat(32) + '/handbook?read=reader-capability';
  for (const url of [path, 'https://evil.example' + path, path.replace('a'.repeat(32), 'b'.repeat(32))]) {
    const ui = await boot({singleSession: true, status: 'partial', tripFields: {
      document: {version: 1}, delivery: {status: 'partial'}, handbook_url: url}});
    const readers = ui.get('state-copy').children.filter(child => child.href);
    assert.equal(readers.length, url === path ? 1 : 0);
    if (readers.length) {
      assert.equal(readers[0].href, 'https://fixture.example' + path);
      assert.equal(readers[0].rel, 'noopener noreferrer');
    }
  }
});


test('single-session failure without a document uses actionable Chinese and no invented saved version', async () => {
  const ui = await boot({ singleSession: true, status: 'failed', tripFields: {
    error: { type: 'BudgetExhausted', message: 'Turn or presentation failed SECRET sk-synthetic' }, document: null } });
  assert.match(ui.get('trip-meta').textContent, /尚未保存手册草稿/);
  assert.doesNotMatch(ui.get('trip-meta').textContent, /历史方案版本/);
  assert.match(ui.get('state-copy').textContent, /尚未保存手册草稿.*额度已用完/);
  assert.doesNotMatch(ui.get('state-copy').textContent, /Turn|SECRET|sk-synthetic/);
  assert.doesNotMatch(ui.get('state-title').textContent, /已保存/);
});

test('single-session reader metadata displays actual saved document version', async () => {
  const ui = await boot({ singleSession: true, status: 'completed', tripFields: { document: { version: 3 } } });
  assert.match(ui.get('trip-meta').textContent, /手册版本 3/);
  assert.doesNotMatch(ui.get('trip-meta').textContent, /历史方案版本/);
});

const submitRevision = ui => ui.get('feedback-form').listeners.submit({ preventDefault() {} });
const operationKey = id => `atom-travel-studio-pending-op-${id}`;
const savedDocument = { document: { version: 1, hash: 'a'.repeat(64) } };

test('single-session unknown result reuses original whole body after version/hash advance and reload', async () => {
  const first = await boot({ singleSession: true, tripFields: savedDocument, post: async () => {
    throw Object.assign(new Error('timeout'), { name: 'AbortError' });
  } });
  first.get('feedback').value = '爸爸连续步行上限改为20分钟';
  await submitRevision(first);
  const original = first.requests.find(request => request.method === 'POST').body;
  const proof = JSON.parse(first.storage.get(operationKey(first.session.id)));
  assert.deepEqual(proof.input, Object.fromEntries(Object.entries(original).filter(([key]) => key !== 'request_id')));
  const second = await boot({ singleSession: true, savedStorage: first.storage,
    tripFields: { revision: 2, document: { version: 2, hash: 'b'.repeat(64) }, job: { request_id: original.request_id } },
    post: async () => { throw new TypeError('response loss'); } });
  assert.equal(second.requests.some(request => request.method === 'POST'), false);
  second.get('feedback').value = first.get('feedback').value;
  await submitRevision(second);
  assert.deepEqual(second.requests.find(request => request.method === 'POST').body, original);
  assert.equal(second.get('feedback').value, first.get('feedback').value);
  assert.match(second.get('notice-text').textContent, /不会自动重复提交/);
});

test('single-session storage denial, quota and silent persistence failure block all mutation POSTs', async () => {
  for (const mode of ['read', 'quota', 'silent']) {
    const ui = await boot({ singleSession: true, tripFields: savedDocument,
      post: async () => assert.fail('storage failure must block POST') });
    if (mode === 'read') ui.localStorage.getItem = () => { throw Object.assign(new Error('disabled'), { name: 'SecurityError' }); };
    else ui.localStorage.setItem = () => { if (mode === 'quota') throw Object.assign(new Error('quota'), { name: 'QuotaExceededError' }); };
    ui.get('feedback').value = '步行上限改20分钟';
    await submitRevision(ui);
    assert.equal(ui.requests.filter(request => request.method === 'POST').length, 0);
    assert.match(ui.get('notice-text').textContent, /尚未发送/);
    assert.equal(ui.get('feedback').value, '步行上限改20分钟');
    assert.equal(ui.get('feedback').disabled, false);
  }
});

test('changed input cannot replace unknown operation and queued duplicate click cannot submit twice', async () => {
  let rejectPost;
  const ui = await boot({ singleSession: true, tripFields: savedDocument,
    post: () => new Promise((resolve, reject) => { rejectPost = reject; }) });
  ui.get('feedback').value = '原修订'; const submitting = submitRevision(ui); await flush();
  await submitRevision(ui);
  assert.equal(ui.requests.filter(request => request.method === 'POST').length, 1);
  rejectPost(new TypeError('lost')); await submitting;
  const original = ui.storage.get(operationKey(ui.session.id));
  ui.get('feedback').value = '不同修订'; await submitRevision(ui);
  assert.equal(ui.requests.filter(request => request.method === 'POST').length, 1);
  assert.equal(ui.storage.get(operationKey(ui.session.id)), original);
  assert.match(ui.get('notice-text').textContent, /上一操作尚未确认/);
});

test('reload without local proof reads server job and blocks new identity until explicit review', async () => {
  const ui = await boot({ singleSession: true, tripFields: { ...savedDocument, job: { request_id: 'c'.repeat(32), status: 'completed' } },
    post: async () => assert.fail('must not resend unknown old operation') });
  ui.get('feedback').value = '上次未确认的修改';
  const initialGets = ui.requests.filter(request => request.path.endsWith(ui.session.id)).length;
  await submitRevision(ui);
  assert.ok(ui.requests.filter(request => request.path.endsWith(ui.session.id)).length > initialGets);
  assert.equal(ui.requests.filter(request => request.method === 'POST').length, 0);
  assert.equal(ui.storage.has(operationKey(ui.session.id)), false);
  assert.match(ui.get('notice-text').textContent, /核对当前结果/);
  const button = ui.get('notice').children.find(child => child.id === 'operation-review');
  assert.ok(button); button.listeners.click();
  assert.equal(ui.requests.filter(request => request.method === 'POST').length, 0);
});

test('stale mutation receipt cannot overwrite newer saved document or switch trip', async () => {
  let ui;
  ui = await boot({ singleSession: true, tripFields: { revision: 3, document: { version: 3, hash: 'c'.repeat(64) } },
    post: async () => ({ ok: true, json: async () => ({ kind: 'revision', trip: { ...ui.trip(), revision: 2, document: { version: 2, hash: 'b'.repeat(64) } } }) }) });
  ui.get('feedback').value = '局部修改'; await submitRevision(ui);
  assert.match(ui.get('trip-meta').textContent, /手册版本 3/);
});

test('example fills future-date draft only and result navigation stays hidden without a trip', async () => {
  const ui = await boot({ fresh: true, singleSession: true, post: async () => assert.fail('example does not call model') });
  ui.get('start-demo').listeners.click();
  assert.equal(ui.requests.filter(request => request.method === 'POST').length, 0);
  const date = ui.get('brief').value.match(/(\d{4})年(\d{1,2})月(\d{1,2})日/);
  assert.ok(date);
  assert.ok(new Date(+date[1], +date[2] - 1, +date[3]).valueOf() > Date.now());
  assert.equal(ui.get('handbook-nav').hidden, true);
});

test('completed flags without artifact URLs do not announce complete delivery', async () => {
  const ui = await boot({ singleSession: true, status: 'completed',
    tripFields: { ...savedDocument, delivery: { status: 'ready', quality_gaps: [] } } });
  assert.doesNotMatch(ui.get('state-title').textContent, /导出文件已准备好|全部完成/);
  assert.doesNotMatch(ui.get('state-copy').textContent, /导出文件已准备好/);
});

test('definitive version rejection releases only that proof so corrected input can be new', async () => {
  let attempts = 0; let ui;
  ui = await boot({ singleSession: true, tripFields: savedDocument, post: async () => {
    if (++attempts === 1) return { ok: false, status: 409, headers: { get() {} },
      json: async () => ({ error: { code: 'revision_conflict', message_zh: '版本已变化' } }) };
    return { ok: true, json: async () => ({ kind: 'revision', trip: ui.trip() }) };
  } });
  ui.get('feedback').value = '第一次修改'; await submitRevision(ui);
  assert.equal(ui.storage.has(operationKey(ui.session.id)), false);
  ui.get('feedback').value = '修正后重新提交'; await submitRevision(ui);
  const posts = ui.requests.filter(request => request.method === 'POST');
  assert.equal(posts.length, 2); assert.notEqual(posts[0].body.request_id, posts[1].body.request_id);
});


test('single-session recorded progress is visible without legacy events, and never labels attempts verified', async () => {
  const ui = await boot({ status: 'running', singleSession: true, tripFields: { progress: {
    tool_counts: {search: 3, read_page: 2}, evidence_count: 1, last_activity_at: '2026-11-18T10:05:00Z'
  } } });
  assert.match(ui.get('state-copy').textContent, /检索 3 次/);
  assert.match(ui.get('state-copy').textContent, /读取原文 2 次/);
  assert.match(ui.get('events').textContent, /已登记证据 1 条/);
  assert.doesNotMatch(ui.get('events').textContent, /已核验|已成功|已完成|服务尚未返回/);
  assert.equal(ui.requests.filter(r => r.method === 'POST').length, 0);
});
test('legacy progress event remains authoritative when also given counters', async () => {
  const ui = await boot({ status: 'running', singleSession: true, tripFields: {
    events: [{label: '正在定向修订', at: '2026-11-18T10:05:00Z'}], progress: {tool_counts: {search: 8}}
  } });
  assert.match(ui.get('state-copy').textContent, /正在定向修订/);
  assert.doesNotMatch(ui.get('state-copy').textContent, /检索 8 次/);
});
test('empty or malformed counters do not invent progress', async () => {
  for (const counts of [{}, {search: -1, read_page: 'invalid'}, {search: Infinity}]) {
    const ui = await boot({status: 'running', singleSession: true, tripFields: {progress: {tool_counts: counts}}});
    assert.match(ui.get('state-copy').textContent, /等待服务端进展/);
    assert.match(ui.get('events').textContent, /服务尚未返回/);
  }
});
test('terminal failure is not covered by past progress counters', async () => {
  const ui = await boot({status: 'failed', singleSession: true, tripFields: {
    progress: {tool_counts: {search: 3}, evidence_count: 1}
  }});
  assert.equal(ui.get('state-title').textContent, '本轮尚未生成可保存的手册');
  assert.doesNotMatch(ui.get('state-copy').textContent, /处理中|正在检索|等待服务端进展/);
});
