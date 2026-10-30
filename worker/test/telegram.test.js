import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { createTelegram, statusMessage } from '../src/telegram.js';
import { fixture, resultBody } from './helpers.js';
import { runCron } from '../src/cron.js';
import { assertBalanced } from './format.test.js';

function transport(respond) {
  const calls = [];
  let nextId = 100;
  const telegram = createTelegram('private-token', 42, async (url, options) => {
    const method = url.split('/').at(-1);
    const body = typeof options.body === 'string' ? JSON.parse(options.body) : options.body;
    const call = { method, body }; calls.push(call);
    const response = await respond?.(call, calls.length);
    return response || Response.json({ ok: true, result: { message_id: body.message_id ?? nextId++ } });
  });
  return { telegram, calls };
}

const task = { id: 't_abc', kind: 'ask', name: 'ccbot', prompt: 'Explain **code** <please>', status: 'queued', minutes: 2 };
const run = { run_id: 7, run_url: 'https://github.com/owner/app/actions/runs/7' };

test('formatted sends and edits disable previews; 400 retries plain text including link targets', async () => {
  const tx = transport((call, index) => index % 2 ? Response.json({ ok: false, description: "can't parse entities" }, { status: 400 }) : null);
  const markdown = '**bold** <x> [docs](https://example.com)';
  assert.equal(await tx.telegram.send(markdown), true);
  assert.equal(tx.calls[0].body.parse_mode, 'HTML');
  assert.equal(tx.calls[1].body.parse_mode, undefined);
  assert.equal(tx.calls[1].body.text, markdown);
  assert.equal((await tx.telegram.status({ ...task, status: 'running', status_message_id: 99, ...run })).ok, true);
  assert.deepEqual(tx.calls.map(c => c.method), ['sendMessage', 'sendMessage', 'editMessageText', 'editMessageText']);
  assert.equal(tx.calls[3].body.parse_mode, undefined);
  assert.equal(tx.calls[3].body.message_id, 99);
  assert.match(tx.calls[3].body.text, /view run \(https:\/\/github.com\/owner\/app\/actions\/runs\/7\)/);
  for (const call of tx.calls) assert.deepEqual(call.body.link_preview_options, { is_disabled: true });
});

test('edit failures send replacement status; not-modified is successful without a duplicate', async () => {
  const tx = transport(call => call.method === 'editMessageText' ? Response.json({ ok: false }, { status: 403 }) : null);
  assert.equal((await tx.telegram.status({ ...task, status_message_id: 9 })).message_id, 100);
  assert.deepEqual(tx.calls.map(c => c.method), ['editMessageText', 'sendMessage']);
  const unchanged = transport(() => Response.json({ ok: false, description: 'Bad Request: message is not modified' }, { status: 400 }));
  assert.equal((await unchanged.telegram.status({ ...task, status_message_id: 9 })).ok, true);
  assert.equal(unchanged.calls.length, 1);
});

test('missing-message 400 attempts plain edit then replacement; replacement can also fall back', async () => {
  const tx = transport((call, index) => index < 4 ? Response.json({ ok: false }, { status: 400 }) : null);
  const sent = await tx.telegram.status({ ...task, status_message_id: 9 });
  assert.equal(sent.ok, true); assert.equal(sent.message_id, 100);
  assert.deepEqual(tx.calls.map(c => c.method), ['editMessageText', 'editMessageText', 'sendMessage', 'sendMessage']);
  assert.equal(tx.calls.at(-1).body.parse_mode, undefined);
});

test('queue status is bounded, escaped, and terminal statuses retain the run link', () => {
  assert.equal(statusMessage(task), '⏳ <b>Queued</b> · /ask · <code>ccbot</code>\n<i>Explain **code** &lt;please&gt;</i>');
  assert.equal(statusMessage({ ...task, prompt: 'first\nsecond' }).split('\n')[1], '<i>first second</i>');
  const queued = statusMessage({ ...task, kind: 'plan', prompt: '🚀'.repeat(201) });
  assert.match(queued, /\/plan/);
  assert.equal(Array.from(queued.match(/<i>(.*)<\/i>/)[1]).length, 200);
  assert.equal(statusMessage({ ...task, status: 'done' }, { ok: true }), '✅ <b>Done</b> · 2 min');
  assert.equal(statusMessage({ ...task, status: 'failed' }, { exit_code: 3, error: 'not authenticated' }), '❌ <b>Failed</b> (exit 3: not authenticated)');
  for (const [status, prefix] of [['running', '▶️ <b>Running</b>'], ['cancelled', '🛑 <b>Cancelled</b>'], ['timeout', '⏱ <b>Timed out</b>'], ['failed', '❌ <b>Failed</b>']]) {
    const html = statusMessage({ ...task, status, ...run }, { error: '<oops> & failed' });
    assert.ok(html.startsWith(prefix)); assert.match(html, /<a href="https:\/\/github.com\/owner\/app\/actions\/runs\/7">view run<\/a>/);
    assertBalanced(html);
  }
});

