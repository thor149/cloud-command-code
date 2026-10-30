export const conversationKind = kind => kind === 'do' ? 'do' : 'ask';
export const sessionKey = task => `session:${task.name}:${conversationKind(task.kind)}:${task.epoch}`;
export const isActive = task => task && ['queued', 'running', 'finishing'].includes(task.status);
export const minutesUsed = (task, now) => task.started_at == null ? 1 : Math.max(1, Math.ceil((now - task.started_at) / 60));

export function createStore(db, kv) {
  const query = (sql, ...args) => db.prepare(sql).bind(...args);
  return {
    async dedupe(id, now) {
      const result = await query('INSERT OR IGNORE INTO updates(update_id,created_at) VALUES (?,?)', id, now).run();
      return result.meta.changes === 1;
    },
    async getSetting(key, fallback = null) {
      const row = await query('SELECT value FROM settings WHERE key=?', key).first();
      return row ? JSON.parse(row.value) : fallback;
    },
    async setSetting(key, value) {
      await query('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', key, JSON.stringify(value)).run();
    },
    async conversation(repo, kind, now) {
      await query('INSERT OR IGNORE INTO conversations(repo,kind,updated_at) VALUES (?,?,?)', repo, kind, now).run();
      return query('SELECT * FROM conversations WHERE repo=? AND kind=?', repo, kind).first();
    },
    getConversation(repo, kind) { return query('SELECT * FROM conversations WHERE repo=? AND kind=?', repo, kind).first(); },
    async resetConversation(repo, kind, now) {
      await query(`INSERT INTO conversations(repo,kind,epoch,updated_at) VALUES (?,?,1,?)
        ON CONFLICT(repo,kind) DO UPDATE SET epoch=epoch+1, session_id=NULL, session_task_id=NULL,
        branch=NULL, branch_exists=0, updated_at=excluded.updated_at`, repo, kind, now).run();
    },
    async active(repo) {
      return query("SELECT * FROM tasks WHERE repo=? AND status IN ('queued','running','finishing')", repo).first();
    },
    async insertTask(task) {
      const fields = ['id','kind','repo','name','prompt','base','branch','continue_branch','epoch','model','effort','edit_mode','timeout_minutes','metered','status','created_at'];
      // Epoch is checked in the INSERT so /new or /done cannot race task creation.
      const result = await query(`INSERT OR IGNORE INTO tasks(${fields.join(',')}) SELECT ${fields.map(() => '?').join(',')}
        WHERE EXISTS (SELECT 1 FROM conversations WHERE repo=? AND kind=? AND epoch=?)`,
      ...fields.map(key => typeof task[key] === 'boolean' ? Number(task[key]) : task[key] ?? null),
      task.repo, conversationKind(task.kind), task.epoch).run();
      if (result.meta.changes !== 1) return false;
      if (task.kind === 'do') await query(`UPDATE conversations SET branch=COALESCE(branch,?), updated_at=?
        WHERE repo=? AND kind='do' AND epoch=?`, task.branch, task.created_at, task.repo, task.epoch).run();
      return true;
    },
    task(id) { return query('SELECT * FROM tasks WHERE id=?', id).first(); },
    async startTask(id, run, now) {
      return query(`UPDATE tasks SET status='running', started_at=?, run_id=?, run_url=?
        WHERE id=? AND status='queued' RETURNING *`, now, run.run_id, run.run_url, id).first();
    },
    async claimResult(id, claim, now) {
      return query(`UPDATE tasks SET status='finishing', claim=?, claimed_at=? WHERE id=?
        AND status IN ('queued','running') RETURNING *`, claim, now, id).first();
    },
    async releaseResult(task) {
      await query(`UPDATE tasks SET status=CASE WHEN started_at IS NULL THEN 'queued' ELSE 'running' END,
        claim=NULL, claimed_at=NULL WHERE id=? AND status='finishing' AND claim=?`, task.id, task.claim).run();
    },
    async finishResult(task, result, status, now, hasSession) {
      const kind = conversationKind(task.kind);
      const statements = [];
      // Conversation and terminal state commit atomically. Only the winning claim may publish.
      if (hasSession || (kind === 'do' && result.branch === task.branch)) {
        statements.push(query(`UPDATE conversations SET
          session_id=CASE WHEN ? THEN ? ELSE session_id END,
          session_task_id=CASE WHEN ? THEN ? ELSE session_task_id END,
          branch_exists=CASE WHEN ? THEN 1 ELSE branch_exists END, updated_at=?
          WHERE repo=? AND kind=? AND epoch=? AND EXISTS
          (SELECT 1 FROM tasks WHERE id=? AND status='finishing' AND claim=?)`,
        Number(hasSession), result.session_id ?? null, Number(hasSession), task.id,
        Number(kind === 'do' && result.branch === task.branch && (result.commits > 0 || Boolean(result.pr_url))),
        now, task.repo, kind, task.epoch, task.id, task.claim));
      }
      const stored = { ...result }; delete stored.session;
      statements.push(query(`UPDATE tasks SET status=?, finished_at=?, minutes=?, result=?, claim=NULL, claimed_at=NULL
        WHERE id=? AND status='finishing' AND claim=? RETURNING *`,
      status, now, minutesUsed(task, now), JSON.stringify(stored), task.id, task.claim));
      const results = await db.batch(statements);
      return results.at(-1).results[0] ?? null;
    },
    async endTask(task, status, result, now) {
      return query(`UPDATE tasks SET status=?, finished_at=?, minutes=?, result=?, claim=NULL, claimed_at=NULL
        WHERE id=? AND status=? AND status IN ('queued','running') RETURNING *`,
      status, now, minutesUsed(task, now), JSON.stringify(result), task.id, task.status).first();
    },
    async usage(now) {
      // Only the last week/current month is needed; old history contributes its first-task date.
      const since = Math.min(now - 7 * 86400, Date.UTC(new Date(now * 1000).getUTCFullYear(), new Date(now * 1000).getUTCMonth(), 1) / 1000);
      const rows = await query('SELECT minutes,finished_at,created_at,metered FROM tasks WHERE metered=1 AND finished_at>=?', since).all();
      const first = await query('SELECT MIN(created_at) AS first_at FROM tasks WHERE metered=1').first();
      return { tasks: rows.results, firstTaskAt: first?.first_at ?? null };
    },
    async expired(now) {
      const result = await query(`SELECT * FROM tasks WHERE (status='queued' AND created_at<?)
        OR (status='running' AND started_at<?-(timeout_minutes+15)*60)
        OR (status='finishing' AND claimed_at<?)`, now - 1200, now, now - 300).all();
      return result.results;
    },
    async recoverResult(task) {
      // A terminated isolate must not leave a permanent per-repo lock.
      await query(`UPDATE tasks SET status=CASE WHEN started_at IS NULL THEN 'queued' ELSE 'running' END,
        claim=NULL, claimed_at=NULL WHERE id=? AND status='finishing' AND claim=?`, task.id, task.claim).run();
    },
    async putSession(task, bytes) {
      await kv.put(sessionKey(task), bytes, { metadata: { task_id: task.id, session_id: task.result_session_id } });
    },
    async getSession(task, conversation) {
      if (conversation.epoch !== task.epoch || !conversation.session_id || !conversation.session_task_id) return null;
      const entry = await kv.getWithMetadata(sessionKey(task), 'arrayBuffer');
      // KV is eventually consistent. Refuse an older blob rather than resuming the wrong session.
      if (!entry.value || entry.metadata?.task_id !== conversation.session_task_id || entry.metadata?.session_id !== conversation.session_id) {
        throw new Error('SESSION_NOT_READY');
      }
      return entry.value;
    }
  };
}
