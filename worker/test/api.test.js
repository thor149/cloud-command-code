import test from 'node:test';
import assert from 'node:assert/strict';
import { fixture, resultBody } from './helpers.js';
import { decodeSession, encodeBase64, MAX_SESSION_BYTES, readBody } from '../src/api.js';

test('task GET returns the exact contract without changing task state', async t => {
  const f = fixture(t);
  await f.webhook('/plan Design a cache'); const task = f.dispatches[0];
  const before = await f.store.task(task.id);
  const response = await f.api('GET', `/api/tasks/${task.id}?ignored=1`);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { id: task.id, kind: 'plan', repo: 'owner/app', name: 'app', prompt: 'Design a cache', base: 'main',
    branch: null, continue_branch: false, session_id: null, session: null, model: null, effort: null, edit_mode: 'edits', timeout_minutes: 15 });
  assert.deepEqual(await f.store.task(task.id), before);
  assert.equal((await f.api('POST', `/api/tasks/${task.id}`, {})).status, 405);
});

test('started is idempotent; result is idempotent, rounded and saved without session in D1', async t => {
  const f = fixture(t);
  await f.webhook('/ask Work'); const task = f.dispatches[0];
  const run = { run_id: 12, run_url: 'https://github.com/owner/app/actions/runs/12' };
  assert.equal((await f.api('POST', `/api/tasks/${task.id}/started`, run)).status, 204);
  f.advance(61);
  assert.equal((await f.api('POST', `/api/tasks/${task.id}/started`, run)).status, 204);
  assert.equal(f.messages.filter(m => m.startsWith('Started:')).length, 1);
  const body = resultBody({ session_id: 'session-1', session: btoa('archive') });
  assert.equal((await f.api('POST', `/api/tasks/${task.id}/result`, body)).status, 204);
  const saved = await f.store.task(task.id);
  assert.equal(saved.status, 'done'); assert.equal(saved.minutes, 2);
  assert.equal(JSON.parse(saved.result).session, undefined);
  f.advance(100);
  assert.equal((await f.api('POST', `/api/tasks/${task.id}/result`, '{broken')).status, 204);
  assert.deepEqual(await f.store.task(task.id), saved); assert.equal(f.results.length, 1);
  await f.webhook('/plan Follow up');
  const payload = await (await f.api('GET', `/api/tasks/${f.dispatches[1].id}`)).json();
  assert.equal(payload.session_id, 'session-1'); assert.equal(payload.session, btoa('archive'));
});

test('result with no started callback costs one minute and failure/timeout statuses are distinct', async t => {
  const f = fixture(t);
  for (const [overrides, status] of [[{ ok: false, exit_code: 1 }, 'failed'], [{ ok: false, timed_out: true }, 'timeout'], [{}, 'done']]) {
    await f.webhook('/ask task'); const task = f.dispatches.at(-1);
    f.advance(400);
    await f.api('POST', `/api/tasks/${task.id}/result`, resultBody(overrides));
    const saved = await f.store.task(task.id);
    assert.equal(saved.status, status); assert.equal(saved.minutes, 1);
  }
});

test('/new prevents a late session result from overwriting fresh ask/plan state', async t => {
  const f = fixture(t);
  await f.webhook('/ask old'); const task = f.dispatches[0];
  await f.webhook('/new');
  await f.api('POST', `/api/tasks/${task.id}/result`, resultBody({ session_id: 'old', session: btoa('old archive') }));
  const conv = await f.store.getConversation('owner/app', 'ask');
  assert.equal(conv.epoch, 1); assert.equal(conv.session_id, null);
  await f.webhook('/plan fresh'); const next = f.dispatches[1];
  assert.equal(next.epoch, 1);
  assert.equal((await (await f.api('GET', `/api/tasks/${next.id}`)).json()).session, null);
  assert.equal(f.blobs.has('session:app:ask:0'), true);
  assert.equal(f.blobs.has('session:app:ask:1'), false);
});

test('ask/plan sessions and edit sessions stay separate', async t => {
  const f = fixture(t);
  await f.webhook('/ask question'); const ask = f.dispatches[0];
  await f.api('POST', `/api/tasks/${ask.id}/result`, resultBody({ session_id: 'read', session: btoa('read') }));
  await f.webhook('/do change'); const edit = f.dispatches[1];
  assert.equal((await (await f.api('GET', `/api/tasks/${edit.id}`)).json()).session_id, null);
  await f.api('POST', `/api/tasks/${edit.id}/result`, resultBody({ branch: edit.branch, session_id: 'edit', session: btoa('edit') }));
  await f.webhook('/new');
  assert.equal((await f.store.getConversation('owner/app', 'do')).session_id, 'edit');
});

