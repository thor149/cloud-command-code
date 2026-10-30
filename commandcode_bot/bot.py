import asyncio
import io
import logging
from dataclasses import dataclass

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .config import Config, load_config
from .runner import RunResult, run_headless


LOG = logging.getLogger(__name__)
MAX_PROMPT_CHARS = 4000


class TokenRedactingFormatter(logging.Formatter):
    def __init__(self, token: str) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.token = token

    def format(self, record: logging.LogRecord) -> str:
        return super().format(record).replace(self.token, "[REDACTED_BOT_TOKEN]")


@dataclass
class State:
    active_project: str | None = None
    running_project: str | None = None

    def __post_init__(self) -> None:
        self.run_lock = asyncio.Lock()


def is_authorized(update: Update, user_id: int) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    return bool(user and chat and user.id == user_id and chat.type == "private" and chat.id == user_id)


async def send_result(update: Update, result: RunResult) -> None:
    assert update.message is not None
    if result.timed_out:
        header = "Command Code timed out and was stopped. Its partial output is below."
    elif result.exit_code != 0:
        header = f"Command Code failed (exit {result.exit_code})."
    else:
        header = "Command Code finished."

    output = result.output or "(No output was produced.)"
    if result.truncated:
        output += "\n\n[Output exceeded 64 KiB and was truncated.]"
    if len(output) <= 7000:
        await update.message.reply_text(header)
        for start in range(0, len(output), 3900):
            await update.message.reply_text(output[start:start + 3900])
    else:
        payload = io.BytesIO(output.encode("utf-8"))
        payload.name = "command-code-result.txt"
        await update.message.reply_document(payload, caption=header)


def build_app(config: Config) -> Application:
    state = State()
    app = Application.builder().token(config.token).concurrent_updates(8).build()

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update, config.allowed_user_id):
            return
        assert update.message is not None
        await update.message.reply_text(
            "Cloud Command Code is online (read-only).\n"
            "/projects — list projects\n/use <project> — select one\n"
            "/status — see current state\n/plan <question> — investigate and plan\n"
            "/task <question> — investigate and answer\n"
            "You can also send a plain text question."
        )

    async def projects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update, config.allowed_user_id):
            return
        assert update.message is not None
        await update.message.reply_text("Available projects:\n" + "\n".join(f"• {name}" for name in config.projects))

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
        await update.message.reply_text(f"Active project: {name}")

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update, config.allowed_user_id):
            return
        assert update.message is not None
        activity = f"Running read-only task in {state.running_project}" if state.run_lock.locked() else "Idle"
        await update.message.reply_text(
            f"Bot: online\nProject: {state.active_project or 'none selected'}\nActivity: {activity}"
        )

    async def execute(update: Update, prompt: str, mode: str) -> None:
        if not is_authorized(update, config.allowed_user_id):
            return
        assert update.message is not None
        if not state.active_project:
            await update.message.reply_text("Choose a project first with /use <project>.")
            return
        prompt = prompt.strip()
        if not prompt:
            await update.message.reply_text(f"Usage: /{mode} <question>")
            return
        if len(prompt) > MAX_PROMPT_CHARS:
            await update.message.reply_text("That prompt is too long (maximum 4000 characters).")
            return
        if state.run_lock.locked():
            await update.message.reply_text("A request is already running. Use /status and try again after it finishes.")
            return

        project = state.active_project
        instruction = (
            "Investigate the repository and produce a concrete plan. "
            if mode == "plan" else
            "Investigate the repository and answer the question. "
        )
        full_prompt = instruction + "This run is read-only. Do not edit files or execute commands.\n\n" + prompt
        async with state.run_lock:
            state.running_project = project
            try:
                await update.message.reply_text(f"Running {mode} in {project}. This may take several minutes.")
                result = await run_headless(config.command_bin, config.projects[project], full_prompt, config.timeout_seconds)
                await send_result(update, result)
            finally:
                state.running_project = None

    async def plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await execute(update, " ".join(context.args), "plan")

    async def task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await execute(update, " ".join(context.args), "task")

    async def text_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await execute(update, update.message.text if update.message else "", "task")

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        LOG.error("Telegram handler failed (%s): %s", type(context.error).__name__, context.error)
        if isinstance(update, Update) and is_authorized(update, config.allowed_user_id) and update.message:
            await update.message.reply_text("The bot hit an error. Check the service log and try again.")

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("projects", projects))
    app.add_handler(CommandHandler("use", use_project))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("plan", plan))
    app.add_handler(CommandHandler("task", task))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_task))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    config = load_config()
    logging.basicConfig(level=logging.WARNING, force=True)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(TokenRedactingFormatter(config.token))
    LOG.setLevel(logging.INFO)
    LOG.info("Starting read-only polling bot for %d configured project(s)", len(config.projects))
    build_app(config).run_polling(allowed_updates=["message"], drop_pending_updates=False)


if __name__ == "__main__":
    main()
