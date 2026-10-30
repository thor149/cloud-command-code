import { formatUsage, formatTime, parseLimit, usageReport, WINDOWS } from './limits.js';
import { conversationKind } from './store.js';
import { escapeHtml, linkHtml } from './format.js';
import { notifyResult, updateTaskStatus } from './telegram.js';

const HELP = `💬 <b>Ask &amp; plan</b>\n/ask &lt;question&gt; — or send plain text\n/plan &lt;goal&gt;\n/new — fresh ask/plan conversation\n\n🛠 <b>Edit</b>\n/do &lt;task&gt; — edit on a task branch\n/done — end edit conversation\n\n⚙️ <b>Control</b>\n/repos — list repositories\n/use &lt;name&gt; — select repository\n/status — active task and settings\n/cancel — cancel active task\n/model [id|default]\n/effort [low|medium|high|xhigh|max|default]\n/help — show commands\n\n📊 <b>Limits</b>\n/usage — usage and projections\n/limits [hourly|daily|monthly &lt;minutes|off&gt;]`;
const EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'];
export const PAUSED_MESSAGE = 'AI tasks are paused to prevent model/API spending. Your repositories, branches and saved conversations are retained. /status, /repos and other controls still work. An operator must verify hosting and model costs before enabling task execution.';

export function parseCommand(text) {
  const match = /^\/([a-z]+)(?:@[a-z0-9_]+)?(?:\s+([\s\S]*))?$/i.exec(text.trim());
  return match ? { command: match[1].toLowerCase(), args: (match[2] ?? '').trim() } : { command: 'ask', args: text.trim() };
}

export async function effectiveLimits(store, defaults) {
  const pairs = await Promise.all(WINDOWS.map(async window => [window, await store.getSetting(`limit:${window}`, defaults[window] ?? null)]));
  return Object.fromEntries(pairs);
}

export async function getUsage(deps) {
  const [history, limits] = await Promise.all([deps.store.usage(deps.now()), effectiveLimits(deps.store, deps.config.limits)]);
  return usageReport(history.tasks, limits, deps.now(), history.firstTaskAt);
}

export function branchName(name, prompt, now, id) {
  const stamp = new Date(now * 1000).toISOString().slice(0, 16).replace(/[-:T]/g, '');
  const slug = prompt.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 36).replace(/-$/, '') || 'task';
  return `ccbot/${name}-${stamp.slice(0, 8)}-${stamp.slice(8)}-${slug}-${id.slice(-8)}`;
}

