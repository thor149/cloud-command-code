# Cloud Command Code — serverless edition (spec, owned by Claude)

Goal: $0 hosting. A **Cloudflare Worker** (free plan) is the Telegram front desk and keeps state in **D1** and
**KV**. Every Command Code run happens in **GitHub Actions** inside the repository being worked on (GitHub's
terms: hosted runners only for that repo's own development). The existing VM mode (`commandcode_bot/bot.py`,
`deploy/`) stays as an alternative and must keep passing its tests.

```
Telegram ──webhook──► Worker (/telegram) ──workflow_dispatch(task_id)──► <target repo>/.github/workflows/ccbot.yml
                         ▲  D1: tasks, settings, conversations, updates            │ uses thor149/cloud-command-code/.github/workflows/task.yml
                         │  KV: session blobs                                       ▼
                         └──── HMAC-signed /api/tasks/* ◄──── job "agent" (untrusted) ─artifact─► job "publish" (trusted)
```

## 1. Job ⇄ Worker HTTP contract (both sides MUST implement exactly this)

Base URL = secret `CCBOT_API` (e.g. `https://cloud-command-code.<acct>.workers.dev`), no trailing slash.

**Auth on every `/api/*` request.** Headers:
- `X-Ccbot-Timestamp`: unix seconds (string)
- `X-Ccbot-Signature`: `sha256=` + lowercase hex of HMAC-SHA256(key = `CCBOT_SECRET` as UTF-8,
  message = `METHOD + "\n" + PATH + "\n" + TIMESTAMP + "\n" + sha256_hex(BODY)`), where PATH excludes the
  query string and BODY is the exact request body bytes (empty string for GET).
- Worker rejects (401) if the signature is wrong (constant-time compare) or |now − timestamp| > 300 s.

**`GET /api/tasks/{id}`** → `200` JSON:
```json
{
  "id": "t_…", "kind": "ask" | "plan" | "do",
  "repo": "owner/name", "name": "short-name",
  "prompt": "raw user text",
  "base": "main",
  "branch": "ccbot/…" | null,          // do: the task branch (Worker chooses it)
  "continue_branch": true | false,      // do: true = branch already exists on origin, check it out
  "session_id": "uuid" | null,          // Command Code session to resume
  "session": "<base64 of .tar.gz>" | null,
  "model": "deepseek/deepseek-v4.1-flash" | null,
  "effort": "max" | null,
  "edit_mode": "edits" | "full",
  "timeout_minutes": 15 | 45
}
```
`410` if the task was cancelled, `404` if unknown. Fetching does not change state.

**`POST /api/tasks/{id}/started`** body `{"run_id": 123, "run_url": "https://github.com/…/actions/runs/123"}`
→ `204`. Sets status `running`, `started_at`, run id/url. Worker sends Telegram "Started: <run_url>".

**`POST /api/tasks/{id}/result`** body:
```json
{
  "ok": true, "exit_code": 0, "timed_out": false,
  "text": "final answer (≤ 60000 chars)",
  "session_id": "uuid" | null,
  "session": "<base64 .tar.gz>" | null,      // ≤ 20 MB decoded
  "tool_calls": 12,
  "branch": "ccbot/…" | null, "commits": 2, "pr_url": "https://github.com/…/pull/7" | null,
  "error": "message" | null
}
```
→ `204`. Sets `done`/`failed`, `finished_at`, `minutes = max(1, ceil((finished_at − started_at)/60))`
(if never started, minutes = 1). Stores session (see §3). Sends the Telegram result (§2). Idempotent: a second
result for a finished task is ignored with `204`.

Session tarball format: gzip tar whose members are paths **relative to `~/.commandcode`**, e.g.
`projects/<slug>/<session_id>.jsonl`, `.checkpoints.jsonl`, `.meta.json`.

## 2. Telegram behaviour (Worker)

Webhook `POST /telegram`, header `X-Telegram-Bot-Api-Secret-Token` must equal `TELEGRAM_WEBHOOK_SECRET`
(else 401). Dedupe by `update_id` (D1). Only `message.from.id == OWNER_ID` **and** `chat.type == "private"`
**and** `chat.id == OWNER_ID` is processed; everything else is ignored silently (still 200).

Commands (`/cmd@BotName` must also work):
| Command | Behaviour |
|---|---|
| `/start`, `/help` | command list |
| `/repos` | configured repos, writable/metered flags, active marker |
| `/use <name>` | set active repo (persisted) |
| `/ask <q>` or plain text | queue kind `ask` |
| `/plan <goal>` | queue kind `plan` |
| `/do <task>` | queue kind `do` (writable repos only). First `/do` creates branch `ccbot/<name>-<yyyymmdd-hhmm>-<slug>`; later `/do`s continue the same branch and session until `/done` |
| `/done` | end the current edit task (next `/do` starts a new branch); branch/PR stay on GitHub |
| `/new` | fresh read-only conversation for the active repo |
| `/cancel` | cancel the queued/running task of the active repo (GitHub API cancel if run id known) |
| `/status` | active repo, current task (status, elapsed, run link), model/effort, usage summary line |
| `/model [id\|default]`, `/effort [low\|medium\|high\|xhigh\|max\|default]` | persisted settings |
| `/usage` | usage report + projections (§4) |
| `/limits` | show limits; `/limits hourly 60`, `/limits daily 180`, `/limits monthly 1800`, `/limits daily off` |

Queue rules: one active (queued/running) task per repo → otherwise "A task is already running"; prompt ≤ 4000
chars; limits checked before dispatch (§4). Dispatch:
`POST https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches` with
`{"ref": base, "inputs": {"task_id": id}}`, headers `Authorization: Bearer GITHUB_TOKEN`,
`Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`, `User-Agent: cloud-command-code`.
On dispatch failure: mark task `failed` and tell the user the GitHub error (status + message).
Cancel: `POST /repos/{repo}/actions/runs/{run_id}/cancel`.

