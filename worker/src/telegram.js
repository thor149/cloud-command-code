export function resultMessage(task, result) {
  const header = task.status === 'cancelled' ? '🛑 Cancelled' : task.status === 'timeout' || result.timed_out ? '⏱ Timed out'
    : result.ok ? '✅ Done' : `❌ Failed (exit ${result.exit_code ?? '?'})`;
  const output = result.text || result.error || '(No output)';
  const error = result.text && result.error && result.error !== result.text ? `\n\nError: ${result.error}` : '';
  return `${header} — ${task.minutes} min used\n\n${output}${error}${result.pr_url ? `\n\nPR: ${result.pr_url} — paste it to Claude for review and merge.` : ''}`;
}

export function createTelegram(token, owner, fetcher = fetch) {
  async function call(method, body, multipart = false) {
    try {
      const response = await fetcher(`https://api.telegram.org/bot${token}/${method}`, {
        method: 'POST', ...(multipart ? {} : { headers: { 'Content-Type': 'application/json' } }),
        body: multipart ? body : JSON.stringify(body)
      });
      const result = await response.json();
      if (!response.ok || !result.ok) console.error(JSON.stringify({ event: 'telegram_error', method, status: response.status }));
      return Boolean(response.ok && result.ok);
    } catch {
      // Never log the URL or exception: both may contain the bot token.
      console.error(JSON.stringify({ event: 'telegram_network_error', method }));
      return false;
    }
  }
  const send = text => call('sendMessage', { chat_id: owner, text });
  return {
    send,
    async result(task, result) {
      const text = resultMessage(task, result);
      if (text.length <= 3500) return send(text);
      const data = new FormData();
      data.set('chat_id', String(owner));
      data.set('caption', text.split('\n')[0]);
      data.set('document', new Blob([text], { type: 'text/markdown;charset=utf-8' }), `${task.id}.md`);
      return call('sendDocument', data, true);
    }
  };
}