test('one persisted status is sent before dispatch, edited on started/done; answer is separate', async t => {
  const f = fixture(t), tx = transport(); f.deps.telegram = tx.telegram;
  f.github.dispatch = async queued => {
    assert.equal((await f.store.task(queued.id)).status_message_id, 100);
    f.dispatches.push(queued); return { ok: true };
  };
  await f.webhook('/ask explain'); const queued = f.dispatches[0];
  assert.equal((await f.store.task(queued.id)).status_message_id, 100);
  await f.api('POST', `/api/tasks/${queued.id}/started`, run);
  await f.api('POST', `/api/tasks/${queued.id}/started`, run);
  f.advance(61);
  await f.api('POST', `/api/tasks/${queued.id}/result`, resultBody({ text: '**Answer**' }));
  await f.api('POST', `/api/tasks/${queued.id}/result`, resultBody());
  assert.deepEqual(tx.calls.map(c => c.method), ['sendMessage', 'editMessageText', 'editMessageText', 'sendMessage']);
  assert.equal(tx.calls[1].body.message_id, 100); assert.equal(tx.calls[2].body.message_id, 100);
  assert.match(tx.calls[2].body.text, /^✅ <b>Done<\/b> · 2 min/);
  assert.equal(tx.calls[3].body.text, '<b>Answer</b>');
  assert.equal((await f.store.task(queued.id)).status_message_id, 100);
  for (const call of tx.calls) {
    assert.deepEqual(call.body.link_preview_options, { is_disabled: true });
    assert.equal(call.body.parse_mode, 'HTML');
  }
});

test('replacement status ID is persisted so later transitions edit the replacement', async t => {
  const f = fixture(t);
  let failOnce = true;
  const tx = transport(call => {
    if (call.method === 'editMessageText' && failOnce) { failOnce = false; return Response.json({ ok: false }, { status: 403 }); }
  }); f.deps.telegram = tx.telegram;
  await f.webhook('/ask explain'); const queued = f.dispatches[0];
  await f.api('POST', `/api/tasks/${queued.id}/started`, run);
  assert.equal((await f.store.task(queued.id)).status_message_id, 101);
  await f.api('POST', `/api/tasks/${queued.id}/result`, resultBody());
  assert.equal(tx.calls.at(-2).method, 'editMessageText');
  assert.equal(tx.calls.at(-2).body.message_id, 101);
});

test('queue delivery failure recovers at started and still dispatches the task', async t => {
  const f = fixture(t), tx = transport((call, index) => index === 1 ? Response.json({ ok: false }, { status: 503 }) : null);
  f.deps.telegram = tx.telegram;
  await f.webhook('/ask explain'); const queued = f.dispatches[0];
  assert.equal((await f.store.task(queued.id)).status_message_id, null);
  await f.api('POST', `/api/tasks/${queued.id}/started`, run);
  assert.equal(tx.calls[1].method, 'sendMessage');
  assert.equal((await f.store.task(queued.id)).status_message_id, 100);
});

test('a delayed running edit reconciles to a concurrently completed task', async t => {
  const f = fixture(t);
  let release, enter;
  const entered = new Promise(resolve => { enter = resolve; });
  const tx = transport(async call => {
    if (call.method === 'editMessageText' && call.body.text.includes('<b>Running</b>')) {
      enter(); await new Promise(resolve => { release = resolve; });
    }
  }); f.deps.telegram = tx.telegram;
  await f.webhook('/ask explain'); const queued = f.dispatches[0];
  const started = f.api('POST', `/api/tasks/${queued.id}/started`, run);
  await entered;
  await f.api('POST', `/api/tasks/${queued.id}/result`, resultBody());
  release(); await started;
  assert.equal(tx.calls.at(-1).method, 'editMessageText');
  assert.match(tx.calls.at(-1).body.text, /^✅ <b>Done<\/b>/);
});

for (const outcome of ['cancelled', 'timeout', 'failed', 'dispatch-failed']) test(`${outcome} edits the live status and sends its answer separately`, async t => {
  const f = fixture(t), tx = transport(); f.deps.telegram = tx.telegram;
  if (outcome === 'dispatch-failed') f.github.dispatch = async () => ({ ok: false, status: 422, message: 'No trigger' });
  await f.webhook('/ask explain'); const queued = f.db.sqlite.prepare('SELECT * FROM tasks').get();
  if (outcome === 'cancelled' || outcome === 'timeout') await f.api('POST', `/api/tasks/${queued.id}/started`, run);
  if (outcome === 'cancelled') await f.webhook('/cancel');
  if (outcome === 'timeout') { f.advance(1801); await runCron(f.deps); }
  if (outcome === 'failed') { f.advance(1201); await runCron(f.deps); }
  const saved = await f.store.task(queued.id);
  assert.equal(saved.status, outcome === 'dispatch-failed' ? 'failed' : outcome);
  const last = tx.calls.slice(-2);
  assert.deepEqual(last.map(c => c.method), ['editMessageText', 'sendMessage']);
  assert.equal(last[0].body.message_id, 100);
  assert.ok(last[1].body.text.length > 0);
  if (saved.run_id) assert.match(last[0].body.text, /view run/);
});

