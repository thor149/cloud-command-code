import { constantTimeEqual } from './auth.js';
import { handleApi, readBody } from './api.js';
import { handleCommand } from './commands.js';
import { runCron } from './cron.js';
import { createStore } from './store.js';
import { createGithub } from './github.js';
import { createTelegram } from './telegram.js';

export function configuration(env) {
  const input = JSON.parse(env.REPOS || '{}');
  if (!input || Array.isArray(input) || typeof input !== 'object' || !Object.keys(input).length) throw new Error('REPOS is empty');
  const repos = Object.create(null);
  const targets = new Set();
  for (const [name, repo] of Object.entries(input)) {
    if (!/^[a-z0-9][a-z0-9-]{0,39}$/.test(name) || !repo || typeof repo.repo !== 'string'
      || !/^[a-zA-Z0-9_.-]+\/[a-zA-Z0-9_.-]+$/.test(repo.repo)
      || typeof repo.base !== 'string' || !repo.base || typeof repo.workflow !== 'string' || !repo.workflow
      || targets.has(repo.repo.toLowerCase())) throw new Error('Invalid or duplicate repository');
    targets.add(repo.repo.toLowerCase());
    repos[name] = { ...repo, writable: repo.writable === true, metered: repo.metered !== false };
  }
  const limits = JSON.parse(env.DEFAULT_LIMITS || '{"hourly":60,"daily":180,"monthly":1800}');
  for (const window of ['hourly', 'daily', 'monthly']) {
    if (limits[window] != null && (!Number.isSafeInteger(limits[window]) || limits[window] <= 0)) throw new Error('Invalid limits');
  }
  if (!['edits', 'full'].includes(env.EDIT_MODE || 'edits')) throw new Error('Invalid EDIT_MODE');
  // Opt-in is deliberate: missing/misspelled configuration must never start paid model work.
  const execution = env.TASK_EXECUTION ?? 'paused';
  if (!['paused', 'enabled'].includes(execution)) throw new Error('Invalid TASK_EXECUTION');
  return { repos, limits, model: env.DEFAULT_MODEL || null, effort: env.DEFAULT_EFFORT || null,
    editMode: env.EDIT_MODE || 'edits', execution };
}

export function dependencies(env) {
  return { env, config: configuration(env), store: createStore(env.DB, env.SESSIONS),
    telegram: createTelegram(env.TELEGRAM_BOT_TOKEN, env.OWNER_ID), github: createGithub(env.GITHUB_TOKEN),
    now: () => Math.floor(Date.now() / 1000) };
}

export async function handleFetch(request, deps) {
  const path = new URL(request.url).pathname;
  if (path.startsWith('/api/')) return handleApi(request, deps);
  if (path !== '/telegram') return new Response('Not found', { status: 404 });
  if (request.method !== 'POST') return new Response('Method not allowed', { status: 405 });
  if (!deps.env.TELEGRAM_WEBHOOK_SECRET || !constantTimeEqual(request.headers.get('X-Telegram-Bot-Api-Secret-Token'), deps.env.TELEGRAM_WEBHOOK_SECRET)) return new Response('Unauthorized', { status: 401 });
  let update;
  try { update = JSON.parse(new TextDecoder().decode(await readBody(request, 65536))); }
  catch (error) { return new Response('Invalid update', { status: error.message === 'BODY_TOO_LARGE' ? 413 : 400 }); }
  if (!update || !Number.isSafeInteger(update.update_id)) return new Response('Invalid update', { status: 400 });
  const message = update.message;
  const owner = String(deps.env.OWNER_ID);
  if (owner === '0' || !message || String(message.from?.id) !== owner || message.chat?.type !== 'private'
    || String(message.chat?.id) !== owner || typeof message.text !== 'string') return new Response('OK');
  if (!await deps.store.dedupe(update.update_id, deps.now())) return new Response('OK');
  await handleCommand(message, deps);
  return new Response('OK');
}

export default {
  async fetch(request, env, ctx) {
    try { return await handleFetch(request, dependencies(env)); }
    catch { console.error(JSON.stringify({ event: 'worker_request_failed' })); return Response.json({ error: 'Internal error' }, { status: 500 }); }
  },
  async scheduled(event, env, ctx) {
    // Await completion so scheduled failures are visible; no untracked promises.
    await runCron(dependencies(env));
  }
};
