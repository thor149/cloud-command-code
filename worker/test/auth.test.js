import test from 'node:test';
import assert from 'node:assert/strict';
import { sign, verify } from '../src/auth.js';
import { fixture } from './helpers.js';

test('HMAC exactly matches both Python contract vectors', async () => {
  assert.equal(await sign('test-secret', 'GET', '/api/tasks/t_abc', '1700000000', ''), 'sha256=3c19f8fea134ad6df9b8af38d1d6a080df846afb96c7facb173b31b2c4cf5675');
  assert.equal(await sign('test-secret', 'POST', '/api/tasks/t_abc/result', '1700000000', '{"ok":true}'), 'sha256=dbe4df59d51c3478bec531a4f29d6da1f6b10875582c6f036f96df1599d979e1');
});

test('auth rejects bad, stale, future, missing and malformed signatures; includes exact bytes and excludes query', async () => {
  const stamp = '1700000000';
  const signature = await sign('test-secret', 'POST', '/api/tasks/t_abc/result', stamp, 'é');
  const req = (signatureValue = signature, timestamp = stamp) => new Request('https://worker/api/tasks/t_abc/result?ignored=1', { method: 'POST', headers: { 'X-Ccbot-Timestamp': timestamp, 'X-Ccbot-Signature': signatureValue } });
  assert.equal(await verify(req(), 'test-secret', new TextEncoder().encode('é'), 1700000300), true);
  for (const time of [1700000301, 1699999699]) assert.equal(await verify(req(), 'test-secret', new TextEncoder().encode('é'), time), false);
  assert.equal(await verify(req(), 'test-secret', new TextEncoder().encode('e'), 1700000000), false);
  assert.equal(await verify(req(signature.slice(0, -1) + '0'), 'test-secret', new TextEncoder().encode('é'), 1700000000), false);
  for (const timestamp of ['NaN', '', '1e9', '-1']) assert.equal(await verify(req(signature, timestamp), 'test-secret', new Uint8Array(), 1700000000), false);
  assert.equal(await verify(new Request('https://worker/api/x'), 'test-secret', new Uint8Array(), 1700000000), false);
});

test('API verifies auth even for unknown endpoints and tasks', async t => {
  const f = fixture(t);
  assert.equal((await f.api('GET', '/api/unknown', undefined, { 'X-Ccbot-Signature': 'bad' })).status, 401);
  assert.equal((await f.api('GET', '/api/unknown')).status, 404);
  assert.equal((await f.api('GET', '/api/tasks/t_missing')).status, 404);
});
