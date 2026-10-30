CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('ask', 'plan', 'do')),
  repo TEXT NOT NULL,
  name TEXT NOT NULL,
  prompt TEXT NOT NULL,
  base TEXT NOT NULL,
  branch TEXT,
  continue_branch INTEGER NOT NULL DEFAULT 0,
  epoch INTEGER NOT NULL,
  model TEXT,
  effort TEXT,
  edit_mode TEXT NOT NULL,
  timeout_minutes INTEGER NOT NULL,
  metered INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued','running','finishing','done','failed','cancelled','timeout')),
  created_at INTEGER NOT NULL,
  started_at INTEGER,
  finished_at INTEGER,
  minutes INTEGER NOT NULL DEFAULT 0,
  run_id INTEGER,
  run_url TEXT,
  status_message_id INTEGER,
  result TEXT,
  claim TEXT,
  claimed_at INTEGER
);
-- Enforce the queue rule in the database, including concurrent webhook requests.
CREATE UNIQUE INDEX IF NOT EXISTS tasks_one_active_repo ON tasks(repo)
  WHERE status IN ('queued','running','finishing');
CREATE INDEX IF NOT EXISTS tasks_usage ON tasks(metered, finished_at);
CREATE INDEX IF NOT EXISTS tasks_timeouts ON tasks(status, created_at, started_at);
CREATE TABLE IF NOT EXISTS updates (update_id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS updates_created ON updates(created_at);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversations (
  repo TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('ask','do')),
  epoch INTEGER NOT NULL DEFAULT 0,
  session_id TEXT,
  session_task_id TEXT,
  branch TEXT,
  branch_exists INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (repo, kind)
);
