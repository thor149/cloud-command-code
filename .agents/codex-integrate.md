# Brief for Codex — integration review + docs (runs after Command Code finishes)

The Worker (`worker/`, yours, already deployed at https://cloud-command-code.rajpriyanshu7214.workers.dev) and the
GitHub Actions runner (`commandcode_bot/task.py`, `commandcode_bot/prompts.py`, `.github/workflows/task.yml`,
`templates/ccbot.yml`, `tests/test_task.py`, by Command Code) were built separately against
`.agents/serverless-spec.md`. Make them work together and make the repo ready to publish.

## 1. Contract check (fix anything that disagrees, on either side)
- Every field, type, status code and header in spec §1 matches between `worker/src/api.js` and `task.py`
  (incl. `exit_code: null` allowed, session base64 of a gzip tar with members relative to `~/.commandcode`,
  410 → quiet exit, run_url format `https://github.com/{repo}/actions/runs/{id}` which the Worker checks exactly).
- HMAC: both sides pass the shared vectors; the Python signer uses the URL path without query string.
- The workflow matches spec §5: two jobs, `agent` has `contents: read` only and no `GITHUB_TOKEN`/`CCBOT_*` in the
  Command Code step's env; `publish` is `if: ${{ !cancelled() }}`; checkouts use `persist-credentials: false`;
  push uses a one-off extraheader; artifact retention 1 day and deleted after publish; `templates/ccbot.yml`
  calls `thor149/cloud-command-code/.github/workflows/task.yml@main` and passes the three secrets.
- `task.py` must not crash when the agent job produced no artifact, when the session is missing, or when the
  patch is empty; publish then reports a clear failed/ok result.
- Run `python -m unittest discover -s tests` (from the repo root, with `.venv\Scripts\python.exe`) and
  `cd worker && node --test test/`. All must pass. If `actionlint` is unavailable, review the YAML by hand
  carefully (expressions, `needs`, outputs, permissions, `secrets:` passing in a reusable workflow).

## 2. Repo polish for open-sourcing
- Add `LICENSE` (MIT, "Copyright (c) 2026 Priyanshu").
- Rewrite `README.md`: lead with the **serverless edition** (what it is, architecture diagram in text, commands
  incl. `/usage` and `/limits`, security model: owner-only, HMAC, two-job runner, deny policy, branch-only edits,
  PR review by the owner; limits: free Actions minutes are unlimited on public repos and monthly on private
  ones; setup: create bot, Command Code API key, fine-grained token with Actions read/write on the target repos,
  `pwsh -File deploy/serverless/setup-secrets.ps1 -WorkerUrl … -Repos …`, copy `templates/ccbot.yml` into each
  target repo's `.github/workflows/ccbot.yml`, add the repo to `REPOS` in `worker/wrangler.jsonc` and
  `wrangler deploy`). Keep the existing VM instructions as a shorter "Self-hosted VM mode" section.
- Update `.github/workflows/ci.yml` to also run the Worker tests (`node --test` in `worker/`, Node 22).
- Copy `templates/ccbot.yml` to `.github/workflows/ccbot.yml` so this repo is its own first target.

## Rules
Do not commit, push or deploy. Do not touch secrets. Append a DONE/BLOCKED note with what you changed to the log
below (if `.agents/` is read-only for you, put the note at the end of your final message instead).

## Log
