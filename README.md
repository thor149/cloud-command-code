# Cloud Command Code — serverless edition

Run [Command Code](https://commandcode.ai) on your repositories from a private Telegram chat. A Cloudflare Worker handles commands and conversation state; GitHub Actions runs each task inside the repository it is working on. Successful edit tasks return a `ccbot/*` branch and a pull request for you to review.

There is no always-on server. The Worker uses D1 for task metadata and KV for resumable Command Code sessions. The existing [self-hosted VM mode](#self-hosted-vm-mode) is also supported.

**Deployment defaults to paused AI execution.** `TASK_EXECUTION: "paused"` keeps owner controls available but refuses `/ask`, `/plan`, `/do` and plain-text prompts before dispatch. Missing or unrecognized configuration also pauses execution without disabling controls or result callbacks. After verifying model access, hosting costs and spending limits, an operator can set `TASK_EXECUTION: "enabled"` in `worker/wrangler.jsonc` and redeploy. Telegram `/model` cannot lift the pause. Existing sessions and branches are retained; already-running model processes must be stopped separately. This switch prevents new AI work, not charges from an existing VM or other services. See [the phone-only architecture assessment](docs/PHONE_ONLY_ARCHITECTURE.md).

```text
Your private Telegram chat
          |
          v
Cloudflare Worker (/telegram) ---- D1: tasks, settings, conversations, usage
          |                       KV: session archives
          | workflow_dispatch(task_id)
          v
Target repo: .github/workflows/ccbot.yml
          | calls this repo's reusable task.yml
          v
agent job (contents: read) -- untrusted artifact --> publish job (trusted)
          |                                         |
          | HMAC /started                            | validate patch, push ccbot/*, open PR
          +------------> Worker <--------------------+ HMAC /result
                            |
                            v
                    Telegram answer + PR link
```

## Commands

| Command | Behaviour |
|---|---|
| `/start`, `/help` | Show help |
| `/repos`, `/use <name>` | List configured repositories and choose the active one |
| `/ask <question>` or plain text | Investigate the code in read-only plan mode |
| `/plan <goal>` | Produce a read-only implementation plan; shares the `/ask` session |
| `/do <task>` | Edit a writable repo; push a task branch and open a PR when there are changes |
| `/done` | End the edit conversation; the next `/do` starts a fresh branch and session |
| `/new` | Start a fresh read-only conversation |
| `/cancel` | Cancel the active repo's queued/running task |
| `/status` | Show the repo, active task/run link, model, effort and usage |
| `/model [id\|default]` | Show or set the model; `default` clears the override |
| `/effort [low\|medium\|high\|xhigh\|max\|default]` | Show or set reasoning effort |
| `/usage` | Show usage, free-up/reset times and daily/monthly projections |
| `/limits` | Show limits; set one with `/limits hourly 60`, `/limits daily 180`, `/limits monthly 1800`, or `/limits daily off` |

Commands addressed to `/command@BotName` also work. Prompts are limited to 4,000 characters, with one queued/running task per repo. Follow-up `/do` commands reuse the branch and session until `/done`. An empty patch is a successful no-op; a missing artifact is reported as a failed run. Long answers arrive as Markdown documents.

## Setup

You need a Cloudflare account, GitHub repositories with Actions enabled, PowerShell 7, Node 22.13+, the GitHub CLI (`gh`) and Wrangler (the commands below use `npx wrangler`). Authenticate `gh auth login` and `npx wrangler login` on your own machine.

1. Create a Telegram bot through [@BotFather](https://t.me/BotFather) (`/newbot`). Create a Command Code API key in [Command Code Studio](https://commandcode.ai/docs/studio). Create a GitHub **fine-grained personal access token**, restricted to your target repos, with **Actions: read and write**. The Worker uses it only to dispatch/cancel runs; publishing uses the separate, repository-scoped Actions token.
2. Copy [templates/ccbot.yml](templates/ccbot.yml) into **each target repo** as `.github/workflows/ccbot.yml`, on its base branch. It calls `thor149/cloud-command-code/.github/workflows/task.yml@main` and explicitly passes `COMMAND_CODE_API_KEY`, `CCBOT_SECRET` and `CCBOT_API`. This repository includes its own copy. If you fork the runner, update both the reusable-workflow URL and the runner-repo checkouts in `task.yml`.
3. Provision your own D1 database and KV namespace. From `worker/`:

   ```powershell
   npx wrangler d1 create ccbot-state
   npx wrangler kv namespace create SESSIONS
   ```

   Set their returned IDs in [worker/wrangler.jsonc](worker/wrangler.jsonc); the checked-in IDs belong to the original installation. Set a unique Worker `name`. Add target repos to the JSON string in `vars.REPOS`, using a unique lowercase alias for each repo:

   ```json
   {"app":{"repo":"you/app","base":"main","writable":true,"metered":true,"workflow":"ccbot.yml"}}
   ```

   Use `metered: false` for public repos and `writable: false` for read-only repos. Set `DEFAULT_LIMITS`, `DEFAULT_MODEL`, `DEFAULT_EFFORT` and `EDIT_MODE` as needed. Apply the schema and deploy to obtain your Worker URL:

   ```powershell
   npx wrangler d1 execute ccbot-state --remote --file schema.sql
   npx wrangler deploy
   ```

4. From the repository root, run the secret setup yourself:

   ```powershell
   pwsh -File deploy/serverless/setup-secrets.ps1 -WorkerUrl https://cloud-command-code.YOUR-ACCOUNT.workers.dev -Repos you/app
   ```

   The script prompts privately for your bot token, Command Code key and fine-grained token, discovers your Telegram owner ID from a private message, generates the HMAC/webhook secrets, sets Worker and target-repo Actions secrets, and registers `/telegram` as the webhook. It does not write secrets into source files. For several repos, invoke from PowerShell with `& ./deploy/serverless/setup-secrets.ps1 -WorkerUrl 'https://cloud-command-code.YOUR-ACCOUNT.workers.dev' -Repos @('you/app','you/docs')`. Use a fresh bot if your current bot is already polling or has another webhook.
5. Enable **Allow GitHub Actions to create and approve pull requests** in each target repo's Actions settings (subject to organization policy), and protect the base branch with required PR review. Send `/start`, `/repos`, then `/use app`. Try `/ask Where is the entry point?`, followed by a small `/do` task on a writable repo. Review the returned PR yourself before merging. When changing `REPOS` or other Worker variables later, run `npx wrangler deploy` from `worker/` again and ensure each new target has the workflow and three secrets.

## Security model

- **Owner only:** the Worker verifies the Telegram webhook secret and accepts messages only when sender ID, private-chat ID and configured owner ID all match. Update IDs are deduplicated.
- **Authenticated callbacks:** every `/api/*` request uses HMAC-SHA256 over the method, URL path without query, timestamp and exact body hash. Timestamps have a five-minute acceptance window; the Worker verifies the MAC with Web Crypto. Cancelled fetches return 410 and stop the runner quietly.
- **Two jobs:** the agent gets `contents: read` only, with the Command Code key as its only application secret. The Command Code step receives no `GITHUB_TOKEN` or `CCBOT_*` variables. All checkouts disable persisted Git credentials. A separate publish job re-fetches the task, treats the artifact as untrusted, applies accepted changes and uses a one-off Git authorization header to push.
- **Deny policy:** the runner installs [deploy/commandcode-policy.json](deploy/commandcode-policy.json). `/ask` and `/plan` use plan mode. Default `edits` mode also denies shell/web/MCP tools; `full` permits shell commands for builds and tests. The CLI uses `--yolo` for edits; deny rules remain the intended tool-policy boundary.
- **Branch-only edits:** the publisher accepts only `ccbot/*` task branches, rejects patches over 5 MB and changes to `.github/workflows/`, and checks the applied commits before pushing. It opens a PR and never merges it. **Review by the owner and GitHub branch protection are essential.**
- **Sessions and artifacts:** gzip tar members are relative to `~/.commandcode`; extraction rejects traversal, links and devices and bounds archive expansion. Decoded session uploads are limited to 20 MB. Artifacts have one-day retention and are deleted after publishing, with best-effort cleanup on failure.

The CLI and repository run as the agent runner's OS user; deny rules are not an OS sandbox. In `full` mode arbitrary code can access the Command Code API key and the agent job's files. The separate publish runner keeps the write token and HMAC key out of that job. Treat generated code, answers and sessions as untrusted. Telegram, Command Code, GitHub and Cloudflare process relevant prompts/results; avoid including secrets in prompts.

## Usage and cost limits

Standard GitHub-hosted Actions runners have unlimited free compute minutes for **public** repos; **private** repos draw from a monthly plan allowance and can incur charges. Larger runners and artifact storage have separate billing rules. See [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

The bot defaults to hourly/daily/monthly limits of 60/180/1,800 minutes on metered repos. Hourly and daily windows roll over 60 minutes and 24 hours; monthly resets at the start of the UTC calendar month. Usage rounds task runtime up to whole minutes (minimum one), and counts cancelled/failed/timed-out tasks. Unmetered repos are excluded. Limits are checked before dispatch, so an in-flight task can exceed the remaining budget.

`/usage` is a task-runtime estimate: it does not measure both Actions jobs, checkout/install overhead, other workflows or artifact storage. It is not a GitHub billing cap. Configure GitHub billing budgets separately. Cloudflare free-plan quotas and Command Code API charges still apply; serverless hosting does not make model usage free. Queued tasks that never start fail after 20 minutes; stalled running tasks expire after their timeout plus 15 minutes, checked by the 15-minute cron.

## Self-hosted VM mode

The original Python Telegram bot runs continuously on an Ubuntu VM with Node 22, Python, Git, systemd and outbound internet access. Use a dedicated VM/project without a cloud service account; size it for the builds you intend to run. A 1 GB VM benefits from the installer's 2 GB swap file.

Copy this repository to the VM, then:

```bash
sudo bash deploy/install.sh
sudo bash deploy/configure.sh secrets
sudo bash deploy/configure.sh add-repo app git@github.com:you/app.git --writable
sudo bash deploy/configure.sh add-repo notes https://github.com/you/notes.git
sudo bash deploy/configure.sh status
```

The secrets command asks for your Telegram bot token, Command Code key, owner ID, model, effort and edit mode. Use a **separate bot** from serverless mode: Telegram polling and webhooks cannot serve the same bot simultaneously. For SSH repos, add the printed public deploy key under that repo's **Settings → Deploy keys**, granting write access only where needed. The base defaults to the remote default; override with `--base <branch>`. The service starts after secrets and at least one repo are configured.

VM commands include `/projects`, `/sync`, `/diff`, `/push` and `/discard`; `/do` works in an isolated Git worktree and `/push` explicitly publishes its branch. Serverless `/repos`, `/done`, `/usage` and `/limits` are not VM commands. Configuration references: [.env.example](.env.example) and [projects.example.json](projects.example.json).

The installer creates the unprivileged `ccbot` user, installs a root-owned global deny policy and a systemd service. Credentials live in `/etc/commandcode-bot/env`; the agent environment strips `TELEGRAM_*`. In full mode the agent shares the bot's OS user and can access its deploy keys, so protect base branches and review PRs. To update, copy a fresh checkout to the VM and run `sudo bash deploy/update.sh`; it runs tests before replacing the installed app. Inspect `journalctl -u commandcode-bot -f` for failures. An edits-mode task cannot run shell-based tests; use full mode deliberately when needed.

## Development and verification

Python 3.11+ and Git are required. Worker tests need Node 22.13+ (built-in SQLite). No live tokens, network or Command Code account are needed by either suite.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m unittest discover -s tests
cd worker
node --test test/
```

On Unix use `.venv/bin/python`. CI runs both suites (Python 3.12, Node 22) and ShellCheck. Shared signed fixtures verify the Python runner's exact HTTP bytes against the Worker handler, including null exit codes, Unicode output and session round-trips. Tests use temporary Git repositories, SQLite, fake KV/HTTP and a fake agent. Passing locally does not verify deployed Cloudflare bindings, Telegram delivery or a live GitHub workflow. See [worker/README.md](worker/README.md) for storage and runtime details. Licensed under [MIT](LICENSE).