Result message: header (`✅ Done` / `❌ Failed (exit N)` / `⏱ Timed out` / `🛑 Cancelled`), minutes used,
then the text. If `pr_url`: "PR: <url> — paste it to Claude for review and merge." Long text (> 3500 chars) is sent
as a `.md` document via `sendDocument` (multipart) with a short caption. Telegram API errors must not throw.

Cron (`*/15 * * * *`): tasks `queued` > 20 min → `failed` ("workflow never started — is ccbot.yml on the base
branch and are the repo secrets set?"); `running` > timeout_minutes + 15 → `timeout`. Notify the owner.

## 3. Conversations and sessions

Table `conversations(repo, kind ∈ {ask, do}, epoch, session_id, branch, updated_at)`. `/new` bumps the `ask`
epoch; `/done` bumps the `do` epoch and clears its branch. A task records the epoch at creation; its result only
updates the conversation if the epoch is unchanged. Session blobs live in KV under
`session:{name}:{kind}:{epoch}` (raw bytes, decoded from base64); the task fetch returns the latest blob for its
conversation, base64-encoded. `ask` and `plan` share the `ask` conversation.

## 4. Usage limits and projections (Worker)

Usage = `minutes` of finished/cancelled/timed-out tasks on repos with `metered: true` (public repos can be
`metered: false`: unlimited Actions, not counted). Windows: **hourly** = rolling last 60 min, **daily** =
rolling last 24 h, **monthly** = calendar month UTC (matches GitHub's monthly allowance). Limits are minutes,
or off. Defaults from var `DEFAULT_LIMITS` (e.g. `{"hourly":60,"daily":180,"monthly":1800}`), overridable with
`/limits` (persisted). Before dispatch on a metered repo: if any window's usage ≥ its limit, refuse with which
limit and when it frees up (rolling: when the oldest counted minutes leave the window; monthly: 1st of next month
00:00 UTC).

`/usage` shows, per window: used / limit, %, and reset/free-up time. Projections:
- daily rate = minutes in the last 7 days ÷ 7 (if < 7 days of history, use days since first task, min 1).
- monthly: "At ~X min/day the monthly limit lasts until <date> (N days); month resets <date>." If it outlasts the
  month: "won't run out this month".
- daily: "At the last hour's pace (Y min/h) the daily limit is reached in ~Z h." (skip if pace is 0).
Keep the projection maths in a pure function with unit tests.

## 5. GitHub Actions side (repo `thor149/cloud-command-code`)

`templates/ccbot.yml` (copied into each target repo as `.github/workflows/ccbot.yml`):
- `on: workflow_dispatch` with input `task_id`; `concurrency: {group: ccbot, cancel-in-progress: false}`;
  `permissions: {contents: write, pull-requests: write, actions: write}`;
- one job `uses: thor149/cloud-command-code/.github/workflows/task.yml@main` with `task_id`, passing secrets
  `COMMAND_CODE_API_KEY`, `CCBOT_SECRET`, `CCBOT_API`.

`.github/workflows/task.yml` (`on: workflow_call`):
- **Job `agent`** (`permissions: contents: read`, `timeout-minutes: 55`): checkout this runner repo to
  `runner/` (ref `main`); `python3 -m commandcode_bot.task fetch` (env `CCBOT_API`, `CCBOT_SECRET`,
  `CCBOT_TASK_ID`; writes `$RUNNER_TEMP/ccbot/task.json` without the session, restores the session into
  `~/.commandcode`, posts `/started`, sets step outputs `ref` = branch if continue_branch else base, `kind`);
  if the fetch returns 410 the job ends quietly; checkout the target repo to `repo/` at that ref with
  `persist-credentials: false`, `fetch-depth: 0`; setup Node 22; `npm i -g command-code@latest`; install the
  global deny policy (`runner/deploy/commandcode-policy.json` → `~/.commandcode/settings.json`);
  `python3 -m commandcode_bot.task agent` with **only** `COMMAND_CODE_API_KEY` in env, cwd `repo/`;
  upload artifact `ccbot-out` (retention 1 day) containing `result.json`, `changes.patch` (do only),
  `session.tar.gz`.
- **Job `publish`** (`needs: agent`, `if: ${{ !cancelled() }}`, `permissions: contents: write,
  pull-requests: write, actions: write`): checkout runner repo; re-fetch the task (trusted); download the
  artifact (if any); for `do` with an ok result and a non-empty patch: validate it (reject any path under
  `.github/workflows/`, reject > 5 MB), checkout the target repo (`persist-credentials: false`) at the branch
  (if continue_branch) or base, create/switch to `branch`, `git am --3way` the patch, push
  `HEAD:refs/heads/<branch>` using a one-off `-c http.extraheader=AUTHORIZATION: basic …` from `GITHUB_TOKEN`
  (never persisted), then `gh pr create` if no open PR exists for the branch (title `ccbot: <first prompt line>`,
  body = prompt + agent summary, both truncated); finally `python3 -m commandcode_bot.task publish` posts
  `/result` (with session and pr_url), then deletes this run's artifacts via the GitHub API. If the agent job
  failed or produced no artifact, post a failed result with a useful error.

`commandcode_bot/task.py` reuses `runner.run_headless`, `runner.command_args`, `runner.StreamParser`,
`gitops.prepare_agent_settings` and `gitops.commit_all`. Prompt prefixes move to `commandcode_bot/prompts.py`
(shared with `bot.py`). It must only use the standard library (no pip install in Actions).
