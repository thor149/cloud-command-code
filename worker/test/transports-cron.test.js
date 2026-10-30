import test from 'node:test';
import assert from 'node:assert/strict';
import { createGithub } from '../src/github.js';
import { createTelegram, resultMessage } from '../src/telegram.js';
import { runCron } from '../src/cron.js';
import { configuration } from '../src/index.js';
import { fixture } from './helpers.js';

test('failed publication keeps its warning separate from the agent summary', async () => {
  const calls = [];
  const telegram = createTelegram('secret', 42, async (url, options) => {
    calls.push(JSON.parse(options.body)); return Response.json({ ok: true });
  });
  const message = resultMessage({ status: 'failed', minutes: 1 }, {
    ok: false, exit_code: 0, text: 'Updated app.py.', error: 'Could not publish the changes: checkout failed.'
  });
  assert.equal(message, 'Updated app.py.');
  await telegram.result({ status: 'failed', minutes: 1 }, {
    ok: false, exit_code: 0, text: 'Updated app.py.', error: 'Could not publish the changes: checkout failed.'
  });
  assert.equal(calls[0].text, message);
  assert.equal(calls[1].text, '⚠️ Could not publish the changes: checkout failed.');
});

test('GitHub dispatch/cancel URLs, headers, exact input payload, HTTP/network failures', async () => {
  const calls = [];
  const github = createGithub('token', async (url, options) => { calls.push({ url, options }); return new Response(null, { status: 204 }); });
  const task = { repo: 'owner/app', base: 'main', workflow: 'ccbot.yml', id: 't_123', run_id: 12 };
  assert.equal((await github.dispatch(task)).ok, true);
  assert.equal(calls[0].url, 'https://api.github.com/repos/owner/app/actions/workflows/ccbot.yml/dispatches');
  assert.deepEqual(JSON.parse(calls[0].options.body), { ref: 'main', inputs: { task_id: 't_123' } });
  assert.deepEqual(calls[0].options.headers, { Authorization: 'Bearer token', Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'cloud-command-code', 'Content-Type': 'application/json' });
  await github.cancel(task); assert.equal(calls[1].url, 'https://api.github.com/repos/owner/app/actions/runs/12/cancel');
  assert.equal(calls[1].options.body, undefined);
  assert.deepEqual(await createGithub('token', async () => Response.json({ message: 'Not Found' }, { status: 404 })).dispatch(task), { ok: false, status: 404, message: 'Not Found' });
  assert.equal((await createGithub('token', async () => { throw new Error('network'); }).dispatch(task)).status, 0);
});