test('storage failure is retryable; concurrent callbacks do not acknowledge an unfinished write', async t => {
  const f = fixture(t);
  await f.webhook('/ask work'); const task = f.dispatches[0];
  const body = resultBody({ session_id: 's', session: btoa('archive') });
  const put = f.kv.put;
  f.kv.put = async () => { throw new Error('KV failed'); };
  assert.equal((await f.api('POST', `/api/tasks/${task.id}/result`, body)).status, 503);
  assert.equal((await f.store.task(task.id)).status, 'queued');
  assert.equal(f.results.length, 0);
  let resume, entered;
  const holding = new Promise(resolve => { resume = resolve; });
  const writing = new Promise(resolve => { entered = resolve; });
  f.kv.put = async (...args) => { entered(); await holding; await put(...args); };
  const pending = f.api('POST', `/api/tasks/${task.id}/result`, body);
  await writing;
  assert.equal((await f.api('POST', `/api/tasks/${task.id}/result`, body)).status, 503);
  await f.webhook('/ask concurrent'); assert.equal(f.dispatches.length, 1);
  resume(); assert.equal((await pending).status, 204);
  assert.equal(f.results.length, 1);
});

test('KV stale or missing versions cannot poison resumed sessions', async t => {
  const f = fixture(t);
  await f.webhook('/ask work'); const task = f.dispatches[0];
  await f.api('POST', `/api/tasks/${task.id}/result`, resultBody({ session_id: 'new', session: btoa('new') }));
  await f.webhook('/ask follow up'); const next = f.dispatches[1];
  f.blobs.set('session:app:ask:0', { value: new Uint8Array([1]).buffer, metadata: { task_id: 'old', session_id: 'old' } });
  assert.equal((await f.api('GET', `/api/tasks/${next.id}`)).status, 503);
});

test('result validation rejects oversized output, bad session, foreign PR/branch and malformed started data', async t => {
  const f = fixture(t);
  await f.webhook('/ask test'); const task = f.dispatches[0]; const path = `/api/tasks/${task.id}`;
  for (const overrides of [{ text: 'x'.repeat(60001) }, { session: '????', session_id: 's' }, { session: btoa('data') },
    { branch: 'other' }, { pr_url: 'https://github.com/evil/repo/pull/1' }, { tool_calls: -1 }, { ok: 'yes' }]) {
    assert.equal((await f.api('POST', `${path}/result`, resultBody(overrides))).status, 400);
  }
  assert.equal((await f.api('POST', `${path}/result`, '{invalid')).status, 400);
  for (const run of [{ run_id: 0, run_url: 'x' }, { run_id: 12, run_url: 'https://github.com/other/repo/actions/runs/12' }, null]) {
    assert.equal((await f.api('POST', `${path}/started`, run)).status, 400);
  }
  assert.equal((await f.store.task(task.id)).status, 'queued');
});

test('base64 encodes binary chunk boundaries and enforces decoded 20 MB limit', () => {
  const bytes = new Uint8Array(70000); for (let i = 0; i < bytes.length; i++) bytes[i] = i % 256;
  assert.deepEqual(decodeSession(encodeBase64(bytes)), bytes);
  assert.throws(() => decodeSession('aaaa'.repeat(Math.ceil(MAX_SESSION_BYTES / 3) + 1)));
  assert.throws(() => decodeSession('a==='));
  assert.throws(() => decodeSession('a'));
});

test('body reader enforces content-length and streamed-size bounds', async () => {
  await assert.rejects(readBody(new Request('https://x', { method: 'POST', body: 'too long' }), 2), /BODY_TOO_LARGE/);
  await assert.rejects(readBody(new Request('https://x', { method: 'POST', headers: { 'content-length': '100' }, body: 'x' }), 10), /BODY_TOO_LARGE/);
});

test('a full 20 MB session decodes successfully and one decoded byte over is rejected', () => {
  // Build base64 directly, avoiding a second large source buffer in the test.
  const full = 'AAAA'.repeat(Math.floor(MAX_SESSION_BYTES / 3)) + 'AAA=';
  const bytes = decodeSession(full);
  assert.equal(bytes.length, MAX_SESSION_BYTES);
  assert.equal(bytes[0], 0); assert.equal(bytes.at(-1), 0);
  assert.throws(() => decodeSession(full.slice(0, -1) + 'A'), /20 MB/);
});
