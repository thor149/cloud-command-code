# Brief for Command Code — Cloud Command Code V2: deployment tooling (GCP)

You are one of two agents. Claude is rewriting the bot itself (`commandcode_bot/`, `tests/`) in parallel.
**Your job is everything needed to install, configure, update and verify it on a fresh Google Cloud
Ubuntu 24.04 VM**, plus CI and the README. Work only in the files you own.

## Files you own (create/edit only these)
- `deploy/install.sh` — new
- `deploy/configure.sh` — new
- `deploy/update.sh` — new
- `deploy/commandcode-bot.service` — rewrite
- `.github/workflows/ci.yml` — new
- `README.md` — full rewrite
- `.env.example`, `projects.example.json` — rewrite to the new contract below

## Files you must NOT touch
`commandcode_bot/**`, `tests/**`, `.agents/**` (except appending to the log at the bottom of this file),
`.gitignore`, `.gitattributes`, `requirements.txt`. Do not commit; Claude reviews and commits.

## Target layout on the VM (fixed — Claude's code depends on it)
| What | Where | Owner / mode |
|---|---|---|
| Service user | `ccbot` (system user, home `/home/ccbot`, shell `/bin/bash`) | — |
| App code + venv | `/opt/commandcode-bot` (`.venv` inside) | root:root, 755 |
| Secrets env file | `/etc/commandcode-bot/env` | root:root, 600 (read by systemd only) |
| Projects file | `/etc/commandcode-bot/projects.json` | root:ccbot, 640 |
| State | `/var/lib/commandcode-bot/state.json` (systemd `StateDirectory=commandcode-bot`) | ccbot |
| Repos | `/home/ccbot/repos/<name>` | ccbot |
| Edit-task worktrees | `/home/ccbot/worktrees` | ccbot |
| Per-repo deploy keys | `/home/ccbot/.ssh/deploy_<name>` + ssh config `Host github-<name>` | ccbot, 600 |

## Environment contract (`/etc/commandcode-bot/env`, and `.env.example` for docs)
```
TELEGRAM_BOT_TOKEN=            # from @BotFather (required)
TELEGRAM_USER_ID=              # numeric owner id (required)
PROJECTS_FILE=/etc/commandcode-bot/projects.json
COMMAND_CODE_BIN=/usr/bin/cmd  # absolute path of the Linux Command Code binary (install.sh detects it)
COMMAND_CODE_API_KEY=          # from Command Code Studio (required for headless auth)
STATE_FILE=/var/lib/commandcode-bot/state.json
WORKTREE_ROOT=/home/ccbot/worktrees
RUN_TIMEOUT_SECONDS=900        # read-only runs, 30–3600
EDIT_TIMEOUT_SECONDS=2700      # edit runs (/do), 30–7200
DEFAULT_MODEL=                 # optional, e.g. deepseek/deepseek-v4.1-flash ; empty = account default
DEFAULT_EFFORT=                # optional: low|medium|high|xhigh|max ; empty = model default
```
`projects.json` new schema (a bare string path still means read-only):
```json
{
  "hirable": {"path": "/home/ccbot/repos/hirable", "writable": true, "base": "main"},
  "notes": "/home/ccbot/repos/notes"
}
```

## Requirements per file
**install.sh** (run as root on a fresh Ubuntu 24.04 VM, idempotent — safe to re-run; `set -euo pipefail`):
apt update + `git python3 python3-venv python3-pip curl unzip ca-certificates`; Node.js 22 LTS via NodeSource;
`npm i -g command-code@latest`; detect the CLI binary (`command -v cmd || command -v commandcode || command -v cmdc`)
and record its absolute path; create user `ccbot`, all dirs above with the owners/modes above; 2 GB swapfile
if total RAM < 2 GB and no swap exists (persist in fstab); copy the app (the directory the script lives in,
`..`) to `/opt/commandcode-bot` (rsync-like, excluding `.venv`, `.git`, `.env`, `projects.json`); create the venv
and `pip install -r requirements.txt`; install the systemd unit and `daemon-reload`; enable the service, but
only **start** it if `/etc/commandcode-bot/env` exists and has a non-empty token — otherwise print the next step
(`sudo bash deploy/configure.sh`). Never print secrets.