test('long results send a preview then answer.md, with edit metadata in the footer', async () => {
  const calls = [];
  const telegram = createTelegram('secret', 42, async (url, options) => { calls.push({ url, options }); return Response.json({ ok: true }); });
  const task = { id: 't_abc', kind: 'do', status: 'done', minutes: 3, branch: 'ccbot/fix' };
  const result = { ok: true, text: 'x'.repeat(3501), pr_url: 'https://github.com/owner/app/pull/7', tool_calls: 8 };
  assert.equal(await telegram.result(task, result), true);
  assert.match(calls[0].url, /\/sendMessage$/);
  const preview = JSON.parse(calls[0].options.body);
  assert.equal(preview.parse_mode, 'HTML');
  assert.match(preview.text, /^x{1500}\n\n<i>…full answer attached<\/i>/);
  assert.match(preview.text, /🌿 <b>Branch<\/b> <code>ccbot\/fix<\/code>/);
  assert.match(preview.text, /🔀 <b>Pull request<\/b> <a href="https:\/\/github.com\/owner\/app\/pull\/7">#7<\/a> — paste it to Claude for review/);
  assert.match(preview.text, /🛠 <b>8 tool calls<\/b>/);
  assert.match(calls[1].url, /\/sendDocument$/);
  const form = calls[1].options.body;
  assert.equal(form.get('chat_id'), '42');
  assert.equal(form.get('document').name, 'answer.md');
  assert.equal(await form.get('document').text(), result.text);
  assert.deepEqual(JSON.parse(form.get('link_preview_options')), { is_disabled: true });
  await telegram.result({ ...task, kind: 'ask' }, { ok: true, text: '**short**' });
  assert.match(calls[2].url, /\/sendMessage$/);
  assert.equal(JSON.parse(calls[2].options.body).text, '<b>short</b>');
});

test('Telegram HTTP, API, JSON and network errors never throw or expose tokens', async t => {
  const original = console.error; const logs = []; console.error = value => logs.push(value); t.after(() => { console.error = original; });
  for (const fetcher of [async () => Response.json({ ok: false }, { status: 429 }), async () => Response.json({ ok: false }),
    async () => new Response('bad gateway', { status: 502 }), async () => { throw new Error('token-secret'); }]) {
    const telegram = createTelegram('token-secret', 42, fetcher);
    assert.equal(await telegram.send('hello'), false);
    assert.equal(await telegram.result({ id: 't_a', status: 'failed', minutes: 1 }, { ok: false, text: 'x'.repeat(4000) }), false);
  }
  assert.equal(logs.some(x => x.includes('token-secret')), false);
});

test('cron fails queued >20 min and times out running >timeout+15, not at exact boundary; retries do not renotify', async t => {
  const f = fixture(t);
  await f.webhook('/ask queued'); const queued = f.dispatches[0];
  await f.webhook('/use docs'); await f.webhook('/ask running'); const running = f.dispatches[1];
  await f.api('POST', `/api/tasks/${running.id}/started`, { run_id: 99, run_url: 'https://github.com/owner/docs/actions/runs/99' });
  f.advance(1200); await runCron(f.deps); assert.equal(f.results.length, 0);
  f.advance(1); await runCron(f.deps);
  assert.equal((await f.store.task(queued.id)).status, 'failed');
  assert.match(f.results[0].result.text, /workflow never started/);
  f.advance(599); await runCron(f.deps); assert.equal(f.results.length, 1);
  f.advance(1); await runCron(f.deps);
  assert.equal((await f.store.task(running.id)).status, 'timeout'); assert.equal(f.results[1].task.minutes, 31);
  assert.equal(f.cancellations[0].run_id, 99);
  await runCron(f.deps); assert.equal(f.results.length, 2);
});

test('cron respects the 45-minute do timeout and recovers abandoned result claims', async t => {
  const f = fixture(t);
  await f.webhook('/do work'); const task = f.dispatches[0];
  await f.api('POST', `/api/tasks/${task.id}/started`, { run_id: 3, run_url: 'https://github.com/owner/app/actions/runs/3' });
  await f.store.claimResult(task.id, 'crashed-isolate', f.now());
  f.advance(301); await runCron(f.deps);
  assert.equal((await f.store.task(task.id)).status, 'running');
  f.advance(3299); await runCron(f.deps); assert.equal(f.results.length, 0);
  f.advance(1); await runCron(f.deps); assert.equal(f.results.at(-1).task.status, 'timeout');
});

test('cron observation cannot fail a queued task that started concurrently', async t => {
  const f = fixture(t);
  await f.webhook('/ask work'); const task = f.dispatches[0];
  f.advance(1201); const stale = (await f.store.expired(f.now()))[0];
  await f.store.startTask(task.id, { run_id: 9, run_url: 'https://github.com/owner/app/actions/runs/9' }, f.now());
  assert.equal(await f.store.endTask(stale, 'failed', {}, f.now()), null);
  assert.equal((await f.store.task(task.id)).status, 'running');
});

test('configuration rejects aliases for same repo and invalid configuration', () => {
  const repo = { repo: 'owner/repo', base: 'main', workflow: 'ccbot.yml' };
  assert.throws(() => configuration({ REPOS: '{}' }));
  assert.throws(() => configuration({ REPOS: JSON.stringify({ a: repo, b: repo }) }));
  assert.throws(() => configuration({ REPOS: JSON.stringify({ '../x': repo }) }));
  assert.throws(() => configuration({ REPOS: JSON.stringify({ a: repo }), DEFAULT_LIMITS: '{"hourly":-1}' }));
});
