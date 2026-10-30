export function createGithub(token, fetcher = fetch) {
  async function post(path, body) {
    try {
      const response = await fetcher(`https://api.github.com${path}`, {
        method: 'POST', headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json',
          'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'cloud-command-code', 'Content-Type': 'application/json' },
        ...(body === undefined ? {} : { body: JSON.stringify(body) })
      });
      if (response.ok) return { ok: true, status: response.status };
      let message = 'GitHub rejected the request';
      try { message = (await response.json()).message || message; } catch { /* non-JSON gateway error */ }
      return { ok: false, status: response.status, message: String(message).slice(0, 1000) };
    } catch { return { ok: false, status: 0, message: 'GitHub network error; dispatch outcome is unknown. Check Actions before retrying.' }; }
  }
  return {
    dispatch: task => post(`/repos/${task.repo}/actions/workflows/${encodeURIComponent(task.workflow)}/dispatches`, { ref: task.base, inputs: { task_id: task.id } }),
    cancel: task => post(`/repos/${task.repo}/actions/runs/${task.run_id}/cancel`)
  };
}
