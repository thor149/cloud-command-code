# Cloud Command Code

A private Telegram bot that runs [Command Code](https://commandcode.ai) on your repositories from a small
cloud VM. Ask questions about a codebase from your phone, or hand it a task and get back a branch with the
change, a diff, and a pull-request link.

- **Read-only mode** (any project): `/ask` and `/plan` run Command Code in plan mode. It reads code; it can't
  edit files or run commands. Follow-up questions continue the same conversation until `/new`.
- **Edit mode** (projects marked writable): `/do` creates a fresh `ccbot/*` branch in its own git worktree,
  lets Command Code work there, then reports the commits. Send `/do` again to keep going on the same branch.
  `/push` pushes only that branch. The server chooses how much the agent may do:
  - `EDIT_MODE=edits` (default): file edits only, enforced by Command Code's fail-closed `dont-ask` mode
    with shell denied. The bot commits the result after a successful run.
  - `EDIT_MODE=full`: edits plus shell commands, so it can install dependencies, run tests and commit itself
    (`--yolo`). Deny rules still apply.

Only one Telegram account (yours) can use the bot, and only in a private chat.

## Commands

| Command | What it does |
|---|---|
| `/projects`, `/use <name>` | List projects; pick the one to work on (remembered across restarts) |
| `/ask <question>` or plain text | Investigate and answer (read-only) |
| `/plan <goal>` | Investigate and produce a plan (read-only) |
| `/new` | Start a fresh conversation for the project |
| `/sync` | Fast-forward the project's checkout to the latest base branch |
| `/do <task>` | Work on the task in a `ccbot/*` branch (writable projects only) |
| `/diff` | The task branch's full diff as a file |
| `/push` | Push the task branch and get a "open pull request" link |
| `/discard` | Delete the task branch and its worktree |
| `/status` | What is running, elapsed time, tool calls, model, open task |
| `/cancel` | Stop the running job |
| `/model [id\|default]`, `/models` | Show, set or list models (e.g. `deepseek/deepseek-v4.1-flash`) |
| `/effort [level\|default]` | `low`, `medium`, `high`, `xhigh` or `max` |

While a job runs, the bot edits its "working" message every minute with elapsed time, tool calls and the last
tool used. One job runs at a time; `/status` and `/cancel` stay responsive.

## Security model

- **Owner only.** Every handler checks your numeric Telegram ID *and* that the chat is a private chat with you.
  Everyone else, and every group, is ignored silently.
- **Fixed permission boundary.** Telegram text is passed to Command Code as a single prompt argument. The
  permission mode comes from the bot's own code: plan mode for `/ask` and `/plan`; for `/do`, `edits` or
  `full` from the server config, never from chat. In `edits` mode the bot writes a per-run
  `.commandcode/settings.local.json` into the worktree (git-excluded) that denies Shell. A deny always beats
  any allow rule a repository ships in its own `.commandcode/settings.json`.
- **Global deny policy.** `deploy/commandcode-policy.json` is installed as `/home/ccbot/.commandcode/settings.json`,
  root-owned in a sticky directory, so the agent can't change or delete it. In every mode it blocks reading
  `.env`/key files and `~/.ssh`, editing the main checkouts in `~/repos`, `git push`, `git remote`,
  `git config`, and MCP tools.
- **Isolated VM.** The Google Cloud VM is in its own project and has **no service account**, so even a fully
  compromised bot cannot reach any other cloud resource.
- **Sandboxed service.** systemd runs the bot as the unprivileged user `ccbot` with `ProtectSystem=strict`:
  it can write only to `/home/ccbot` (repos, worktrees, caches) and its state directory.
- **Secrets stay put.** The bot token, your ID and the Command Code API key live in
  `/etc/commandcode-bot/env` (root-only, mode 600), loaded by systemd. `TELEGRAM_*` variables are stripped
  from the agent's environment, and the bot token is redacted from logs.
- **Per-repo deploy keys.** Each GitHub repo gets its own SSH deploy key. Give write access only to repos you
  mark writable; `/push` refuses anything that is not a `ccbot/*` branch.

**Limits, stated plainly.** The agent runs as the same Linux user as the bot. In `full` mode it can run
arbitrary commands as `ccbot`. The deny rules stop the obvious routes, but a determined program could still
read the Command Code API key from its environment or use the deploy key to push. So for writable repos,
**protect the base branch on GitHub** (Settings → Branches → require a pull request). Then nothing reaches
`main` without your review. Stay on `edits` mode unless you need the agent to run tests itself.

Telegram messages and answers pass through Telegram's servers. Don't ask the bot to print secrets or private data.

## Deploy on Google Cloud

The bot only makes outbound connections (Telegram long polling, the Command Code API, git), so it needs no
inbound ports beyond SSH.

### 1. Create an isolated VM

An `e2-micro` in `us-central1`, `us-west1` or `us-east1` with a 30 GB standard disk falls under Google Cloud's
Always Free tier (one per billing account). Google may bill a small amount for the VM's external IPv4 address.

```bash
PROJECT=cloud-command-code-<suffix>
gcloud projects create "$PROJECT" --name="Cloud Command Code"
gcloud billing projects link "$PROJECT" --billing-account=<BILLING_ACCOUNT_ID>
gcloud services enable compute.googleapis.com --project "$PROJECT"
gcloud compute instances create ccbot --project "$PROJECT" --zone us-central1-a \
  --machine-type e2-micro --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud \
  --boot-disk-size 30GB --boot-disk-type pd-standard \
  --no-service-account --no-scopes --shielded-secure-boot
```

Copy this project to the VM and install:

```bash
git archive --format=tar.gz --prefix=commandcode-bot/ -o ccbot.tar.gz HEAD
gcloud compute scp ccbot.tar.gz ccbot:/tmp/ --project "$PROJECT" --zone us-central1-a
gcloud compute ssh ccbot --project "$PROJECT" --zone us-central1-a
# on the VM:
tar -xzf /tmp/ccbot.tar.gz -C ~ && cd ~/commandcode-bot
sudo bash deploy/install.sh
```

`install.sh` is safe to re-run. It installs Node.js 22, the Command Code CLI, Python and git, creates the
`ccbot` user, adds a 2 GB swap file on small VMs, installs the app to `/opt/commandcode-bot`, and enables the
systemd service.

### 2. Create the Telegram bot and get a Command Code API key

1. In Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot`, and keep the token it gives you.
2. Send your new bot any private message (this lets the setup script find your user ID).
3. Create an API key in [Command Code Studio](https://commandcode.ai/docs/studio).

### 3. Configure

```bash
sudo bash deploy/configure.sh secrets
```

This asks, with hidden input, for the bot token and the API key. It can look up your Telegram user ID for you,
then asks for an optional default model and effort, and the edit mode (`edits` or `full`). Before every
restart it validates the whole configuration (`python -m commandcode_bot.check`), and the unit gives up after
five failed starts, so a typo can't cause a restart loop.

### 4. Add repositories

```bash
sudo bash deploy/configure.sh add-repo hirable git@github.com:thor149/hirable.git --writable
sudo bash deploy/configure.sh add-repo notes https://github.com/you/public-notes.git
```

For `git@github.com:` URLs the script creates a deploy key and prints the public key. Add it under the GitHub
repo's **Settings → Deploy keys**, ticking **Allow write access** for writable repos, then press Enter. The
GitHub host key is checked against GitHub's published fingerprint. The base branch defaults to the remote's
default branch; override with `--base <branch>`.

The service starts once secrets and at least one repo are configured. Message the bot `/start`.

### Updating

Copy the new code to the VM as in step 1, then from the new checkout:

```bash
sudo bash deploy/update.sh
```

It runs the test suite against the new code in a throwaway virtualenv, and only then installs requirements,
replaces `/opt/commandcode-bot` and restarts. If the tests fail, the running service and its environment are
left untouched.

## Troubleshooting

- `sudo bash deploy/configure.sh status` shows the service state, the last 30 log lines, and the configured
  projects. `journalctl -u commandcode-bot -f` follows the log.
- **"Conflict: terminated by other getUpdates request"**: only one process can poll a bot token. Stop any copy
  of the bot running elsewhere.
- **Command Code authentication errors**: re-run `configure.sh secrets` with a fresh API key, or
  `sudo -u ccbot -H cmd login`.
- **Slow or killed runs on e2-micro**: 1 GB RAM plus 2 GB swap suits most tasks. For heavy builds, stop the VM
  and change the machine type to `e2-small` or `e2-medium`.
- **A `/do` task can't run tests**: in `edits` mode Command Code may edit files but can't run shell commands.
  Switch to `full` with `configure.sh secrets` if you want it to run tests and builds, after protecting the
  base branch (see the security model).
- **A run stops early with "permission denied" (exit 4)**: in `edits` mode, anything other than reading and
  editing files is denied by design. Rephrase the task, or use `full` mode.

## Local development

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt   # Windows: .venv\Scripts\python.exe
.venv/bin/python -m unittest discover -s tests -v
```

The tests use temporary git repositories and a fake Command Code process. They need `git`, but no network,
Telegram token or Command Code account. CI runs them and `shellcheck` on every push.

Configuration reference: [.env.example](.env.example) and [projects.example.json](projects.example.json).
Command Code flags: [headless mode](https://commandcode.ai/docs/headless).
