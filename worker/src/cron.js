export async function runCron(deps) {
  const { store, telegram, github, now } = deps;
  const expired = await store.expired(now());
  for (const candidate of expired) {
    if (candidate.status === 'finishing') {
      await store.recoverResult(candidate);
      // Let a callback retry after a crashed result write before timing it out on a later tick.
      continue;
    }
    const status = candidate.status === 'queued' ? 'failed' : 'timeout';
    const result = { ok: false, timed_out: status === 'timeout', exit_code: null,
      text: status === 'failed' ? 'workflow never started — is ccbot.yml on the base branch and are the repo secrets set?' : 'Workflow exceeded its timeout plus 15 minutes.' };
    const ended = await store.endTask(candidate, status, result, now());
    if (!ended) continue;
    if (ended.run_id) await github.cancel(ended);
    await telegram.result(ended, result);
  }
}
