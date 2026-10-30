import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.ext import CommandHandler

from commandcode_bot.bot import TokenRedactingFormatter, build_app, is_authorized, send_result
from commandcode_bot.config import Config
from commandcode_bot.runner import MAX_OUTPUT_BYTES, RunResult, command_args, run_headless


class SecurityTests(unittest.TestCase):
    def test_log_formatter_redacts_bot_token(self):
        formatter = TokenRedactingFormatter("123456:secret")
        record = logging.LogRecord("test", logging.ERROR, __file__, 1,
                                   "Request to https://api.telegram.org/bot123456:secret/getMe failed", (), None)
        self.assertNotIn("123456:secret", formatter.format(record))

    def test_only_owner_in_private_chat_is_authorized(self):
        owner = 123456789
        def update(user, chat, kind):
            return SimpleNamespace(effective_user=SimpleNamespace(id=user), effective_chat=SimpleNamespace(id=chat, type=kind))

        self.assertTrue(is_authorized(update(owner, owner, "private"), owner))
        self.assertFalse(is_authorized(update(999, 999, "private"), owner))
        self.assertFalse(is_authorized(update(owner, -1001, "supergroup"), owner))
        self.assertFalse(is_authorized(update(owner, 999, "private"), owner))

    def test_headless_arguments_always_enforce_plan_mode(self):
        prompt = "look at auth; --yolo $(touch pwned)"
        args = command_args("cmd", prompt)
        self.assertEqual(args[args.index("-p") + 1], prompt)
        self.assertEqual(args[args.index("--permission-mode") + 1], "plan")
        self.assertNotIn("--yolo", args)


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_reports_exit_code_and_output(self):
        fake = Path(tempfile.gettempdir())
        process = SimpleNamespace(
            stdout=SimpleNamespace(read=AsyncMock(side_effect=[b"authentication failed", b""])),
            wait=AsyncMock(return_value=3),
            returncode=3,
        )
        with patch("commandcode_bot.runner.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)) as create:
            result = await run_headless("cmd", fake, "inspect", 30)
        self.assertEqual(result.exit_code, 3)
        self.assertIn("authentication failed", result.output)
        self.assertEqual(create.call_args.kwargs["stdin"], asyncio.subprocess.DEVNULL)
        self.assertEqual(create.call_args.kwargs["cwd"], fake)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", create.call_args.kwargs["env"])

    async def test_output_is_bounded_and_labeled(self):
        process = SimpleNamespace(
            stdout=SimpleNamespace(read=AsyncMock(side_effect=[b"x" * (MAX_OUTPUT_BYTES + 10), b""])),
            wait=AsyncMock(return_value=0),
            returncode=0,
        )
        with patch("commandcode_bot.runner.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
            result = await run_headless("cmd", Path(tempfile.gettempdir()), "inspect", 30)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.output), MAX_OUTPUT_BYTES)

    async def test_timeout_stops_the_process(self):
        async def hang(*_args):
            await asyncio.sleep(60)
        process = SimpleNamespace(
            stdout=SimpleNamespace(read=AsyncMock(side_effect=hang)),
            wait=AsyncMock(side_effect=hang),
            returncode=None,
        )
        async def stop(fake):
            self.assertIs(fake, process)
            fake.returncode = -9
        with patch("commandcode_bot.runner.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)), \
             patch("commandcode_bot.runner._stop_process", new=AsyncMock(side_effect=stop)) as stopper:
            result = await run_headless("cmd", Path(tempfile.gettempdir()), "inspect", 0.01)
        stopper.assert_awaited_once()
        self.assertTrue(result.timed_out)
        self.assertEqual(result.exit_code, -9)

    async def test_result_does_not_call_success_on_timeout(self):
        reply = SimpleNamespace(reply_text=AsyncMock(), reply_document=AsyncMock())
        update = SimpleNamespace(message=reply)
        await send_result(update, RunResult(-9, "partial", timed_out=True))
        self.assertIn("timed out", reply.reply_text.call_args_list[0].args[0])
        self.assertEqual(reply.reply_text.call_args_list[1].args[0], "partial")

    async def test_authorized_project_selection_runs_read_only_task(self):
        owner = 123456789
        repo = Path(tempfile.gettempdir())
        app = build_app(Config("123456:testing", owner, {"hirable": repo}, "cmd", 30))
        handlers = {name: handler.callback for handler in app.handlers[0]
                    if isinstance(handler, CommandHandler) for name in handler.commands}
        message = SimpleNamespace(reply_text=AsyncMock(), reply_document=AsyncMock())
        valid = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=owner),
                                effective_chat=SimpleNamespace(id=owner, type="private"))
        intruder = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=999),
                                   effective_chat=SimpleNamespace(id=999, type="private"))
        with patch("commandcode_bot.bot.run_headless", new=AsyncMock(return_value=RunResult(0, "Found the issue."))) as run:
            await handlers["use"](intruder, SimpleNamespace(args=["hirable"]))
            await handlers["task"](intruder, SimpleNamespace(args=["inspect", "auth"]))
            run.assert_not_called()
            await handlers["use"](valid, SimpleNamespace(args=["hirable"]))
            await handlers["task"](valid, SimpleNamespace(args=["inspect", "auth"]))
        run.assert_awaited_once()
        self.assertEqual(run.call_args.args[1], repo)
        self.assertIn("inspect auth", run.call_args.args[2])
        self.assertIn("read-only", run.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
