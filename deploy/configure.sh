#!/usr/bin/env bash
# Configure Cloud Command Code after install.sh. Run as root:
#   sudo bash deploy/configure.sh secrets
#   sudo bash deploy/configure.sh add-repo <name> <git-url> [--writable] [--base <branch>]
#   sudo bash deploy/configure.sh remove-repo <name>
#   sudo bash deploy/configure.sh status
# Secrets are only ever read with hidden prompts and are never printed.
set -euo pipefail

APP_DIR=/opt/commandcode-bot
CONF_DIR=/etc/commandcode-bot
ENV_FILE=$CONF_DIR/env
PROJECTS=$CONF_DIR/projects.json
BOT_USER=ccbot
BOT_HOME=/home/$BOT_USER
SERVICE=commandcode-bot
# GitHub's published ed25519 host key fingerprint (docs.github.com, "GitHub's SSH key fingerprints").
GITHUB_ED25519_FP="SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"

die() { echo "Error: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo bash deploy/configure.sh ...)"
[[ -x $APP_DIR/.venv/bin/python ]] || die "run deploy/install.sh first"

get_env() {
  [[ -f $ENV_FILE ]] || return 0
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1
}

ready() {
  [[ -s $ENV_FILE ]] && grep -Eq '^TELEGRAM_BOT_TOKEN=.+' "$ENV_FILE" &&
    [[ -s $PROJECTS ]] &&
    python3 -c 'import json, sys; sys.exit(0 if json.load(open(sys.argv[1])) else 1)' "$PROJECTS"
}

validate_config() {
  # Load the env file the way systemd would and run the bot's own config check as ccbot.
  (
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
    runuser -u "$BOT_USER" -- env -C "$APP_DIR" HOME="$BOT_HOME" "$APP_DIR/.venv/bin/python" -m commandcode_bot.check
  )
}

restart_if_ready() {
  if ready; then
    if ! validate_config; then
      echo "Not restarting: fix the configuration error above first."
      return 1
    fi
    systemctl reset-failed "$SERVICE" 2>/dev/null || true
    systemctl enable "$SERVICE" >/dev/null
    systemctl restart "$SERVICE"
    sleep 3
    if systemctl is-active --quiet "$SERVICE"; then
      echo "Service is running. Message your bot /start in Telegram."
    else
      echo "Service failed to start. Recent log:"
      journalctl -u "$SERVICE" -n 30 --no-pager
    fi
  else
    echo "Not starting yet: both 'secrets' and at least one 'add-repo' are needed."
  fi
}

# ask_secret <label> <variable-name>: hidden prompt; Enter keeps the current value.
ask_secret() {
  local label=$1 var=$2 input hint
  hint="hidden"
  [[ -n ${!var} ]] && hint="hidden; press Enter to keep the current value"
  read -rsp "$label ($hint): " input
  echo
  if [[ -n $input ]]; then
    printf -v "$var" '%s' "$input"
  fi
}

# ask_plain <label> <variable-name>: visible prompt with the current value as default.
ask_plain() {
  local label=$1 var=$2 input
  read -rp "$label [${!var:-none}]: " input
  if [[ -n $input ]]; then
    printf -v "$var" '%s' "$input"
  fi
}

cmd_secrets() {
  local token apikey userid model effort mode cmdbin answer tmp
  token="$(get_env TELEGRAM_BOT_TOKEN)"
  apikey="$(get_env COMMAND_CODE_API_KEY)"
  userid="$(get_env TELEGRAM_USER_ID)"
  model="$(get_env DEFAULT_MODEL)"
  effort="$(get_env DEFAULT_EFFORT)"
  mode="$(get_env EDIT_MODE)"
  : "${mode:=edits}"

  ask_secret "Telegram bot token from @BotFather" token
  [[ $token =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || die "that does not look like a BotFather token"
  ask_secret "Command Code API key from Command Code Studio" apikey
  [[ -n $apikey ]] || echo "No API key set: run 'sudo -u $BOT_USER -H cmd login' instead."

  if [[ -z $userid ]]; then
    read -rp "Find your Telegram user ID now? Message the bot privately first, then press Enter (or type n): " answer
    if [[ $answer != n ]]; then
      systemctl stop "$SERVICE" 2>/dev/null || true
      TELEGRAM_BOT_TOKEN="$token" runuser -u "$BOT_USER" -- \
        env -C "$APP_DIR" "$APP_DIR/.venv/bin/python" -m commandcode_bot.identify || true
    fi
  fi
  ask_plain "Your numeric Telegram user ID" userid
  [[ $userid =~ ^[0-9]+$ ]] || die "the Telegram user ID must be a number"
  ask_plain "Default model (blank = account default, e.g. deepseek/deepseek-v4.1-flash)" model
  [[ -z $model || $model =~ ^[A-Za-z0-9._/:-]{1,100}$ ]] || die "invalid model id"
  ask_plain "Default effort (blank, low, medium, high, xhigh, max)" effort
  [[ -z $effort || $effort =~ ^(low|medium|high|xhigh|max)$ ]] || die "invalid effort level"
  ask_plain "Edit mode for /do (edits = file edits only; full = edits plus shell, tests and commits)" mode
  [[ $mode == edits || $mode == full ]] || die "edit mode must be edits or full"

  cmdbin="$(command -v cmd || command -v commandcode || command -v cmdc || true)"
  [[ -n $cmdbin ]] || die "Command Code CLI not found; re-run install.sh"

  tmp="$(mktemp "$CONF_DIR/.env.XXXXXX")"
  chmod 600 "$tmp"
  {
    echo "TELEGRAM_BOT_TOKEN=$token"
    echo "TELEGRAM_USER_ID=$userid"
    echo "PROJECTS_FILE=$PROJECTS"
    echo "COMMAND_CODE_BIN=$cmdbin"
    [[ -n $apikey ]] && echo "COMMAND_CODE_API_KEY=$apikey"
    echo "STATE_FILE=/var/lib/commandcode-bot/state.json"
    echo "WORKTREE_ROOT=$BOT_HOME/worktrees"
    echo "RUN_TIMEOUT_SECONDS=$(get_env RUN_TIMEOUT_SECONDS | grep -E '^[0-9]+$' || echo 900)"
    echo "EDIT_TIMEOUT_SECONDS=$(get_env EDIT_TIMEOUT_SECONDS | grep -E '^[0-9]+$' || echo 2700)"
    echo "DEFAULT_MODEL=$model"
    echo "DEFAULT_EFFORT=$effort"
    echo "EDIT_MODE=$mode"
  } > "$tmp"
  chown root:root "$tmp"
  mv -f "$tmp" "$ENV_FILE"
  echo "Saved $ENV_FILE (root-only, mode 600)."
  restart_if_ready
}

ensure_github_host_key() {
  local known=$BOT_HOME/.ssh/known_hosts scanned existing
  if [[ -f $known ]] && existing="$(ssh-keygen -F github.com -f "$known" | grep -v '^#')" && [[ -n $existing ]]; then
    # Never trust an existing entry blindly: it must be GitHub's published key.
    ssh-keygen -lf - <<< "$existing" | awk '{print $2}' | grep -qxF "$GITHUB_ED25519_FP" ||
      die "known_hosts has a github.com key that is not GitHub's published ed25519 key; remove it and retry"
    return 0
  fi
  scanned="$(ssh-keyscan -t ed25519 github.com 2>/dev/null)"
  [[ "$(ssh-keygen -lf - <<< "$scanned" | awk '{print $2}')" == "$GITHUB_ED25519_FP" ]] ||
    die "github.com host key did not match GitHub's published fingerprint"
  echo "$scanned" >> "$known"
  chown "$BOT_USER:$BOT_USER" "$known"
  chmod 600 "$known"
}

ensure_ssh_alias() {
  local name=$1 key=$2 config=$BOT_HOME/.ssh/config
  touch "$config"
  if ! grep -q "^Host github-$name\$" "$config"; then
    printf '\nHost github-%s\n  HostName github.com\n  User git\n  IdentityFile %s\n  IdentitiesOnly yes\n' \
      "$name" "$key" >> "$config"
  fi
  chown "$BOT_USER:$BOT_USER" "$config"
  chmod 600 "$config"
}

cmd_add_repo() {
  local name=${1:-} url=${2:-} writable=false base="" dest clone_url key head
  [[ -n $name && -n $url ]] || die "usage: add-repo <name> <git-url> [--writable] [--base <branch>]"
  shift 2
  while (( $# )); do
    case $1 in
      --writable) writable=true ;;
      --base)
        [[ -n ${2:-} ]] || die "--base needs a branch name"
        base=$2
        shift
        ;;
      *) die "unknown option: $1" ;;
    esac
    shift
  done
  [[ $name =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "name must match ^[a-z0-9][a-z0-9_-]*$"
  [[ -z $base || ($base =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$ && $base != *..*) ]] || die "invalid base branch: $base"
  [[ $url =~ ^(git@github\.com:|https://|ssh://) ]] || die "use a git@github.com:, https:// or ssh:// URL"
  dest=$BOT_HOME/repos/$name
  clone_url=$url

  if [[ $url =~ ^git@github\.com:(.+)$ ]]; then
    key=$BOT_HOME/.ssh/deploy_$name
    if [[ ! -f $key ]]; then
      runuser -u "$BOT_USER" -- ssh-keygen -q -t ed25519 -N '' -C "ccbot-$name@$(hostname)" -f "$key"
    fi
    ensure_ssh_alias "$name" "$key"
    ensure_github_host_key
    echo
    echo "Add this deploy key to the GitHub repository (Settings -> Deploy keys -> Add deploy key)."
    if [[ $writable == true ]]; then
      echo "Tick 'Allow write access' so /push can push ccbot/* branches."
    fi
    echo
    cat "$key.pub"
    echo
    read -rp "Press Enter once the key is added... " _
    clone_url="git@github-$name:${BASH_REMATCH[1]}"
  fi

  if [[ -d $dest/.git ]]; then
    echo "$dest already exists; reusing it."
  else
    # No terminal prompts: a private https URL fails fast instead of waiting for a username.
    runuser -u "$BOT_USER" -- env GIT_TERMINAL_PROMPT=0 git clone "$clone_url" "$dest"
  fi
  if [[ -z $base ]]; then
    head="$(runuser -u "$BOT_USER" -- git -C "$dest" symbolic-ref --quiet --short refs/remotes/origin/HEAD || true)"
    base=${head#origin/}
    base=${base:-main}
  fi

  python3 - "$PROJECTS" "$name" "$dest" "$writable" "$base" <<'PY'
import json, os, sys
path, name, dest, writable, base = sys.argv[1:]
try:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
except FileNotFoundError:
    data = {}
data[name] = {"path": dest, "writable": writable == "true", "base": base}
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2)
    handle.write("\n")
os.replace(tmp, path)
PY
  chown root:"$BOT_USER" "$PROJECTS"
  chmod 640 "$PROJECTS"
  if [[ $writable == true ]]; then
    echo "Added project '$name' (writable, base $base)."
    echo "Recommended: protect '$base' on GitHub (Settings -> Branches -> require a pull request) so only"
    echo "reviewed ccbot/* branches can ever reach it."
  else
    echo "Added project '$name' (read-only)."
  fi
  restart_if_ready
}

cmd_remove_repo() {
  local name=${1:-}
  [[ -n $name ]] || die "usage: remove-repo <name>"
  [[ -f $PROJECTS ]] || die "no projects configured"
  python3 - "$PROJECTS" "$name" <<'PY'
import json, os, sys
path, name = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    data = json.load(handle)
if data.pop(name, None) is None:
    sys.exit(f"No project named {name!r}.")
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2)
    handle.write("\n")
os.replace(tmp, path)
PY
  chown root:"$BOT_USER" "$PROJECTS"
  chmod 640 "$PROJECTS"
  echo "Removed '$name' from the bot (files in $BOT_HOME/repos/$name are kept)."
  restart_if_ready
}

cmd_status() {
  systemctl --no-pager status "$SERVICE" || true
  echo
  journalctl -u "$SERVICE" -n 30 --no-pager || true
  echo
  if [[ -f $PROJECTS ]]; then
    python3 - "$PROJECTS" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    projects = json.load(handle)
for name, entry in projects.items():
    entry = entry if isinstance(entry, dict) else {"path": entry}
    mode = "writable, base " + entry.get("base", "main") if entry.get("writable") else "read-only"
    print(f"{name}: {entry['path']} ({mode})")
PY
  else
    echo "No projects configured."
  fi
}

case ${1:-secrets} in
  secrets) cmd_secrets ;;
  add-repo) shift; cmd_add_repo "$@" ;;
  remove-repo) shift; cmd_remove_repo "$@" ;;
  status) cmd_status ;;
  *) die "unknown command: $1 (secrets | add-repo | remove-repo | status)" ;;
esac
