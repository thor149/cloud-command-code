# Brief for Codex — make Telegram messages structured and readable

Live feedback from the owner: replies are "very unstructured". Observed in Telegram:
- Command Code's answers are Markdown, but the Worker sends plain text, so `**bold**`, backticks and lists show raw.
- "Started: https://github.com/.../actions/runs/…" produces a big GitHub link preview card.
- Queued / Started / Done arrive as three separate messages.

## Files you own
`worker/**` (src, tests, schema + a new migration file), and `.github/workflows/task.yml` (only for item 5).
Do not commit, push or deploy. Keep all existing tests passing; add tests for everything below.

## Changes
1. **Markdown → Telegram HTML** (`parse_mode: "HTML"`), new pure module `worker/src/format.js`:
   escape `& < >` first, then convert fenced code blocks → `<pre><code>…</code></pre>` (keep language out),
   inline code → `<code>`, `**x**`/`__x__` → `<b>`, `*x*`/`_x_` → `<i>` (do not touch underscores inside words or
   code), `[t](https://…)` → `<a href>` (http/https only), `#`/`##`/`###` headings → `<b>…</b>`, `- `/`* ` list
   items → `• `, `>` quotes → `<blockquote>`. Never emit unbalanced tags. If Telegram answers 400 for a formatted
   message, resend the same content as plain text (no parse_mode) — never lose a reply.
2. **No link previews**: every send/edit uses `link_preview_options: {"is_disabled": true}`.
3. **One live status message per task**: on queue, send
   `⏳ <b>Queued</b> · /ask · <code>ccbot</code>` + a second line with the prompt (italic, first 200 chars) and
   store its `message_id` on the task (add column via a new `worker/migrations/0002_status_message.sql`
   `ALTER TABLE tasks ADD COLUMN status_message_id INTEGER;` and mirror it in `schema.sql`). On `/started`, **edit**
   that message to `▶️ <b>Running</b> · /ask · <code>ccbot</code> · <a href="run_url">view run</a>`. On result/cancel/timeout,
   edit it to `✅ <b>Done</b> · 2 min` / `❌ <b>Failed</b> (exit 3: not authenticated)` / `⏱ <b>Timed out</b>` /
   `🛑 <b>Cancelled</b>` (+ the run link), then send the **answer as its own message**. If the edit fails, send new.
4. **Result layout**: answer rendered via format.js. If the rendered answer is > 3500 characters, send the first
   ~1500 characters (cut at a paragraph boundary, tags balanced) followed by `<i>…full answer attached</i>`, then
   the full Markdown as `answer.md` via sendDocument. For `do` results add a footer block:
   `🌿 <b>Branch</b> <code>ccbot/…</code>`, `🔀 <b>Pull request</b> <a href=…>#7</a> — paste it to Claude for review`,
   `🛠 <b>N tool calls</b>`. Errors from publishing appear in a separate `⚠️` line, not mixed into the answer.
5. **Faster runs**: in `task.yml`'s agent job, cache npm's download cache for the Command Code install
   (`actions/cache@v4`, path `~/.npm`, key `ccbot-npm-${{ runner.os }}-${{ hashFiles('runner/.github/workflows/task.yml') }}`)
   so `npm i -g command-code@latest` reuses downloaded tarballs. Keep the install itself (always latest).
6. **Tidy `/status`, `/usage`, `/limits`, `/repos`, `/help`**: bold labels, one fact per line, consistent emoji
   (📁 repo, ⚙️ model/effort, 📊 usage, ⏳/▶️ task), times shown as `HH:MM UTC` or `in 3 h`, percentages rounded,
   projections as their own lines. `/help` grouped: Ask & plan · Edit · Control · Limits.

## Done when
`cd worker && node --test test/` passes (including new format.js tests: escaping, nested/unbalanced input,
code blocks containing `*` and `<`, links with bad schemes, long-answer split) and the Python suite still passes
(`.venv\Scripts\python.exe -m unittest discover -s tests`). End your final message with DONE + files changed.