**configure.sh** (root). Subcommands:
- `secrets` (default): hidden `read -rs` prompts for `TELEGRAM_BOT_TOKEN` and `COMMAND_CODE_API_KEY`, prompt for
  `TELEGRAM_USER_ID` (offer to run `sudo -u ccbot /opt/commandcode-bot/.venv/bin/python -m commandcode_bot.identify`
  first), keep existing values when the user just presses Enter, write the env file atomically with the contract
  above (mode 600), restart the service.
- `add-repo <name> <git-url> [--writable] [--base <branch>]`: validate name `^[a-z0-9][a-z0-9_-]*$`; for
  `git@github.com:` URLs generate an ed25519 deploy key for ccbot, add an ssh config alias `github-<name>`,
  print the **public** key with a one-line instruction ("GitHub → repo Settings → Deploy keys → Add, tick
  *Allow write access* if writable"), wait for Enter, then clone as ccbot using the alias; https URLs clone
  directly; set the repo's `git config user.name "Cloud Command Code"` / `user.email ccbot@localhost`;
  update projects.json with python3 `json` (never sed), mode 640 root:ccbot; restart the service.
- `remove-repo <name>`: drop it from projects.json (leave files), restart.
- `status`: `systemctl status --no-pager`, last 30 journal lines, configured project names (no secrets).

**update.sh** (root): re-copy the app from the directory it is run from into `/opt/commandcode-bot`, re-install
requirements, run `python -m unittest discover -s tests` from the copy, `systemctl restart`, show status. Abort
before restart if tests fail.

**commandcode-bot.service**: `User=ccbot`, `Group=ccbot`, `WorkingDirectory=/opt/commandcode-bot`,
`EnvironmentFile=/etc/commandcode-bot/env`, `ExecStart=/opt/commandcode-bot/.venv/bin/python -m commandcode_bot.bot`,
`StateDirectory=commandcode-bot`, `Restart=on-failure`, `RestartSec=5`, `NoNewPrivileges=true`, `PrivateTmp=true`,
`ProtectSystem=strict`, `ReadWritePaths=/home/ccbot /var/lib/commandcode-bot`, `UMask=0077`,
`Environment=HOME=/home/ccbot`, `KillMode=control-group`, `TimeoutStopSec=30`.

**ci.yml**: on push/PR — Python 3.12, `pip install -r requirements.txt`, `python -m unittest discover -s tests -v`;
plus a `shellcheck deploy/*.sh` job.

**README.md**: rewrite for V2. Sections: what it is (two modes: read-only `/ask` `/plan`; edit mode `/do` that
works on its own branch and never touches the base branch), command reference (see list below), security model
(owner-only private chat, isolated VM with **no service account**, deploy keys per repo, edit tasks on
`ccbot/*` branches, `/push` only pushes that branch), **Deploy on Google Cloud** (free-tier e2-micro in
us-central1, separate project, the exact gcloud commands below), Telegram bot creation via @BotFather,
Command Code API key from Command Code Studio, `install.sh` → `configure.sh secrets` → `configure.sh add-repo`,
updating, troubleshooting (journalctl, "only one poller per token", RAM/swap on e2-micro), local tests.

Bot commands to document: `/start`, `/help`, `/projects`, `/use <project>`, `/status`, `/ask <q>` (plain text =
ask), `/plan <q>`, `/do <task>` (writable projects only), `/diff`, `/push`, `/discard`, `/new`, `/cancel`,
`/model [id]`, `/models`, `/effort [level]`.

GCP commands to document (Claude runs these):
```
gcloud projects create cloud-command-code-<suffix> --name="Cloud Command Code"
gcloud billing projects link <project> --billing-account=<ACCOUNT_ID>
gcloud services enable compute.googleapis.com --project <project>
gcloud compute instances create ccbot --project <project> --zone us-central1-a --machine-type e2-micro \
  --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud --boot-disk-size 30GB \
  --boot-disk-type pd-standard --no-service-account --no-scopes --shielded-secure-boot
gcloud compute ssh ccbot --project <project> --zone us-central1-a
```
(No inbound firewall rule is needed beyond the default SSH; the bot uses outbound long polling.)

## Done when
- `bash -n` passes on every script, and they are shellcheck-clean (run `shellcheck` if available; otherwise
  reason carefully about quoting).
- Nothing prints or logs a secret; secrets are only ever read with hidden prompts.
- Append a short DONE note to the log below: files changed, anything you could not verify.
- If blocked, append BLOCKED with the reason instead of guessing.

## Log
