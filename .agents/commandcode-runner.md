# Brief for Command Code — build the GitHub Actions runner

Read `.agents/serverless-spec.md` first; it is the contract. You build the GitHub Actions side (§1 job half,
§5). Claude (orchestrator) reviews and deploys; Codex builds the Cloudflare Worker in parallel.

## Files you own (create/edit only these)
- `commandcode_bot/task.py` (new) — CLI: `python3 -m commandcode_bot.task fetch|agent|publish`
- `commandcode_bot/prompts.py` (new) — move `ASK_PREFIX`, `PLAN_PREFIX`, `EDIT_PREFIX` here from `bot.py`
- `commandcode_bot/bot.py` — only change it to import those three names from `prompts.py`
- `tests/test_task.py` (new)
- `.github/workflows/task.yml` (new, reusable workflow), `templates/ccbot.yml` (new, caller template)

## Must not touch
`worker/**`, other `commandcode_bot/*.py` files, `tests/test_controller.py`, `deploy/**`, `README.md`,
`.agents/**` (except appending to the log below). Do not commit or push.

## Requirements
- Standard library only in `task.py` (runs on a fresh `ubuntu-latest` with Python 3.12; no pip install).
- Reuse `runner.run_headless` / `runner.command_args` / `runner.StreamParser` (edit runs: `edit=True`,
  `edit_mode` from the task), and `gitops.prepare_agent_settings` + `gitops.commit_all`.
- HMAC exactly as spec §1. Test vectors (secret `test-secret`, timestamp `1700000000`):
  `GET /api/tasks/t_abc` (empty body) → `sha256=3c19f8fea134ad6df9b8af38d1d6a080df846afb96c7facb173b31b2c4cf5675`;
  `POST /api/tasks/t_abc/result` body `{"ok":true}` →
  `sha256=dbe4df59d51c3478bec531a4f29d6da1f6b10875582c6f036f96df1599d979e1`.
- `fetch`: env `CCBOT_API`, `CCBOT_SECRET`, `CCBOT_TASK_ID`; writes `task.json` (without the session blob) into
  the dir given by `--out` (default `$RUNNER_TEMP/ccbot`); safely extracts the session tarball into
  `~/.commandcode` (reject absolute paths and `..`; use `tarfile` data filter); posts `/started` with
  `GITHUB_RUN_ID` and the run URL (`$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID`); writes
  `ref` and `kind` to `$GITHUB_OUTPUT`; on 410 writes `kind=cancelled` and exits 0.
- `agent`: reads `task.json`, runs in the current directory (the target checkout). `do`: if not
  `continue_branch`, `git switch -c <branch>`; record start SHA; `prepare_agent_settings`; run with the edit
  prefix from `prompts.py`; on an ok result `commit_all`, then `git format-patch <start>..HEAD --stdout >
  changes.patch`. `ask`/`plan`: read-only run with the matching prefix. Always write `result.json`
  (spec §1 result fields except session/pr_url) and `session.tar.gz` (members `projects/*/<session_id>.*`
  relative to `~/.commandcode`) into `--out`. Timeout from `timeout_minutes`.
- `publish`: re-fetches the task (trusted), reads the artifact dir (treat contents as untrusted: validate
  types/sizes), applies/validates/pushes/opens the PR as in spec §5 (use `git` and `gh` via subprocess; the
  push uses a one-off `-c http.extraheader=...` built from `GITHUB_TOKEN`, never written to git config), posts
  `/result` (base64 session, pr_url), then deletes the run's artifacts (`gh api`). Missing artifact →
  failed result with a clear error.
- Workflows exactly as spec §5. Pin actions to major versions (`actions/checkout@v4`, `actions/setup-node@v4`,
  `actions/upload-artifact@v4`, `actions/download-artifact@v4`). Secrets reach only the steps that need them.
- Tests (`python -m unittest discover -s tests` must pass on Windows and Linux): HMAC vectors; safe tar
  extraction rejects traversal; fetch writes outputs and handles 410 (fake HTTP server or monkeypatched
  urlopen); agent on a temp git repo with a fake `cmd` (skip POSIX-only parts on Windows) produces a patch for
  `do` and none for `ask`; patch validation rejects `.github/workflows/` changes; publish posts a failed result
  when the artifact is missing. Existing tests must still pass.

## Done when
All tests pass, and you append a DONE note to the log below listing files and anything unverified.
If blocked, append BLOCKED with the reason.

## Log
