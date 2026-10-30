import test from 'node:test';
import assert from 'node:assert/strict';
import { configuration } from '../src/index.js';
import { fixture, resultBody } from './helpers.js';

test('execution fails closed when omitted or mistyped without breaking configuration', t => {
  const f = fixture(t);
  const env = { ...f.env };
  delete env.TASK_EXECUTION;
  assert.equal(configuration(env).execution, 'paused');
  for (const value of ['', 'true', 'enable', 'Enabled']) {
    assert.equal(configuration({ ...env, TASK_EXECUTION: value }).execution, 'paused');
  }
});

test('pause blocks every model command without dispatching, creating tasks or changing sessions', async t => {
  const f = fixture(t, { TASK_EXECUTION: 'paused' });
  const conv = await f.store.conversation('owner/app', 'ask', f.now());
  for (const text of ['/ask question', '/plan goal', '/do change', 'plain question', '/ask@MyBot question']) {
    assert.equal((await f.webhook(text)).status, 200);
    assert.match(f.messages.at(-1), /paused to prevent model\/API spending/);
  }
  assert.equal(f.dispatches.length, 0);
  assert.equal(f.db.sqlite.prepare('SELECT COUNT(*) AS n FROM tasks').get().n, 0);
  assert.deepEqual(await f.store.getConversation('owner/app', 'ask'), conv);
  assert.equal(await f.store.getConversation('owner/app', 'do'), null);
});

test('controls work even with a mistyped pause setting and model overrides cannot enable execution', async t => {
  const f = fixture(t, { TASK_EXECUTION: 'enable' });
  await f.webhook('/model expensive/model');
  await f.webhook('/model default');
  await f.webhook('/use docs');
  await f.webhook('/status');
  assert.match(f.messages.at(-1), /AI execution<\/b> paused/);
  assert.match(f.messages.at(-1), /owner\/docs/);
  await f.webhook('/help');
  assert.match(f.messages.at(-1), /AI tasks paused/);
  await f.webhook('/repos');
  assert.match(f.messages.at(-1), /owner\/app/);
  await f.webhook('/ask attempt');
  assert.equal(f.dispatches.length, 0);
});

test('queued runners cannot fetch or start after pause; cancellation remains available', async t => {
  const f = fixture(t);
  await f.webhook('/do queued');
  const task = f.dispatches[0];
  f.config.execution = 'paused';
  assert.equal((await f.api('GET', `/api/tasks/${task.id}`)).status, 503);
  assert.equal((await f.api('POST', `/api/tasks/${task.id}/started`, {
    run_id: 1, run_url: 'https://github.com/owner/app/actions/runs/1'
  })).status, 503);
  assert.equal((await f.store.task(task.id)).status, 'queued');
  await f.webhook('/cancel');
  assert.equal((await f.store.task(task.id)).status, 'cancelled');
  assert.equal((await f.api('GET', `/api/tasks/${task.id}`)).status, 410);
});

test('pause retains in-flight results and session archives; explicit enabling resumes the session', async t => {
  const f = fixture(t);
  await f.webhook('/ask first');
  const task = f.dispatches[0];
  await f.api('POST', `/api/tasks/${task.id}/started`, {
    run_id: 1, run_url: 'https://github.com/owner/app/actions/runs/1'
  });
  f.config.execution = 'paused';
  // The trusted publisher can read a running task, but a second agent cannot start it.
  assert.equal((await f.api('GET', `/api/tasks/${task.id}`)).status, 200);
  assert.equal((await f.api('POST', `/api/tasks/${task.id}/started`, {
    run_id: 2, run_url: 'https://github.com/owner/app/actions/runs/2'
  })).status, 503);
  assert.equal((await f.api('POST', `/api/tasks/${task.id}/result`, resultBody({
    session_id: 'persistent-session', session: btoa('saved transcript')
  }))).status, 204);
  await f.webhook('/ask blocked');
  assert.equal(f.dispatches.length, 1);
  f.config.execution = 'enabled';
  await f.webhook('/plan continue');
  const response = await f.api('GET', `/api/tasks/${f.dispatches[1].id}`);
  const payload = await response.json();
  assert.equal(payload.session_id, 'persistent-session');
  assert.equal(payload.session, btoa('saved transcript'));
});

test('a manually re-run completed workflow cannot bypass pause using an old task id', async t => {
  const f = fixture(t);
  await f.webhook('/ask old');
  const task = f.dispatches[0];
  await f.api('POST', `/api/tasks/${task.id}/result`, resultBody());
  f.config.execution = 'paused';
  assert.equal((await f.api('GET', `/api/tasks/${task.id}`)).status, 503);
  assert.equal((await f.api('POST', `/api/tasks/${task.id}/started`, {
    run_id: 2, run_url: 'https://github.com/owner/app/actions/runs/2'
  })).status, 503);
  // Duplicate results may still be acknowledged; no model work follows that callback.
  assert.equal((await f.api('POST', `/api/tasks/${task.id}/result`, resultBody())).status, 204);
  assert.equal((await f.store.task(task.id)).status, 'done');
});
