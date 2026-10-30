import test from 'node:test';
import assert from 'node:assert/strict';
import { fixture, resultBody } from './helpers.js';
import { parseCommand } from '../src/commands.js';

test('webhook secret, owner/private-chat filters, and update deduplication', async t => {
  const f = fixture(t);
  assert.equal((await f.webhook('/ask test', {}, 'wrong')).status, 401);
  for (const message of [
    { from: { id: 7 }, chat: { id: 42, type: 'private' }, text: 'bad' },
    { from: { id: 42 }, chat: { id: 42, type: 'group' }, text: 'bad' },
    { from: { id: 42 }, chat: { id: 7, type: 'private' }, text: 'bad' },
    { from: { id: 42 }, chat: { id: 42, type: 'private' }, photo: [] }
  ]) assert.equal((await f.webhook('x', { message })).status, 200);
  assert.equal(f.messages.length, 0);
  assert.equal(f.dispatches.length, 0);
  assert.equal((await f.webhook('/ask@MyBot test', { update_id: 100 })).status, 200);
  await f.webhook('/ask duplicate', { update_id: 100 });
  assert.equal(f.dispatches.length, 1);
  assert.equal(f.messages.length, 1);
});

test('commands parse bot suffixes, newlines and plain text', () => {
  assert.deepEqual(parseCommand('/PLAN@My_Bot goal\nmore'), { command: 'plan', args: 'goal\nmore' });
  assert.deepEqual(parseCommand('plain text'), { command: 'ask', args: 'plain text' });
  assert.deepEqual(parseCommand('/done@BotName'), { command: 'done', args: '' });
});

test('queue refuses busy repo, read-only edits, empty/long prompts; independent repos can queue', async t => {
  const f = fixture(t);
  await f.webhook('/ask'); await f.webhook('x'.repeat(4001));
  assert.equal(f.dispatches.length, 0);
  await f.webhook('x'.repeat(4000));
  assert.match(f.dispatches[0].id, /^t_[0-9a-f]{16}$/);
  await f.webhook('/plan busy');
  assert.equal(f.messages.at(-1), 'A task is already running');
  await f.webhook('/use docs'); await f.webhook('/do forbidden');
  assert.match(f.messages.at(-1), /read-only/);
  await f.webhook('/ask documentation');
  assert.equal(f.dispatches.length, 2);
  assert.equal(f.dispatches[1].repo, 'owner/docs');
});

test('D1 unique index refuses simultaneous queue insertions', async t => {
  const f = fixture(t);
  await Promise.all([f.webhook('/ask first'), f.webhook('/ask second')]);
  assert.equal(f.dispatches.length, 1);
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) AS n FROM tasks WHERE status='queued'").get().n, 1);
});

test('dispatch failures terminate the queued task with status and GitHub message', async t => {
  const f = fixture(t);
  f.github.dispatch = async () => ({ ok: false, status: 422, message: 'Workflow does not have workflow_dispatch trigger' });
  await f.webhook('/ask test');
  const task = f.db.sqlite.prepare('SELECT * FROM tasks').get();
  assert.equal(task.status, 'failed'); assert.equal(task.minutes, 1);
  assert.match(f.messages.at(-1), /422.*workflow_dispatch/);
  assert.equal(await f.store.active('owner/app'), null);
});

test('/do retains task branch and session, confirms remote branch, /done creates a fresh epoch/branch', async t => {
  const f = fixture(t);
  await f.webhook('/do Fix the login!');
  const first = f.dispatches[0];
  assert.match(first.branch, /^ccbot\/app-20261015-1200-fix-the-login-[a-f0-9]{8}$/);
  assert.equal(first.continue_branch, false); assert.equal(first.timeout_minutes, 45);
  await f.api('POST', `/api/tasks/${first.id}/result`, resultBody({ branch: first.branch, commits: 2,
    session_id: 'edit-session', session: btoa('fake tar bytes'), pr_url: 'https://github.com/owner/app/pull/7' }));
  await f.webhook('/do Continue editing');
  const second = f.dispatches[1];
  assert.equal(second.branch, first.branch); assert.equal(second.continue_branch, true);
  const payload = await (await f.api('GET', `/api/tasks/${second.id}`)).json();
  assert.equal(payload.session_id, 'edit-session'); assert.equal(atob(payload.session), 'fake tar bytes');
  await f.webhook('/done');
  await f.api('POST', `/api/tasks/${second.id}/result`, resultBody({ branch: second.branch, commits: 1, session_id: 'late', session: btoa('late') }));
  await f.webhook('/do Fix the login!');
  const third = f.dispatches[2];
  assert.notEqual(third.branch, first.branch); assert.equal(third.continue_branch, false); assert.equal(third.epoch, 1);
  assert.equal((await (await f.api('GET', `/api/tasks/${third.id}`)).json()).session, null);
});

