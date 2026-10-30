import asyncio
import io
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from . import gitops
from .config import Config, Project, load_config, optional_effort, optional_model
from .runner import EXIT_MEANINGS, RunResult, agent_env, run_headless
from .state import BotState, EditTask, load_state, save_state


LOG = logging.getLogger(__name__)
MAX_PROMPT_CHARS = 4000
HEARTBEAT_SECONDS = 60
CHUNK_CHARS = 3500          # Telegram's 4096 limit counts UTF-16 units; leave room for astral characters
MAX_DIFF_BYTES = 20 * 1024 * 1024

ASK_PREFIX = ("Investigate the repository and answer the question. "
              "This run is read-only. Do not edit files or execute commands.\n\n")
PLAN_PREFIX = ("Investigate the repository and produce a concrete plan. "
               "This run is read-only. Do not edit files or execute commands.\n\n")
EDIT_PREFIX = {
    "edits": ("You are working in a dedicated git worktree on branch {branch}, created from {base}. "
              "In this run you can read and edit files only: shell commands are not available, so do not "
              "try to run tests or git. The bot commits your changes when you finish. Do not modify files "
              "outside this worktree. Finish with a short summary of what you changed and what should be "
              "tested.\n\nTask: "),
    "full": ("You are working in a dedicated git worktree on branch {branch}, created from {base}. "
             "Complete the task below. Run the project's relevant tests or checks if they exist. "
             "Commit your work on this branch with clear messages. Do not push, do not switch or create "
             "branches, do not change git remotes or git config, and do not modify files outside this "
             "worktree. Finish with a short summary of what you changed and how you verified it.\n\nTask: "),
}

HELP = (
    "Cloud Command Code\n\n"
    "/projects — list projects · /use <project> — select one\n"
    "/status — what is running, model, open task\n\n"
    "Read-only (any project):\n"
    "/ask <question> — investigate and answer (plain text works too)\n"
    "/plan <goal> — investigate and plan\n"
    "/new — start a fresh conversation\n"
    "/sync — fast-forward the project checkout\n\n"
    "Edit mode (writable projects):\n"
    "/do <task> — work on a ccbot/* branch; send /do again to continue the same task\n"
    "/diff — the task branch's full diff · /push — push the branch, get a PR link\n"
    "/discard — delete the task branch\n\n"
    "/cancel — stop the running job\n"
    "/model [id|default] · /models · /effort [level|default]"
)


class TokenRedactingFormatter(logging.Formatter):
    def __init__(self, token: str) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.token = token

    def format(self, record: logging.LogRecord) -> str:
        return super().format(record).replace(self.token, "[REDACTED_BOT_TOKEN]")


