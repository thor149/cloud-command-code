import { answerLayout, escapeHtml, htmlToText, linkHtml, markdownToHtml } from './format.js';

export function statusMessage(task, result = {}) {
  const run = task.run_url ? ` · ${linkHtml('view run', task.run_url)}` : '';
  const context = ` · /${escapeHtml(task.kind)} · <code>${escapeHtml(task.name)}</code>`;
  if (task.status === 'queued') return `⏳ <b>Queued</b>${context}\n<i>${escapeHtml(Array.from(task.prompt || '').slice(0, 200).join('').replace(/\s+/g, ' '))}</i>`;
  if (task.status === 'running' || task.status === 'finishing') return `▶️ <b>Running</b>${context}${run}`;
  if (task.status === 'cancelled') return `🛑 <b>Cancelled</b>${run}`;
  if (task.status === 'timeout' || result.timed_out) return `⏱ <b>Timed out</b>${run}`;
  if (result.ok) return `✅ <b>Done</b> · ${task.minutes} min${run}`;
  const reason = (result.error || result.text || '').split('\n')[0];
  return `❌ <b>Failed</b> (exit ${escapeHtml(result.exit_code ?? '?')}${reason ? `: ${escapeHtml(Array.from(reason).slice(0, 200).join(''))}` : ''})${run}`;
}

export function resultFooter(task, result) {
  if (task.kind !== 'do') return '';
  const lines = [];
  if (result.branch || task.branch) lines.push(`🌿 <b>Branch</b> <code>${escapeHtml(result.branch || task.branch)}</code>`);
  if (result.pr_url) lines.push(`🔀 <b>Pull request</b> ${linkHtml('#' + result.pr_url.split('/').at(-1), result.pr_url)} — paste it to Claude for review`);
  lines.push(`🛠 <b>${result.tool_calls ?? 0} tool calls</b>`);
  return lines.join('\n');
}

export function resultMessage(task, result, layout = answerLayout(result.text || '(No output)')) {
  const answer = layout.html;
  const footer = resultFooter(task, result);
  return answer + (footer ? `\n\n${footer}` : '');
}

export async function updateTaskStatus(deps, task, result, reconciliations = 0) {
  const sent = await deps.telegram.status(task, result);
  if (sent.ok && Number.isSafeInteger(sent.message_id) && sent.message_id !== task.status_message_id) {
    await deps.store.setStatusMessage(task.id, sent.message_id);
    task.status_message_id = sent.message_id;
  }
  // A slow Telegram request can finish after a concurrent cancel/result edit.
  // Reconcile to the persisted state so it cannot leave "Running" over "Done".
  const latest = await deps.store.task(task.id);
  if (latest && latest.status !== task.status && reconciliations < 3) {
    return updateTaskStatus(deps, latest, latest.result ? JSON.parse(latest.result) : undefined, reconciliations + 1);
  }
  return sent.ok;
}

export async function notifyResult(deps, task, result) {
  await updateTaskStatus(deps, task, result);
  return deps.telegram.result(task, result);
}

export function createTelegram(token, owner, fetcher = fetch) {
  const previews = { is_disabled: true };
  async function call(method, body, multipart = false) {
    try {
      const response = await fetcher(`https://api.telegram.org/bot${token}/${method}`, {
        method: 'POST', ...(multipart ? {} : { headers: { 'Content-Type': 'application/json' } }),
        body: multipart ? body : JSON.stringify(body)
      });
      const result = await response.json();
      const unchanged = method === 'editMessageText' && response.status === 400 && /message is not modified/i.test(result.description || '');
      const ok = Boolean(response.ok && result.ok || unchanged);
      if (!ok) console.error(JSON.stringify({ event: 'telegram_error', method, status: response.status }));
      return { ok, status: result.error_code || response.status, message_id: result.result?.message_id ?? body.message_id };
    } catch {
      // Never log the URL or exception: both may contain the bot token.
      console.error(JSON.stringify({ event: 'telegram_network_error', method }));
      return { ok: false, status: 0 };
    }
  }
  async function formatted(method, html, messageId, plain = htmlToText(html)) {
    const body = { chat_id: owner, text: html, parse_mode: 'HTML', link_preview_options: previews,
      ...(messageId == null ? {} : { message_id: messageId }) };
    const sent = await call(method, body);
    if (!sent.ok && sent.status === 400) {
      // A formatting rejection must not discard the answer. Retry visible text
      // without parse_mode (and retain the same destination for edits).
      delete body.parse_mode; body.text = plain;
      return call(method, body);
    }
    return sent;
  }
  const sendHtml = (html, plain) => formatted('sendMessage', html, undefined, plain);
  async function document(markdown, filename) {
    const data = new FormData();
    data.set('chat_id', String(owner));
    data.set('link_preview_options', JSON.stringify(previews));
    data.set('document', new Blob([markdown], { type: 'text/markdown;charset=utf-8' }), filename);
    return call('sendDocument', data, true);
  }
  return {
    async send(text) { return (await sendHtml(markdownToHtml(text), text)).ok; },
    async sendHtml(html) { return (await sendHtml(html)).ok; },
    async status(task, result) {
      const html = statusMessage(task, result);
      if (task.status_message_id != null) {
        const edited = await formatted('editMessageText', html, task.status_message_id);
        if (edited.ok) return edited;
      }
      return sendHtml(html);
    },
    async result(task, result) {
      const layout = answerLayout(result.text || '(No output)');
      const full = resultMessage(task, result, layout), separateFooter = full.length > 3500;
      const plain = layout.attachment !== null ? undefined : (result.text || '(No output)')
        + (!separateFooter && resultFooter(task, result) ? `\n\n${htmlToText(resultFooter(task, result))}` : '');
      let ok = (await sendHtml(separateFooter ? layout.html : full, plain)).ok;
      if (separateFooter && resultFooter(task, result)) {
        const sent = await sendHtml(resultFooter(task, result)); ok = sent.ok && ok;
      }
      if (layout.attachment !== null) {
        const attached = await document(layout.attachment, 'answer.md');
        ok = attached.ok && ok;
      }
      if (result.error && result.error !== result.text) {
        const warning = answerLayout(`⚠️ ${result.error}`);
        const sent = await sendHtml(warning.html, warning.attachment === null ? `⚠️ ${result.error}` : undefined);
        ok = sent.ok && ok;
        if (warning.attachment !== null) { const attached = await document(result.error, 'error.md'); ok = attached.ok && ok; }
      }
      return ok;
    }
  };
}