test('a failed terminal status edit/send never prevents sending the actual answer', async t => {
  const f = fixture(t);
  const tx = transport((call, index) => index > 1 && !call.body.text.includes('Essential answer') ? Response.json({ ok: false }, { status: 403 }) : null);
  f.deps.telegram = tx.telegram;
  await f.webhook('/ask explain'); const queued = f.dispatches[0];
  await f.api('POST', `/api/tasks/${queued.id}/result`, resultBody({ text: 'Essential answer' }));
  assert.equal(tx.calls.at(-1).body.text, 'Essential answer');
});

test('answers rejected as HTML are delivered as plain text and long documents retain original Markdown', async () => {
  const tx = transport((call, index) => index === 1 ? Response.json({ ok: false }, { status: 400 }) : null);
  assert.equal(await tx.telegram.result(task, { ok: true, text: '**<value> & answer**' }), true);
  assert.equal(tx.calls[1].body.parse_mode, undefined);
  assert.equal(tx.calls[1].body.text, '**<value> & answer**');
  const markdown = '# Result\n\n' + '```js\n* < & 🚀\n```\n\n'.repeat(400);
  await tx.telegram.result(task, { ok: true, text: markdown });
  assertBalanced(tx.calls[2].body.text);
  assert.match(tx.calls[2].body.text, /full answer attached/);
  assert.equal(await tx.calls[3].body.get('document').text(), markdown);
  assert.equal(tx.calls[3].body.get('document').name, 'answer.md');
});

test('near-limit answers retain the full answer and deliver a large edit footer separately', async () => {
  const tx = transport();
  const text = 'x'.repeat(3450);
  await tx.telegram.result({ ...task, kind: 'do', branch: 'ccbot/' + 'b'.repeat(240) }, { text, tool_calls: 23 });
  assert.deepEqual(tx.calls.map(c => c.method), ['sendMessage', 'sendMessage']);
  assert.equal(tx.calls[0].body.text, text);
  assert.match(tx.calls[1].body.text, /23 tool calls/);
});

test('huge publishing errors remain separate and are attached without truncation', async () => {
  const tx = transport(), error = '<error> & details\n'.repeat(1000);
  await tx.telegram.result(task, { text: 'Agent summary.', error });
  assert.equal(tx.calls[0].body.text, 'Agent summary.');
  assert.match(tx.calls[1].body.text, /^⚠️/); assertBalanced(tx.calls[1].body.text);
  assert.equal(tx.calls[2].body.get('document').name, 'error.md');
  assert.equal(await tx.calls[2].body.get('document').text(), error);
});

test('migration preserves existing tasks and adds the schema status-message column', () => {
  const sqlite = new DatabaseSync(':memory:');
  try {
    const schema = readFileSync(new URL('../schema.sql', import.meta.url), 'utf8');
    sqlite.exec(schema.replace('  status_message_id INTEGER,\n', '').replace('  status_message_id INTEGER,\r\n', ''));
    sqlite.exec("INSERT INTO tasks(id,kind,repo,name,prompt,base,epoch,edit_mode,timeout_minutes,metered,status,created_at) VALUES ('t_old','ask','o/r','r','p','main',0,'edits',15,1,'queued',1)");
    sqlite.exec(readFileSync(new URL('../migrations/0002_status_message.sql', import.meta.url), 'utf8'));
    assert.equal(sqlite.prepare("SELECT status_message_id FROM tasks WHERE id='t_old'").get().status_message_id, null);
    sqlite.exec("UPDATE tasks SET status_message_id=123 WHERE id='t_old'");
    assert.equal(sqlite.prepare("SELECT status_message_id FROM tasks WHERE id='t_old'").get().status_message_id, 123);
  } finally { sqlite.close(); }
});

test('agent caches npm downloads before installing the latest Command Code', () => {
  const workflow = readFileSync(new URL('../../.github/workflows/task.yml', import.meta.url), 'utf8');
  const agent = workflow.split('\n  publish:')[0];
  assert.match(agent, /uses: actions\/cache@v4/);
  assert.match(agent, /path: ~\/\.npm/);
  assert.ok(agent.includes("key: ccbot-npm-${{ runner.os }}-${{ hashFiles('runner/.github/workflows/task.yml') }}"));
  assert.ok(agent.indexOf('uses: actions/cache@v4') < agent.indexOf('npm install -g --no-fund --no-audit command-code@latest'));
});
