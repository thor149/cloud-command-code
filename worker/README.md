# Cloud Command Code Worker

Plain JavaScript ES modules; no npm dependencies. Telegram is the private front desk; GitHub Actions runs the agent. The VM mode elsewhere in the repository is independent.

Run locally with Node **22.13+** (tested with 22.23.2):

```powershell
cd worker
node --test test/
```

Tests use Node's built-in SQLite behind a D1-compatible in-memory adapter, fake KV, fixed clocks and fake HTTP transports. Node 22 emits an experimental SQLite warning. Production source imports no Node modules.

For a new installation, provision D1 `ccbot-state` and a KV namespace, apply `schema.sql` to D1, replace the original installation's IDs in `wrangler.jsonc`, and configure `REPOS` and defaults. Repository aliases must be unique, lowercase slugs, with one alias per target repo. Metering defaults to true, writing to false. The root [setup guide](../README.md#setup) covers deployment and the interactive secret script, which sets **`OWNER_ID`**, **`TELEGRAM_BOT_TOKEN`**, **`TELEGRAM_WEBHOOK_SECRET`**, **`GITHUB_TOKEN`**, **`CCBOT_SECRET`**, the target-repo Actions secrets, and the Telegram webhook. No secrets belong in source control. No provisioning or deployment is performed by these tests.

`src/index.js` exports the fetch and scheduled handlers. `commands.js`, `api.js`, and `cron.js` receive their store, Telegram, GitHub and clock dependencies. All internal times are unix seconds. Usage is charged at task completion and excludes unmetered tasks; rolling windows expire exactly at 60 minutes/24 hours, monthly resets use UTC.

D1's partial unique index enforces one active task per target repo. Results use an internal `finishing` state and claim token, then atomically publish conversation state and terminal task state. KV errors return 503 so the publisher can retry. Cron releases abandoned claims after five minutes. `/new` and `/done` bump separate conversation epochs, preventing late results from restoring a cleared conversation. Session blobs are stored as bytes at `session:{name}:{ask|do}:{epoch}` with task/session metadata: a missing or stale KV version returns 503 rather than loading the wrong session. The caller should retry transient 503 responses. Existing edit branches are only marked ready for checkout after a result confirms commits or a PR; a task producing no pushed changes retains its branch name with `continue_branch: false`.

Telegram API errors are caught and logged without tokens. Terminal callbacks do not resend an already recorded result; delivery failure does not rerun an agent. Notifications are best-effort (there is no durable delivery outbox). Request bodies, output and decoded sessions are bounded; the full allowed session size and the free-plan CPU budget still require validation in the actual Workers runtime. Shared wire fixtures are checked against the actual Python fetch/publish functions and replayed through the Worker handler, including a 60,000-character Unicode answer, a gzip tar session round-trip, null exit codes and empty patches. Python and Worker suites run independently, without launching one runtime from the other. Local tests do not verify Cloudflare bindings/propagation, live Telegram delivery or live GitHub workflow execution.

Builder handoff (2026-10-30): implementation DONE; `node --test test/` on Node v22.23.2 passed **41 tests, zero failures**, including the decoded 20 MB session boundary. Created `wrangler.jsonc`, `package.json`, `schema.sql`, this README, nine `src/*.js` modules, five `test/*.test.js` suites, and `test/{index,helpers}.js`. The required `.agents/codex-worker.md` Log append was **BLOCKED** by this session's filesystem permissions: `.agents/` is read-only, and the patch was rejected as outside the writable project. The orchestrator can copy this handoff into that Log. Live/runtime verification limits are listed above. No commit, deploy, provisioning, or Cloudflare-contacting Wrangler command was performed; concurrent changes outside `worker/` were left intact.