export async function handleCommand(message, deps) {
  const { store, telegram, github, config, now } = deps;
  const { command, args } = parseCommand(message.text);
  const names = Object.keys(config.repos);
  const storedName = await store.getSetting('active_repo', names[0]);
  const name = names.includes(storedName) ? storedName : names[0];
  const repo = config.repos[name];
  const reply = text => telegram.send(text);
  const replyHtml = html => telegram.sendHtml(html);
  if (['start', 'help'].includes(command)) return replyHtml(`${config.execution === 'enabled' ? '' : '⏸ <b>AI tasks paused</b> · no new model work will start\n\n'}${HELP}`);
  if (command === 'repos') return replyHtml(names.map(n => `📁 <b>${escapeHtml(n)}</b>${n === name ? ' · active' : ''}\n<code>${escapeHtml(config.repos[n].repo)}</code>\n<b>Access</b> ${config.repos[n].writable ? 'writable' : 'read-only'}\n<b>Usage</b> ${config.repos[n].metered ? 'metered' : 'unmetered'}`).join('\n\n'));
  if (command === 'use') {
    if (!Object.hasOwn(config.repos, args)) return reply(`Unknown repository. Use /repos. Active: ${name}`);
    await store.setSetting('active_repo', args);
    return replyHtml(`📁 <b>Active repository</b> <code>${escapeHtml(args)}</code>`);
  }
  if (command === 'model' || command === 'effort') {
    const fallback = command === 'model' ? config.model : config.effort;
    if (!args) return replyHtml(`⚙️ <b>${command}</b> <code>${escapeHtml(await store.getSetting(command, fallback) || 'default')}</code>`);
    if (command === 'effort' && args !== 'default' && !EFFORTS.includes(args)) return reply(`Use /effort ${EFFORTS.join('|')}|default`);
    if (command === 'model' && (args.length > 200 || /\s|[\x00-\x1f]/.test(args))) return reply('Use /model <model-id> or /model default');
    await store.setSetting(command, args === 'default' ? null : args);
    return replyHtml(`⚙️ <b>${command}</b> <code>${escapeHtml(args)}</code>`);
  }
  if (command === 'limits') {
    if (args) {
      const parsed = parseLimit(args);
      if (!parsed) return reply('Use /limits hourly|daily|monthly <positive minutes|off>');
      await store.setSetting(`limit:${parsed.window}`, parsed.value);
    }
    const limits = await effectiveLimits(store, config.limits);
    return replyHtml(WINDOWS.map(w => `📊 <b>${w}</b> ${limits[w] ?? 'off'}${limits[w] === null ? '' : ' min'}`).join('\n'));
  }
  if (command === 'usage') return replyHtml(formatUsage(await getUsage(deps), now()));
  if (command === 'new' || command === 'done') {
    await store.resetConversation(repo.repo, command === 'new' ? 'ask' : 'do', now());
    return reply(command === 'new' ? `Fresh ask/plan conversation for ${name}.` : `Edit conversation ended for ${name}. Branch and PR stay on GitHub.`);
  }
  if (command === 'status') {
    const [task, model, effort, usage] = await Promise.all([store.active(repo.repo), store.getSetting('model', config.model), store.getSetting('effort', config.effort), getUsage(deps)]);
    const elapsed = task ? Math.max(0, Math.floor((now() - (task.started_at ?? task.created_at)) / 60)) : 0;
    const taskLine = task ? `${task.status === 'queued' ? '⏳' : '▶️'} <b>Task</b> ${task.status} · ${elapsed} min elapsed\n<b>ID</b> <code>${escapeHtml(task.id)}</code>${task.run_url ? `\n${linkHtml('view run', task.run_url)}` : ''}` : '⏳ <b>Task</b> No active task';
    return replyHtml(`📁 <b>Repository</b> <code>${escapeHtml(name)}</code>\n<b>Source</b> <code>${escapeHtml(repo.repo)}</code>\n${taskLine}\n<b>AI execution</b> ${config.execution === 'enabled' ? 'enabled (provider charges may apply)' : 'paused (no new model work)'}\n⚙️ <b>Model</b> <code>${escapeHtml(model || 'default')}</code>\n⚙️ <b>Effort</b> <code>${escapeHtml(effort || 'default')}</code>\n${WINDOWS.map(w => `📊 <b>${w}</b> ${usage.windows[w].used}/${usage.windows[w].limit ?? 'off'} min`).join('\n')}`);
  }
  if (command === 'cancel') {
    const task = await store.active(repo.repo);
    if (!task) return reply('No active task to cancel.');
    if (task.status === 'finishing') return reply('The result is being saved. Try /status shortly.');
    const result = { ok: false, text: 'Task cancelled.', exit_code: null };
    let ended = await store.endTask(task, 'cancelled', result, now());
    // A queued task may have started while we were reading it. Cancel the new running state.
    if (!ended) {
      const latest = await store.active(repo.repo);
      if (latest?.id === task.id && latest.status !== 'finishing') ended = await store.endTask(latest, 'cancelled', result, now());
    }
    if (!ended) return reply('Task already finished.');
    if (ended.run_id) {
      const cancelled = await github.cancel(ended);
      if (!cancelled.ok) await reply(`GitHub cancellation failed (${cancelled.status}): ${cancelled.message}. Worker task is cancelled; late results will be ignored.`);
    }
    return notifyResult(deps, ended, result);
  }
  if (!['ask', 'plan', 'do'].includes(command)) return reply('Unknown command. Use /help.');
  if (config.execution !== 'enabled') return reply(PAUSED_MESSAGE);
  if (!args || args.length > 4000) return reply('Enter a prompt between 1 and 4000 characters.');
  if (command === 'do' && !repo.writable) return reply(`${name} is read-only; /do is unavailable.`);
  if (await store.active(repo.repo)) return reply('A task is already running');
  if (repo.metered) {
    const report = await getUsage(deps);
    const blocked = WINDOWS.find(w => report.windows[w].blocked);
    if (blocked) return replyHtml(`📊 <b>${blocked} limit reached</b> ${report.windows[blocked].used}/${report.windows[blocked].limit} min\n<b>Frees up</b> ${formatTime(report.windows[blocked].freesAt, now())}\nUse /usage.`);
  }
  const conv = await store.conversation(repo.repo, conversationKind(command), now());
  const id = 't_' + crypto.randomUUID().replace(/-/g, '').slice(0, 16);
  const task = { id, kind: command, repo: repo.repo, name, prompt: args, base: repo.base, workflow: repo.workflow,
    branch: command === 'do' ? conv.branch || branchName(name, args, now(), id) : null,
    continue_branch: command === 'do' && Boolean(conv.branch_exists), epoch: conv.epoch,
    model: await store.getSetting('model', config.model), effort: await store.getSetting('effort', config.effort),
    edit_mode: config.editMode, timeout_minutes: command === 'do' ? 45 : 15,
    metered: repo.metered, status: 'queued', created_at: now() };
  if (!await store.insertTask(task)) return reply('A task is already running or the conversation changed. Try /status and retry.');
  // Save the status ID before dispatch: a fast /started callback must see it.
  await updateTaskStatus(deps, task);
  const dispatch = await github.dispatch(task);
  if (!dispatch.ok) {
    const text = `GitHub dispatch failed (${dispatch.status}): ${dispatch.message}`;
    const result = { ok: false, exit_code: null, text };
    const ended = await store.endTask(task, 'failed', result, now());
    if (ended) return notifyResult(deps, ended, result);
    return;
  }
  return;
}