@dataclass
class Activity:
    label: str
    project: str
    started: float = field(default_factory=time.monotonic)
    tool_calls: int = 0
    last_tool: str | None = None

    def describe(self) -> str:
        minutes = int((time.monotonic() - self.started) // 60)
        last = f", last: {self.last_tool}" if self.last_tool else ""
        return f"{self.label} in {self.project} — {minutes} min, {self.tool_calls} tool calls{last}"


def is_authorized(update: Update, user_id: int) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    return bool(user and chat and user.id == user_id and chat.type == "private" and chat.id == user_id)


async def reply_long(update: Update, text: str, filename: str = "command-code-result.txt", caption: str = "") -> None:
    assert update.message is not None
    if len(text) <= 2 * CHUNK_CHARS:
        if caption:
            await update.message.reply_text(caption)
        for start in range(0, len(text), CHUNK_CHARS):
            await update.message.reply_text(text[start:start + CHUNK_CHARS])
    else:
        payload = io.BytesIO(text.encode("utf-8"))
        payload.name = filename
        await update.message.reply_document(payload, caption=caption[:1000] or None)


def describe_exit(result: RunResult) -> str:
    meaning = EXIT_MEANINGS.get(result.exit_code or -1)
    return f"exit {result.exit_code}" + (f": {meaning}" if meaning else "")


async def send_result(update: Update, result: RunResult) -> None:
    if result.cancelled:
        header = "Stopped by /cancel."
    elif result.timed_out:
        header = "Command Code timed out and was stopped."
    elif result.exit_code != 0:
        header = f"Command Code failed ({describe_exit(result)})."
    else:
        header = "Command Code finished."

    output = result.output
    if not output:
        detail = f" Last tool used: {result.last_tool}." if result.last_tool else ""
        output = f"(No answer was produced.{detail})"
    elif result.cancelled or result.timed_out:
        header += " Whatever it reported before stopping is below."
    if result.truncated:
        output += "\n\n[Output exceeded 64 KiB; only the beginning is shown.]"
    await reply_long(update, output, caption=header)


def build_app(config: Config, state: BotState | None = None) -> Application:
    state = state if state is not None else load_state(config.state_file, set(config.projects))
    run_lock = asyncio.Lock()       # one job (agent run or git-mutating command) at a time
    models_lock = asyncio.Lock()
    current: dict[str, object] = {}  # "activity" and "cancel" while a job runs
    conversation_epoch: dict[str, int] = {}

    app = Application.builder().token(config.token).concurrent_updates(8).build()

    def persist() -> None:
        try:
            save_state(config.state_file, state)
        except OSError as exc:
            LOG.error("Could not save state: %s", exc)

    def effective_model() -> str | None:
        return state.model or config.default_model

    def effective_effort() -> str | None:
        return state.effort or config.default_effort

    async def selected(update: Update) -> Project | None:
        assert update.message is not None
        if not state.active_project or state.active_project not in config.projects:
            await update.message.reply_text("Choose a project first with /use <project>. See /projects.")
            return None
        return config.projects[state.active_project]

    async def clean_prompt(update: Update, prompt: str, usage: str) -> str | None:
        assert update.message is not None
        prompt = prompt.strip()
        if not prompt:
            await update.message.reply_text(f"Usage: {usage}")
            return None
        if len(prompt) > MAX_PROMPT_CHARS:
            await update.message.reply_text(f"That prompt is too long (maximum {MAX_PROMPT_CHARS} characters).")
            return None
        return prompt

    async def acquire(update: Update, label: str, project: str) -> asyncio.Event | None:
        """Take the job lock without waiting. Checking and acquiring happen with no await in between,
        so two handlers can never both pass. Returns the job's cancel event, or None if busy."""
        assert update.message is not None
        if run_lock.locked():
            await update.message.reply_text("A job is already running. Use /status, or /cancel to stop it.")
            return None
        await run_lock.acquire()
        cancel = asyncio.Event()
        current.update(activity=Activity(label, project), cancel=cancel)
        return cancel

    def release() -> None:
        current.clear()
        run_lock.release()

    async def run_locked(update: Update, project: Project, directory: Path, prompt: str, cancel: asyncio.Event,
                         *, edit: bool, session_id: str | None) -> RunResult:
        """Run Command Code; the caller holds the job lock."""
        assert update.message is not None
        activity = current["activity"]
        assert isinstance(activity, Activity)
        note = await update.message.reply_text(f"{activity.label} in {project.name}. I'll update this message as it works.")

        async def on_tool(name: str) -> None:
            activity.tool_calls += 1
            activity.last_tool = name

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                try:
                    await note.edit_text("Still working: " + activity.describe())
                except Exception:
                    pass

        ticker = asyncio.create_task(heartbeat())
        try:
            result = await run_headless(
                config.command_bin, directory, prompt,
                config.edit_timeout_seconds if edit else config.timeout_seconds,
                edit=edit, edit_mode=config.edit_mode, session_id=session_id,
                model=effective_model(), effort=effective_effort(), cancel=cancel, on_tool=on_tool,
            )
        finally:
            ticker.cancel()
        try:
            await note.edit_text("Done: " + activity.describe())
        except Exception:
            pass
        return result

    # ---------- basic commands ----------

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update, config.allowed_user_id):
            return
        assert update.message is not None
        await update.message.reply_text(HELP)

    async def projects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update, config.allowed_user_id):
            return
        assert update.message is not None
        lines = []
        for name, project in config.projects.items():
            mode = f"writable, base {project.base}" if project.writable else "read-only"
            marker = " ← active" if name == state.active_project else ""
            lines.append(f"• {name} ({mode}){marker}")
        await update.message.reply_text("Projects:\n" + "\n".join(lines))

    async def use_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update, config.allowed_user_id):
            return
        assert update.message is not None
        if len(context.args) != 1:
            await update.message.reply_text("Usage: /use <project>. See /projects.")
            return
        name = context.args[0].lower()
        if name not in config.projects:
            await update.message.reply_text("Unknown project. See /projects.")
            return
        state.active_project = name
        persist()
        task = state.project(name).task
        extra = f"\nOpen edit task: {task.branch} (/do to continue, /discard to drop)" if task else ""
        await update.message.reply_text(f"Active project: {name}{extra}")

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update, config.allowed_user_id):
            return
        assert update.message is not None
        activity = current.get("activity")
        lines = [
            "Bot: online",
            f"Project: {state.active_project or 'none selected'}",
            f"Activity: {activity.describe() if isinstance(activity, Activity) else 'idle'}",
            f"Model: {effective_model() or 'account default'} · Effort: {effective_effort() or 'model default'}",
            f"Edit mode: {config.edit_mode}",
        ]
        if state.active_project:
            pstate = state.project(state.active_project)
            lines.append(f"Conversation: {'continuing' if pstate.ask_session else 'new'}")
            if pstate.task:
                lines.append(f"Open task: {pstate.task.branch}")
        await update.message.reply_text("\n".join(lines))

    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update, config.allowed_user_id):
            return
        assert update.message is not None
        event = current.get("cancel")
        if isinstance(event, asyncio.Event):
            event.set()
            await update.message.reply_text("Stopping the running job…")
        else:
            await update.message.reply_text("Nothing is running.")

    async def new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update, config.allowed_user_id):
            return
        project = await selected(update)
        if project is None:
            return
        assert update.message is not None
        pstate = state.project(project.name)
        pstate.ask_session = None
        conversation_epoch[project.name] = conversation_epoch.get(project.name, 0) + 1
        persist()
        extra = f"\nThe edit task on {pstate.task.branch} is still open (/discard to drop it)." if pstate.task else ""
        await update.message.reply_text(f"Started a fresh conversation for {project.name}.{extra}")

    # ---------- read-only runs ----------

    async def ask_or_plan(update: Update, prompt: str, mode: str) -> None:
        if not is_authorized(update, config.allowed_user_id):
            return
        project = await selected(update)
        if project is None:
            return
        prompt = await clean_prompt(update, prompt, f"/{mode} <question>")
        if prompt is None:
            return
        cancel_event = await acquire(update, "Planning" if mode == "plan" else "Investigating", project.name)
        if cancel_event is None:
            return
        try:
            pstate = state.project(project.name)
            epoch = conversation_epoch.get(project.name, 0)
            prefix = PLAN_PREFIX if mode == "plan" else ASK_PREFIX
            result = await run_locked(update, project, project.path, prefix + prompt, cancel_event,
                                      edit=False, session_id=pstate.ask_session)
            if conversation_epoch.get(project.name, 0) == epoch:  # /new during the run wins
                if result.ok and result.session_id:
                    pstate.ask_session = result.session_id
                elif result.exit_code not in (0, None) and not (result.timed_out or result.cancelled):
                    pstate.ask_session = None  # a broken resume should not poison every later question
                persist()
        finally:
            release()
        await send_result(update, result)

    async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await ask_or_plan(update, " ".join(context.args), "ask")

    async def plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await ask_or_plan(update, " ".join(context.args), "plan")

    async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await ask_or_plan(update, update.message.text if update.message else "", "ask")

    async def sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update, config.allowed_user_id):
            return
        project = await selected(update)
        if project is None:
            return
        assert update.message is not None
        if not (project.path / ".git").exists():
            await update.message.reply_text(f"{project.name} is not a git checkout.")
            return
        if await acquire(update, "Syncing", project.name) is None:
            return
        try:
            message = await asyncio.to_thread(gitops.sync_checkout, project.path, project.base)
        except gitops.GitError as exc:
            message = f"Sync failed: {exc}"
        finally:
            release()
        await update.message.reply_text(message)

    # ---------- edit tasks ----------

    async def open_task(update: Update) -> tuple[Project, EditTask] | None:
        project = await selected(update)
        if project is None:
            return None
        assert update.message is not None
        task = state.project(project.name).task
        if task is None:
            await update.message.reply_text("No open edit task. Start one with /do <task>.")
            return None
        return project, task

    async def do(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update, config.allowed_user_id):
            return
        project = await selected(update)
        if project is None:
            return
        assert update.message is not None
        if not project.writable:
            await update.message.reply_text(f"{project.name} is read-only. Mark it writable in projects.json to use /do.")
            return
        prompt = await clean_prompt(update, " ".join(context.args), "/do <task>")
        if prompt is None:
            return
        cancel_event = await acquire(update, "Working", project.name)
        if cancel_event is None:
            return

        summary_text = None
        try:
            pstate = state.project(project.name)  # read under the lock
            task = pstate.task
            if task is not None and not Path(task.worktree).exists():
                task = pstate.task = None
            if task is None:
                try:
                    created = await asyncio.to_thread(gitops.create_task_worktree, project.path, project.base,
                                                      config.worktree_root, project.name, prompt)
                except (gitops.GitError, OSError) as exc:
                    await update.message.reply_text(f"Could not create a task branch: {exc}")
                    return
                task = pstate.task = EditTask(created.branch, str(created.path), created.base_ref,
                                              created=datetime.now(timezone.utc).isoformat(timespec="seconds"))
                persist()
                await update.message.reply_text(f"New task branch {task.branch} from {task.base}.")
            if cancel_event.is_set():
                await update.message.reply_text("Cancelled before Command Code started.")
                return

            worktree = Path(task.worktree)
            try:
                await asyncio.to_thread(gitops.prepare_agent_settings, worktree, config.edit_mode)
            except (gitops.GitError, OSError) as exc:
                await update.message.reply_text(f"Could not prepare the worktree: {exc}")
                return
            result = await run_locked(update, project, worktree,
                                      EDIT_PREFIX[config.edit_mode].format(branch=task.branch, base=task.base) + prompt,
                                      cancel_event, edit=True, session_id=task.session_id)
            if result.session_id:
                task.session_id = result.session_id
            persist()

            try:
                if result.ok:
                    first_line = prompt.splitlines()[0][:120]
                    await asyncio.to_thread(gitops.commit_all, worktree, f"ccbot: {first_line}")
                    note = ""
                else:
                    note = ("\nThe run did not finish cleanly, so nothing was committed automatically. "
                            "Its edits, if any, are still in the worktree: /do to continue, /discard to drop.")
                changes = await asyncio.to_thread(gitops.summary, worktree, task.base)
            except gitops.GitError as exc:
                changes, note = f"(Could not read the task branch: {exc})", ""
            summary_text = (f"Branch {task.branch}\n{changes}{note}\n\n"
                            "Next: /do <follow-up> · /diff · /push · /discard")
        finally:
            release()
        await send_result(update, result)
        await reply_long(update, summary_text, filename="task-summary.txt")

    async def diff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update, config.allowed_user_id):
            return
        found = await open_task(update)
        if found is None:
            return
        project, task = found
        assert update.message is not None
        if await acquire(update, "Reading diff", project.name) is None:
            return
        try:
            patch = await asyncio.to_thread(gitops.full_diff, Path(task.worktree), task.base)
        except gitops.GitError as exc:
            await update.message.reply_text(f"Could not read the diff: {exc}")
            return
        finally:
            release()
        if not patch:
            await update.message.reply_text("The task branch has no committed changes yet.")
            return
        data = patch.encode("utf-8")
        caption = f"Diff of {task.branch} against {task.base}"
        if len(data) > MAX_DIFF_BYTES:
            data = data[:MAX_DIFF_BYTES]
            caption += " (truncated to 20 MB)"
        payload = io.BytesIO(data)
        payload.name = f"{task.branch.replace('/', '_')}.diff"
        await update.message.reply_document(payload, caption=caption)

    async def push(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update, config.allowed_user_id):
            return
        found = await open_task(update)
        if found is None:
            return
        project, task = found
        assert update.message is not None
        if await acquire(update, "Pushing", project.name) is None:
            return
        try:
            link = await asyncio.to_thread(gitops.push_branch, Path(task.worktree), task.branch, project.base)
        except gitops.GitError as exc:
            await update.message.reply_text(f"Push failed: {exc}")
            return
        finally:
            release()
        await update.message.reply_text(f"Pushed {task.branch}.\nOpen a pull request: {link}")

    async def discard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update, config.allowed_user_id):
            return
        found = await open_task(update)
        if found is None:
            return
        project, task = found
        assert update.message is not None
        if await acquire(update, "Discarding", project.name) is None:
            return
        try:
            await asyncio.to_thread(gitops.remove_task, project.path, Path(task.worktree), task.branch)
            state.project(project.name).task = None
            persist()
        except gitops.GitError as exc:
            await update.message.reply_text(f"Could not remove the task: {exc}")
            return
        finally:
            release()
        await update.message.reply_text(f"Discarded {task.branch}. The base branch was never touched.")

    # ---------- model settings ----------

    async def model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update, config.allowed_user_id):
            return
        assert update.message is not None
        if not context.args:
            await update.message.reply_text(f"Model: {effective_model() or 'account default'}. "
                                            "Set with /model <id>, reset with /model default. See /models.")
            return
        value = context.args[0]
        try:
            state.model = None if value.lower() == "default" else optional_model(value)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        persist()
        await update.message.reply_text(f"Model set to {effective_model() or 'account default'}.")

    async def effort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update, config.allowed_user_id):
            return
        assert update.message is not None
        if not context.args:
            await update.message.reply_text(f"Effort: {effective_effort() or 'model default'}. "
                                            "Set with /effort low|medium|high|xhigh|max, reset with /effort default.")
            return
        value = context.args[0]
        try:
            state.effort = None if value.lower() == "default" else optional_effort(value)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        persist()
        await update.message.reply_text(f"Effort set to {effective_effort() or 'model default'}.")

    async def models(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update, config.allowed_user_id):
            return
        assert update.message is not None
        if models_lock.locked():
            await update.message.reply_text("Already listing models…")
            return
        async with models_lock:
            try:
                process = await asyncio.create_subprocess_exec(
                    config.command_bin, "--list-models", stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=agent_env())
            except OSError as exc:
                await update.message.reply_text(f"Could not list models: {exc}")
                return
            try:
                output, _ = await asyncio.wait_for(process.communicate(), timeout=60)
            except asyncio.TimeoutError:
                await update.message.reply_text("Listing models timed out.")
                return
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
        await reply_long(update, output.decode("utf-8", errors="replace").strip() or "(no output)",
                         filename="models.txt")

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        LOG.error("Telegram handler failed (%s): %s", type(context.error).__name__, context.error)
        if isinstance(update, Update) and is_authorized(update, config.allowed_user_id) and update.message:
            await update.message.reply_text("The bot hit an error. Check the service log and try again.")

    commands = {
        "start": start, "help": start, "projects": projects, "use": use_project, "status": status,
        "ask": ask, "task": ask, "plan": plan, "new": new, "sync": sync, "cancel": cancel,
        "do": do, "diff": diff, "push": push, "discard": discard,
        "model": model, "models": models, "effort": effort,
    }
    for name, callback in commands.items():
        app.add_handler(CommandHandler(name, callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    config = load_config()
    logging.basicConfig(level=logging.WARNING, force=True)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(TokenRedactingFormatter(config.token))
    LOG.setLevel(logging.INFO)
    writable = sum(project.writable for project in config.projects.values())
    LOG.info("Starting polling bot: %d project(s), %d writable, edit mode %s",
             len(config.projects), writable, config.edit_mode)
    build_app(config).run_polling(allowed_updates=["message"], drop_pending_updates=False)


if __name__ == "__main__":
    main()