test('edit tasks without a pushed branch reuse the name but do not checkout a nonexistent remote branch', async t => {
  const f = fixture(t);
  await f.webhook('/do Explain existing changes');
  const first = f.dispatches[0];
  await f.api('POST', `/api/tasks/${first.id}/result`, resultBody({ branch: first.branch }));
  await f.webhook('/do Now edit');
  assert.equal(f.dispatches[1].branch, first.branch);
  assert.equal(f.dispatches[1].continue_branch, false);
});

test('settings persist, /model default clears override, /status and /repos report configured state', async t => {
  const f = fixture(t, { DEFAULT_MODEL: 'configured/model', DEFAULT_EFFORT: 'high' });
  await f.webhook('/model new/model'); await f.webhook('/effort max');
  await f.webhook('/effort invalid'); assert.match(f.messages.at(-1), /Use \/effort/);
  await f.webhook('/ask Settings test');
  assert.equal(f.dispatches[0].model, 'new/model'); assert.equal(f.dispatches[0].effort, 'max');
  await f.webhook('/model default'); await f.webhook('/model'); assert.equal(f.messages.at(-1), 'model: default');
  await f.webhook('/status'); assert.match(f.messages.at(-1), /queued.*0 min elapsed[\s\S]*Usage:/);
  await f.webhook('/repos'); assert.match(f.messages.at(-1), /→ app.*writable, metered[\s\S]*docs.*read-only, unmetered/);
  await f.webhook('/use missing'); assert.match(f.messages.at(-1), /Unknown repository/);
  await f.webhook('/help'); assert.match(f.messages.at(-1), /\/cancel/);
});

test('/limits supports off, validates syntax and persists each window', async t => {
  const f = fixture(t);
  for (const args of ['daily 0', 'weekly 50', 'daily -5', 'daily 5x', 'daily 2.5', 'daily 5 extra']) {
    await f.webhook(`/limits ${args}`); assert.match(f.messages.at(-1), /Use \/limits/);
  }
  await f.webhook('/limits daily off'); assert.match(f.messages.at(-1), /daily: off/);
  await f.webhook('/limits hourly 12'); await f.webhook('/limits');
  assert.match(f.messages.at(-1), /hourly: 12 min\ndaily: off\nmonthly: 1800 min/);
});

for (const window of ['hourly', 'daily', 'monthly']) test(`metered tasks enforce the ${window} limit with free-up date`, async t => {
  const f = fixture(t);
  await f.seedUsage(20);
  for (const w of ['hourly', 'daily', 'monthly']) await f.webhook(`/limits ${w} ${w === window ? '20' : 'off'}`);
  await f.webhook('/ask blocked'); assert.equal(f.dispatches.length, 0);
  assert.match(f.messages.at(-1), new RegExp(`${window} limit reached.*frees up 2026-`));
  await f.webhook('/use docs'); await f.webhook('/ask unmetered'); assert.equal(f.dispatches.length, 1);
  await f.webhook('/usage'); assert.match(f.messages.at(-1), new RegExp(`${window}: 20 / 20 min`));
  if (window === 'monthly') assert.match(f.messages.at(-1), /min\/day/);
});

test('/cancel charges rounded minutes, calls GitHub cancellation, rejects fetch and ignores late result', async t => {
  const f = fixture(t);
  await f.webhook('/ask work'); const task = f.dispatches[0];
  await f.api('POST', `/api/tasks/${task.id}/started`, { run_id: 123, run_url: 'https://github.com/owner/app/actions/runs/123' });
  f.advance(61); await f.webhook('/cancel');
  assert.equal(f.results.at(-1).task.status, 'cancelled'); assert.equal(f.results.at(-1).task.minutes, 2);
  assert.equal(f.cancellations[0].run_id, 123);
  assert.equal((await f.api('GET', `/api/tasks/${task.id}`)).status, 410);
  assert.equal((await f.api('POST', `/api/tasks/${task.id}/result`, resultBody())).status, 204);
  assert.equal(f.results.length, 1);
  await f.webhook('/cancel'); assert.match(f.messages.at(-1), /No active task/);
});

test('/cancel handles a queued task starting concurrently', async t => {
  const f = fixture(t);
  await f.webhook('/ask work'); const task = f.dispatches[0];
  const end = f.store.endTask;
  let raced = false;
  f.store.endTask = async (...args) => {
    if (!raced) {
      raced = true;
      await f.store.startTask(task.id, { run_id: 18, run_url: 'https://github.com/owner/app/actions/runs/18' }, f.now());
    }
    return end(...args);
  };
  await f.webhook('/cancel');
  assert.equal((await f.store.task(task.id)).status, 'cancelled');
  assert.equal(f.cancellations[0].run_id, 18);
});
