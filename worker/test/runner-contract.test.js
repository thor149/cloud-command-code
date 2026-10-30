import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fixture } from './helpers.js';
import { handleFetch } from '../src/index.js';

// Python's test_runner_contract.py checks these exact signed bytes against the actual runner.
const cases = JSON.parse(readFileSync(new URL('./fixtures/runner-contract.json', import.meta.url), 'utf8')
  .replaceAll('__CONTRACT_TEXT__', '🚀'.repeat(60000)));
for (const example of cases) {
  test(`Python runner signed requests interoperate with Worker: ${example.mode}`, async t => {
    const f = fixture(t);
    await f.webhook(example.mode === 'empty' ? '/do Check whether changes are needed' : '/ask Explain the code');
    const queued = f.dispatches[0];
    await f.db.prepare('UPDATE tasks SET id=?, branch=? WHERE id=?').bind(example.task.id, example.task.branch, queued.id).run();
    for (const { url, method, headers, body } of example.requests) {
      const response = await handleFetch(new Request(url, { method, headers,
        ...(method === 'GET' ? {} : { body }) }), f.deps);
      assert.equal(response.status, method === 'GET' ? 200 : 204);
      if (method === 'GET') assert.deepEqual(await response.json(), example.task);
    }
    const saved = await f.store.task(example.task.id);
    assert.equal(saved.status, example.mode === 'missing' ? 'failed' : 'done');
    const result = f.results[0].result;
    if (example.mode === 'missing') {
      assert.equal(result.exit_code, null);
      assert.match(result.error, /result.json is missing/);
    } else if (example.mode === 'success') {
      assert.equal(Array.from(result.text).length, 60000);
      await f.webhook('/plan Continue the session');
      const resumed = await (await f.api('GET', `/api/tasks/${f.dispatches[1].id}`)).json();
      assert.equal(resumed.session_id, 'session-1');
      assert.equal(resumed.session, result.session);
    } else {
      assert.equal(result.session, null);
      assert.equal(result.branch, null);
      assert.equal(result.commits, 0);
    }
  });
}
