# Brief for Codex — build the Cloudflare Worker

Read `.agents/serverless-spec.md` first; it is the contract. You build the Worker side (§1 Worker half, §2, §3,
§4). Claude (orchestrator) reviews and deploys; Command Code builds the GitHub Actions side in parallel.

## Files you own (create/edit only these)
`worker/**` — `wrangler.jsonc`, `package.json`, `schema.sql`, `src/*.js`, `test/*.test.js`, `README.md` (short).

## Must not touch
Everything outside `worker/`. Do not commit; do not deploy; do not run wrangler commands that talk to Cloudflare.

## Technical constraints
- Plain JavaScript ES modules, **no runtime npm dependencies**. Workers APIs only: `fetch`, `crypto.subtle`,
  `crypto.randomUUID`, `FormData`, `Blob`, `TextEncoder`, `btoa/atob`.
- Free plan: 10 ms CPU per request. No heavy loops; base64 of session blobs is fine.
- `src/index.js` exports `default { fetch(request, env, ctx), scheduled(event, env, ctx) }`.
- Put logic in pure modules that receive their dependencies (`store`, `telegram`, `github`, `now()`), so tests use
  in-memory fakes. Suggested: `auth.js` (HMAC sign/verify), `commands.js` (Telegram routing), `api.js` (/api/*),
  `limits.js` (windows + projections, pure), `store.js` (D1 + KV implementation), `telegram.js`, `github.js`.
- `schema.sql`: tables `tasks`, `updates`, `settings`, `conversations` (+ indexes). Task ids `t_` + 16 hex chars.
- `wrangler.jsonc`: name `cloud-command-code`, `main: src/index.js`, `compatibility_date: "2026-10-01"`,
  D1 binding `DB` (`database_name: "ccbot-state"`, `database_id: "REPLACE_WITH_D1_ID"`), KV binding `SESSIONS`
  (`id: "REPLACE_WITH_KV_ID"`), `triggers.crons: ["*/15 * * * *"]`, `observability.enabled: true`, vars:
  `OWNER_ID` ("0"), `REPOS` (JSON string, example
  `{"ccbot":{"repo":"thor149/cloud-command-code","base":"main","writable":true,"metered":false,"workflow":"ccbot.yml"}}`),
  `DEFAULT_LIMITS` (`{"hourly":60,"daily":180,"monthly":1800}`), `DEFAULT_MODEL` (""), `DEFAULT_EFFORT` (""),
  `EDIT_MODE` ("edits"). Secrets (document only): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`,
  `GITHUB_TOKEN`, `CCBOT_SECRET`.
- `package.json`: `"type": "module"`, script `"test": "node --test test/"`, no dependencies.

## HMAC test vectors (must match the Python side)
secret `test-secret`, timestamp `1700000000`:
- `GET` `/api/tasks/t_abc`, body empty → `sha256=3c19f8fea134ad6df9b8af38d1d6a080df846afb96c7facb173b31b2c4cf5675`
- `POST` `/api/tasks/t_abc/result`, body `{"ok":true}` → `sha256=dbe4df59d51c3478bec531a4f29d6da1f6b10875582c6f036f96df1599d979e1`

## Tests required (`cd worker && node --test test/`)
HMAC vectors + rejection of bad/stale signatures; webhook secret check; owner/private-chat filter; update_id
dedupe; `/cmd@BotName` parsing; queue rules (busy repo, read-only repo refuses `/do`, prompt length); dispatch
payload shape and failure handling; `/do` branch naming + continuation + `/done`; `/new` epoch so a late result
does not overwrite a fresh conversation; result idempotency and minutes calculation; limits enforcement for
each window, `metered:false` bypass, `/limits` parsing incl. `off`; projection maths (`limits.js`) with fixed
dates; cron timeouts; long result → sendDocument.

## Done when
All tests pass with Node 22, no dependencies are added, and you append a DONE note to the log below listing
files and anything you could not verify. If blocked, append BLOCKED with the reason.

## Log
