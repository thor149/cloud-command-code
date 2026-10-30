import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import { createStore } from '../src/store.js';
import { configuration, handleFetch } from '../src/index.js';
import { sign } from '../src/auth.js';

// Exercise the actual SQL using SQLite; expose only D1's asynchronous binding interface.
export function sqliteD1() {
  const sqlite = new DatabaseSync(':memory:');
  sqlite.exec(readFileSync(new URL('../schema.sql', import.meta.url), 'utf8'));
  function statement(sql, args = []) {
    return {
      bind(...values) { return statement(sql, values); },
      async first() { return sqlite.prepare(sql).get(...args) ?? null; },
      async run() { const result = sqlite.prepare(sql).run(...args); return { meta: { changes: Number(result.changes) } }; },
      async all() { return { results: sqlite.prepare(sql).all(...args), meta: { changes: sqlite.prepare('SELECT changes() AS n').get().n } }; }
    };
  }
  return { sqlite, prepare: statement, async batch(statements) {
    sqlite.exec('BEGIN');
    try { const results = []; for (const stmt of statements) results.push(await stmt.all()); sqlite.exec('COMMIT'); return results; }
    catch (error) { sqlite.exec('ROLLBACK'); throw error; }
  } };
}

export function fixture(t, overrides = {}) {
  const db = sqliteD1();
  t.after(() => db.sqlite.close());
  const blobs = new Map();
  const kv = {
    async put(key, value, options) { blobs.set(key, { value: new Uint8Array(value).slice().buffer, metadata: options.metadata }); },
    async getWithMetadata(key) { return blobs.get(key) ?? { value: null, metadata: null }; }
  };
  const env = { OWNER_ID: '42', TELEGRAM_WEBHOOK_SECRET: 'hook-secret', CCBOT_SECRET: 'test-secret',
    REPOS: JSON.stringify({ app: { repo: 'owner/app', base: 'main', writable: true, metered: true, workflow: 'ccbot.yml' },
      docs: { repo: 'owner/docs', base: 'trunk', writable: false, metered: false, workflow: 'docs.yml' } }),
    DEFAULT_LIMITS: '{"hourly":60,"daily":180,"monthly":1800}', ...overrides };
  let clock = Date.UTC(2026, 9, 15, 12) / 1000;
  const messages = [], results = [], dispatches = [], cancellations = [];
  const deps = { env, config: configuration(env), store: createStore(db, kv), now: () => clock,
    telegram: { async send(text) { messages.push(text); return true; }, async result(task, result) { results.push({ task, result }); return true; } },
    github: { async dispatch(task) { dispatches.push(task); return { ok: true }; }, async cancel(task) { cancellations.push(task); return { ok: true }; } } };
  let updateId = 0;
  return { ...deps, deps, db, kv, blobs, messages, results, dispatches, cancellations,
    advance(seconds) { clock += seconds; },
    async webhook(text, extra = {}, secret = env.TELEGRAM_WEBHOOK_SECRET) {
      const update = { update_id: ++updateId, message: { from: { id: 42 }, chat: { id: 42, type: 'private' }, text }, ...extra };
      return handleFetch(new Request('https://worker/telegram', { method: 'POST', headers: { 'X-Telegram-Bot-Api-Secret-Token': secret }, body: JSON.stringify(update) }), deps);
    },
    async api(method, path, body, headers = {}) {
      const raw = body === undefined ? '' : typeof body === 'string' ? body : JSON.stringify(body);
      const stamp = String(clock);
      const signature = await sign(env.CCBOT_SECRET, method, new URL(`https://worker${path}`).pathname, stamp, raw);
      return handleFetch(new Request(`https://worker${path}`, { method, headers: { 'X-Ccbot-Timestamp': stamp, 'X-Ccbot-Signature': signature, ...headers }, ...(method === 'GET' ? {} : { body: raw }) }), deps);
    },
    async seedUsage(minutes, finishedAt = clock, metered = true, createdAt = finishedAt - 60) {
      const id = 't_' + crypto.randomUUID().replaceAll('-', '').slice(0, 16);
      await db.prepare(`INSERT INTO tasks(id,kind,repo,name,prompt,base,epoch,edit_mode,timeout_minutes,metered,status,created_at,finished_at,minutes)
        VALUES (?,'ask','owner/old','old','x','main',0,'edits',15,?,'done',?,?,?)`).bind(id, Number(metered), createdAt, finishedAt, minutes).run();
      return id;
    }
  };
}

export const resultBody = (overrides = {}) => ({ ok: true, exit_code: 0, timed_out: false, text: 'Answer', session_id: null,
  session: null, tool_calls: 2, branch: null, commits: 0, pr_url: null, error: null, ...overrides });
