import { verify } from './auth.js';
import { conversationKind, isActive } from './store.js';
import { notifyResult, updateTaskStatus } from './telegram.js';

export const MAX_SESSION_BYTES = 20 * 1024 * 1024;
// Two 60,000-code-point strings can each use 12 JSON bytes per escaped surrogate pair.
const MAX_BODY_BYTES = Math.ceil(MAX_SESSION_BYTES / 3) * 4 + 1500000;
const json = (body, status = 200) => Response.json(body, { status });
const noContent = () => new Response(null, { status: 204 });

export function encodeBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  if (typeof bytes.toBase64 === 'function') return bytes.toBase64();
  // Avoid both a 20 MB spread operation and quadratic character concatenation.
  const chunks = [];
  for (let i = 0; i < bytes.length; i += 32768) chunks.push(String.fromCharCode(...bytes.subarray(i, i + 32768)));
  return btoa(chunks.join(''));
}

export function decodeSession(value) {
  if (typeof value !== 'string' || value.length > Math.ceil(MAX_SESSION_BYTES / 3) * 4 || value.length % 4 !== 0
      || !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) throw new Error('Invalid session base64');
  const decodedLength = value.length / 4 * 3 - (value.endsWith('==') ? 2 : value.endsWith('=') ? 1 : 0);
  if (decodedLength > MAX_SESSION_BYTES) throw new Error('Session exceeds 20 MB');
  if (typeof Uint8Array.fromBase64 === 'function') return Uint8Array.fromBase64(value);
  const binary = atob(value);
  if (binary.length > MAX_SESSION_BYTES) throw new Error('Session exceeds 20 MB');
  // A direct copy avoids Uint8Array.from(string)'s potentially huge intermediate iterable.
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

export async function readBody(request, maxBytes) {
  if (Number(request.headers.get('content-length')) > maxBytes) throw new Error('BODY_TOO_LARGE');
  if (!request.body) return new Uint8Array();
  const reader = request.body.getReader();
  const chunks = []; let length = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    length += value.length;
    if (length > maxBytes) { await reader.cancel(); throw new Error('BODY_TOO_LARGE'); }
    chunks.push(value);
  }
  const bytes = new Uint8Array(length); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  return bytes;
}

function validResult(result) {
  return result && typeof result === 'object' && !Array.isArray(result)
    && typeof result.ok === 'boolean' && (result.exit_code === null || Number.isInteger(result.exit_code)) && typeof result.timed_out === 'boolean'
    && typeof result.text === 'string' && Array.from(result.text).length <= 60000
    && (result.session_id == null || typeof result.session_id === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(result.session_id))
    && (result.session == null || typeof result.session === 'string')
    && (result.session == null || Boolean(result.session_id))
    && Number.isSafeInteger(result.tool_calls) && result.tool_calls >= 0
    && Number.isSafeInteger(result.commits) && result.commits >= 0
    && (result.branch == null || typeof result.branch === 'string' && result.branch.length <= 255)
    && (result.pr_url == null || typeof result.pr_url === 'string' && /^https:\/\/github\.com\/[^/]+\/[^/]+\/pull\/\d+$/.test(result.pr_url))
    && (result.error == null || typeof result.error === 'string' && Array.from(result.error).length <= 60000);
}

export async function handleApi(request, deps) {
  const { store, now, env } = deps;
  let bytes;
  try { bytes = await readBody(request, MAX_BODY_BYTES); }
  catch { return json({ error: 'Request body too large' }, 413); }
  if (!await verify(request, env.CCBOT_SECRET, bytes, now())) return json({ error: 'Unauthorized' }, 401);
  const match = /^\/api\/tasks\/(t_[a-zA-Z0-9_]+)(?:\/(started|result))?$/.exec(new URL(request.url).pathname);
  if (!match) return json({ error: 'Not found' }, 404);
  const task = await store.task(match[1]);
  if (!task) return json({ error: 'Unknown task' }, 404);
  const action = match[2];
  if (!action && request.method === 'GET') {
    if (task.status === 'cancelled') return json({ error: 'Cancelled' }, 410);
    const conv = await store.getConversation(task.repo, conversationKind(task.kind));
    let session;
    try { session = await store.getSession(task, conv); }
    catch { return json({ error: 'Session not yet available; retry shortly' }, 503); }
    return json({ id: task.id, kind: task.kind, repo: task.repo, name: task.name, prompt: task.prompt, base: task.base,
      branch: task.branch, continue_branch: Boolean(task.continue_branch), session_id: session ? conv.session_id : null,
      session: session ? encodeBase64(session) : null, model: task.model, effort: task.effort,
      edit_mode: task.edit_mode, timeout_minutes: task.timeout_minutes });
  }
  if (!action || request.method !== 'POST') return json({ error: 'Method not allowed' }, 405);
  // Ignore duplicate terminal results even if a retry's JSON is damaged.
  if (!isActive(task)) return noContent();
  if (action === 'result' && task.status === 'finishing') return json({ error: 'Result is being saved; retry' }, 503);
  let body;
  try { body = JSON.parse(new TextDecoder().decode(bytes)); } catch { return json({ error: 'Invalid JSON' }, 400); }
  if (action === 'started') {
    if (!body || !Number.isSafeInteger(body.run_id) || body.run_id <= 0 || typeof body.run_url !== 'string'
      || body.run_url !== `https://github.com/${task.repo}/actions/runs/${body.run_id}`) return json({ error: 'Invalid run' }, 400);
    const started = await store.startTask(task.id, body, now());
    if (started) await updateTaskStatus(deps, started);
    return noContent();
  }
  if (!validResult(body) || body.branch != null && body.branch !== task.branch
    || body.pr_url && !body.pr_url.startsWith(`https://github.com/${task.repo}/pull/`)) return json({ error: 'Invalid result' }, 400);
  let session = null;
  try { if (body.session != null) session = decodeSession(body.session); }
  catch (error) { return json({ error: error.message }, 400); }
  const claimed = await store.claimResult(task.id, crypto.randomUUID(), now());
  if (!claimed) return (await store.task(task.id))?.status === 'finishing' ? json({ error: 'Result is being saved; retry' }, 503) : noContent();
  let finished;
  try {
    if (session) await store.putSession({ ...claimed, result_session_id: body.session_id }, session);
    finished = await store.finishResult(claimed, body, body.timed_out ? 'timeout' : body.ok ? 'done' : 'failed', now(), session !== null);
  } catch {
    await store.releaseResult(claimed);
    return json({ error: 'Result storage failed; retry' }, 503);
  }
  if (finished) await notifyResult(deps, finished, body);
  return noContent();
}
