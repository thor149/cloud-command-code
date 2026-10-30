import asyncio
import json
import logging
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.ext import CommandHandler, MessageHandler

from commandcode_bot import gitops
from commandcode_bot.bot import TokenRedactingFormatter, build_app, is_authorized, send_result
from commandcode_bot.config import Config, Project, optional_effort, optional_model, parse_projects
from commandcode_bot.runner import MAX_OUTPUT_BYTES, RunResult, StreamParser, command_args, run_headless
from commandcode_bot.state import BotState, EditTask, load_state, save_state


OWNER = 123456789

SAMPLE_RUN = "\n".join(json.dumps(line) for line in [
    {"type": "event", "event": {"type": "run_start", "sessionId": "sess-1"}},
    {"type": "event", "event": {"type": "tool_running", "toolCallId": "a", "toolName": "read_file"}},
    {"type": "event", "event": {"type": "tool_running", "toolCallId": "b", "toolName": "grep"}},
    {"type": "event", "event": {"type": "run_end", "result": {"finalText": "from run_end"}}},
    {"type": "result", "subtype": "success", "sessionId": "sess-1", "finalText": "It adds two numbers."},
]) + "\n"


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def make_repo(root: Path, name: str = "repo") -> Path:
    repo = root / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("print('v1')\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def fake_process(chunks, returncode=0):
    return SimpleNamespace(
        stdout=SimpleNamespace(read=AsyncMock(side_effect=[*chunks, b""])),
        wait=AsyncMock(return_value=returncode),
        returncode=returncode,
    )


def owner_update(text=""):
    message = SimpleNamespace(text=text, reply_text=AsyncMock(), reply_document=AsyncMock())
    return SimpleNamespace(message=message, effective_user=SimpleNamespace(id=OWNER),
                           effective_chat=SimpleNamespace(id=OWNER, type="private"))


def replies(update):
    return [call.args[0] for call in update.message.reply_text.call_args_list]


def handlers_of(app):
    found = {}
    for handler in app.handlers[0]:
        if isinstance(handler, CommandHandler):
            for name in handler.commands:
                found[name] = handler.callback
        elif isinstance(handler, MessageHandler):
            found["<text>"] = handler.callback
    return found


class SecurityTests(unittest.TestCase):
    def test_log_formatter_redacts_bot_token(self):
        formatter = TokenRedactingFormatter("123456:secret")
        record = logging.LogRecord("test", logging.ERROR, __file__, 1,
                                   "Request to https://api.telegram.org/bot123456:secret/getMe failed", (), None)
        self.assertNotIn("123456:secret", formatter.format(record))

    def test_only_owner_in_private_chat_is_authorized(self):
        def update(user, chat, kind):
            return SimpleNamespace(effective_user=SimpleNamespace(id=user), effective_chat=SimpleNamespace(id=chat, type=kind))

        self.assertTrue(is_authorized(update(OWNER, OWNER, "private"), OWNER))
        self.assertFalse(is_authorized(update(999, 999, "private"), OWNER))
        self.assertFalse(is_authorized(update(OWNER, -1001, "supergroup"), OWNER))
        self.assertFalse(is_authorized(update(OWNER, 999, "private"), OWNER))

    def test_read_only_arguments_always_enforce_plan_mode(self):
        prompt = "look at auth; --yolo $(touch pwned)"
        args = command_args("cmd", prompt)
        self.assertEqual(args[args.index("-p") + 1], prompt)
        self.assertEqual(args[args.index("--permission-mode") + 1], "plan")
        self.assertNotIn("--yolo", args)
        self.assertNotIn("--tools-all", args)
        self.assertEqual(args.count(prompt), 1)

    def test_edit_arguments_follow_configured_permission_only(self):
        auto = command_args("cmd", "fix it", edit=True)
        self.assertEqual(auto[auto.index("--permission-mode") + 1], "auto-accept")
        self.assertIn("--tools-all", auto)
        self.assertNotIn("--yolo", auto)
        yolo = command_args("cmd", "fix it", edit=True, edit_permission="yolo")
        self.assertIn("--yolo", yolo)
        with self.assertRaises(ValueError):
            command_args("cmd", "x", edit=True, edit_permission="anything")

    def test_session_model_and_effort_flags(self):
        args = command_args("cmd", "q", session_id="abc", model="deepseek/deepseek-v4.1-flash", effort="max")
        self.assertEqual(args[args.index("--session") + 1], "abc")
        self.assertEqual(args[args.index("--model") + 1], "deepseek/deepseek-v4.1-flash")
        self.assertEqual(args[args.index("--effort") + 1], "max")
        self.assertEqual(args[args.index("--output-format") + 1], "json")


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_string_entry_means_read_only(self):
        projects = parse_projects({"notes": str(self.tmp)})
        self.assertFalse(projects["notes"].writable)
        self.assertEqual(projects["notes"].base, "main")

    def test_writable_project_must_be_a_git_checkout(self):
        with self.assertRaises(ValueError):
            parse_projects({"app": {"path": str(self.tmp), "writable": True}})
        repo = make_repo(self.tmp)
        projects = parse_projects({"app": {"path": str(repo), "writable": True, "base": "main"}})
        self.assertTrue(projects["app"].writable)

    def test_rejects_bad_names_paths_and_branches(self):
        for raw in ({"Bad Name": str(self.tmp)}, {"ok": "relative/path"}, {"ok": str(self.tmp / "missing")},
                    {"ok": {"path": str(self.tmp), "base": "../evil"}}, {"ok": {"path": str(self.tmp), "writable": "yes"}}, {}):
            with self.assertRaises(ValueError, msg=raw):
                parse_projects(raw)

    def test_model_and_effort_validation(self):
        self.assertEqual(optional_model(" deepseek/deepseek-v4.1-flash "), "deepseek/deepseek-v4.1-flash")
        self.assertIsNone(optional_model(""))
        self.assertEqual(optional_effort("MAX"), "max")
        with self.assertRaises(ValueError):
            optional_model("bad model; rm -rf /")
        with self.assertRaises(ValueError):
            optional_effort("ultra")


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_round_trip_survives_restart(self):
        path = self.tmp / "state" / "state.json"
        state = BotState(active_project="app", model="m", effort="max")
        state.project("app").ask_session = "s1"
        state.project("app").task = EditTask("ccbot/app-1", "/w/app-1", "origin/main", "s2", "now")
        save_state(path, state)
        loaded = load_state(path, {"app"})
        self.assertEqual(loaded.active_project, "app")
        self.assertEqual(loaded.project("app").ask_session, "s1")
        self.assertEqual(loaded.project("app").task.session_id, "s2")
        self.assertEqual(loaded.effort, "max")

    def test_unknown_projects_are_forgotten(self):
        path = self.tmp / "state.json"
        state = BotState(active_project="gone")
        state.project("gone").ask_session = "x"
        save_state(path, state)
        loaded = load_state(path, {"app"})
        self.assertIsNone(loaded.active_project)
        self.assertNotIn("gone", loaded.projects)

    def test_corrupt_file_starts_fresh_and_is_kept_aside(self):
        path = self.tmp / "state.json"
        path.write_text("{not json")
        self.assertIsNone(load_state(path, {"app"}).active_project)
        self.assertTrue((self.tmp / "state.json.corrupt").exists())


class ParserTests(unittest.TestCase):
    def test_reads_session_tools_and_final_text(self):
        parser = StreamParser()
        tools = parser.feed(SAMPLE_RUN.encode())
        parser.finish()
        self.assertEqual(tools, ["read_file", "grep"])
        self.assertEqual(parser.session_id, "sess-1")
        self.assertEqual(parser.tool_calls, 2)
        self.assertEqual(parser.text(), "It adds two numbers.")

    def test_lines_split_across_chunks(self):
        parser = StreamParser()
        data = SAMPLE_RUN.encode()
        for i in range(0, len(data), 7):
            parser.feed(data[i:i + 7])
        parser.finish()
        self.assertEqual(parser.text(), "It adds two numbers.")
        self.assertEqual(parser.tool_calls, 2)

    def test_plain_output_is_kept_when_there_is_no_result(self):
        parser = StreamParser()
        parser.feed(b"Error: not authenticated. Run cmd login.\n")
        parser.finish()
        self.assertIn("not authenticated", parser.text())


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_returns_final_text_and_session(self):
        seen = []

        async def on_tool(name):
            seen.append(name)

        with patch("commandcode_bot.runner.asyncio.create_subprocess_exec",
                   new=AsyncMock(return_value=fake_process([SAMPLE_RUN.encode()]))) as create:
            result = await run_headless("cmd", Path(tempfile.gettempdir()), "inspect", 30, on_tool=on_tool)
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "It adds two numbers.")
        self.assertEqual(result.session_id, "sess-1")
        self.assertEqual(seen, ["read_file", "grep"])
        self.assertEqual(create.call_args.kwargs["stdin"], asyncio.subprocess.DEVNULL)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", create.call_args.kwargs["env"])
        self.assertEqual(create.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")

    async def test_failure_reports_exit_code_and_output(self):
        with patch("commandcode_bot.runner.asyncio.create_subprocess_exec",
                   new=AsyncMock(return_value=fake_process([b"authentication failed\n"], 3))):
            result = await run_headless("cmd", Path(tempfile.gettempdir()), "inspect", 30)
        self.assertEqual(result.exit_code, 3)
        self.assertFalse(result.ok)
        self.assertIn("authentication failed", result.output)

    async def test_output_is_bounded_and_labeled(self):
        huge = json.dumps({"type": "result", "subtype": "success", "finalText": "x" * (MAX_OUTPUT_BYTES + 10)})
        with patch("commandcode_bot.runner.asyncio.create_subprocess_exec",
                   new=AsyncMock(return_value=fake_process([huge.encode()]))):
            result = await run_headless("cmd", Path(tempfile.gettempdir()), "inspect", 30)
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.output.encode()), MAX_OUTPUT_BYTES)

    async def _hanging_run(self, **kwargs):
        async def hang(*_args):
            await asyncio.sleep(60)
        process = SimpleNamespace(stdout=SimpleNamespace(read=AsyncMock(side_effect=hang)),
                                  wait=AsyncMock(side_effect=hang), returncode=None)

        async def stop(fake):
            fake.returncode = -9
        with patch("commandcode_bot.runner.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)), \
             patch("commandcode_bot.runner._stop_process", new=AsyncMock(side_effect=stop)) as stopper:
            result = await run_headless("cmd", Path(tempfile.gettempdir()), "inspect", **kwargs)
        stopper.assert_awaited_once()
        return result

    async def test_timeout_stops_the_process(self):
        result = await self._hanging_run(timeout=0.05)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.exit_code, -9)

    async def test_cancel_event_stops_the_process(self):
        cancel = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, cancel.set)
        result = await self._hanging_run(timeout=30, cancel=cancel)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.timed_out)

    async def test_result_does_not_call_success_on_timeout(self):
        update = owner_update()
        await send_result(update, RunResult(-9, "partial", timed_out=True))
        self.assertIn("timed out", replies(update)[0])
        self.assertEqual(replies(update)[1], "partial")


class GitOpsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = make_repo(self.tmp)

    def test_task_worktree_lifecycle_never_touches_base(self):
        created = gitops.create_task_worktree(self.repo, "main", self.tmp / "wt", "app", "Fix the login bug!")
        self.assertTrue(created.branch.startswith("ccbot/app-"))
        self.assertIn("fix-the-login-bug", created.branch)
        self.assertEqual(created.base_ref, "main")
        (created.path / "app.py").write_text("print('v2')\n")
        self.assertIsNotNone(gitops.commit_all(created.path, "ccbot: fix"))
        self.assertIsNone(gitops.commit_all(created.path, "ccbot: nothing"))
        self.assertIn("1 commit(s)", gitops.summary(created.path, created.base_ref))
        self.assertIn("+print('v2')", gitops.full_diff(created.path, created.base_ref))
        self.assertEqual((self.repo / "app.py").read_text(), "print('v1')\n")
        gitops.remove_task(self.repo, created.path, created.branch)
        self.assertFalse(created.path.exists())
        self.assertNotIn(created.branch, gitops.git(self.repo, "branch"))

    def test_refuses_to_push_or_delete_non_task_branches(self):
        with self.assertRaises(gitops.GitError):
            gitops.push_branch(self.repo, "main", "main")
        with self.assertRaises(gitops.GitError):
            gitops.remove_task(self.repo, self.repo, "main")

    def test_push_and_sync_with_a_real_origin(self):
        origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        git(self.repo, "remote", "add", "origin", str(origin))
        git(self.repo, "push", "-q", "origin", "main")
        created = gitops.create_task_worktree(self.repo, "main", self.tmp / "wt", "app", "add feature")
        self.assertEqual(created.base_ref, "origin/main")
        (created.path / "new.py").write_text("x = 1\n")
        gitops.commit_all(created.path, "ccbot: feature")
        gitops.push_branch(created.path, created.branch, "main")
        self.assertIn(created.branch, subprocess.run(["git", "branch"], cwd=origin, capture_output=True, text=True).stdout)
        self.assertIn("up to date", gitops.sync_checkout(self.repo, "main"))

    def test_compare_urls(self):
        self.assertEqual(gitops.compare_url("git@github-hirable:thor149/hirable.git", "main", "ccbot/x"),
                         "https://github.com/thor149/hirable/compare/main...ccbot/x?expand=1")
        self.assertEqual(gitops.compare_url("https://github.com/thor149/hirable", "main", "ccbot/x"),
                         "https://github.com/thor149/hirable/compare/main...ccbot/x?expand=1")


class BotFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = make_repo(self.tmp)
        self.notes = self.tmp / "notes"
        self.notes.mkdir()
        self.config = Config(
            "123456:testing", OWNER,
            {"app": Project("app", self.repo, True, "main"), "notes": Project("notes", self.notes)},
            "cmd", 30, state_file=self.tmp / "state.json", worktree_root=self.tmp / "wt",
        )
        self.handlers = handlers_of(build_app(self.config))

    async def test_intruders_are_ignored(self):
        message = SimpleNamespace(text="hi", reply_text=AsyncMock(), reply_document=AsyncMock())
        intruder = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=999),
                                   effective_chat=SimpleNamespace(id=999, type="private"))
        with patch("commandcode_bot.bot.run_headless", new=AsyncMock()) as run:
            await self.handlers["use"](intruder, SimpleNamespace(args=["app"]))
            await self.handlers["do"](intruder, SimpleNamespace(args=["delete", "everything"]))
            await self.handlers["<text>"](intruder, SimpleNamespace(args=[]))
        run.assert_not_called()
        message.reply_text.assert_not_called()

    async def test_ask_is_read_only_and_continues_the_conversation(self):
        update = owner_update()
        result = RunResult(0, "Found it.", session_id="sess-9")
        with patch("commandcode_bot.bot.run_headless", new=AsyncMock(return_value=result)) as run:
            await self.handlers["use"](update, SimpleNamespace(args=["notes"]))
            await self.handlers["ask"](update, SimpleNamespace(args=["where", "is", "auth"]))
            await self.handlers["ask"](update, SimpleNamespace(args=["and", "tests?"]))
        first, second = run.call_args_list
        self.assertEqual(first.args[1], self.notes)
        self.assertIn("read-only", first.args[2])
        self.assertFalse(first.kwargs["edit"])
        self.assertIsNone(first.kwargs["session_id"])
        self.assertEqual(second.kwargs["session_id"], "sess-9")
        self.assertIn("Found it.", replies(update))
        restarted = load_state(self.config.state_file, set(self.config.projects))
        self.assertEqual(restarted.active_project, "notes")
        self.assertEqual(restarted.project("notes").ask_session, "sess-9")

    async def test_do_is_refused_on_read_only_projects(self):
        update = owner_update()
        with patch("commandcode_bot.bot.run_headless", new=AsyncMock()) as run:
            await self.handlers["use"](update, SimpleNamespace(args=["notes"]))
            await self.handlers["do"](update, SimpleNamespace(args=["change", "things"]))
        run.assert_not_called()
        self.assertTrue(any("read-only" in text for text in replies(update)))

    async def test_do_works_on_a_task_branch_and_commits_leftovers(self):
        update = owner_update()

        async def agent(_bin, directory, prompt, _timeout, **kwargs):
            self.assertTrue(kwargs["edit"])
            self.assertIn("ccbot/app-", prompt)
            (directory / "app.py").write_text("print('fixed')\n")
            return RunResult(0, "Changed app.py.", session_id="edit-1")

        with patch("commandcode_bot.bot.run_headless", new=AsyncMock(side_effect=agent)) as run:
            await self.handlers["use"](update, SimpleNamespace(args=["app"]))
            await self.handlers["do"](update, SimpleNamespace(args=["fix", "output"]))
            await self.handlers["do"](update, SimpleNamespace(args=["also", "docs"]))
        worktree = run.call_args_list[0].args[1]
        self.assertNotEqual(worktree, self.repo)
        self.assertEqual(run.call_args_list[1].args[1], worktree)
        self.assertEqual(run.call_args_list[1].kwargs["session_id"], "edit-1")
        self.assertEqual((self.repo / "app.py").read_text(), "print('v1')\n")
        self.assertTrue(any("1 commit(s)" in text for text in replies(update)))

        await self.handlers["diff"](update, SimpleNamespace(args=[]))
        update.message.reply_document.assert_awaited()
        await self.handlers["discard"](update, SimpleNamespace(args=[]))
        self.assertFalse(worktree.exists())
        self.assertIsNone(load_state(self.config.state_file, {"app", "notes"}).project("app").task)

    async def test_cancel_signals_the_running_job(self):
        update = owner_update()
        started = asyncio.Event()

        async def slow(*_args, cancel, **_kwargs):
            started.set()
            await cancel.wait()
            return RunResult(-9, "partial", cancelled=True)

        with patch("commandcode_bot.bot.run_headless", new=AsyncMock(side_effect=slow)):
            await self.handlers["use"](update, SimpleNamespace(args=["notes"]))
            job = asyncio.create_task(self.handlers["ask"](update, SimpleNamespace(args=["long", "question"])))
            await asyncio.wait_for(started.wait(), 5)
            await self.handlers["ask"](update, SimpleNamespace(args=["second"]))
            await self.handlers["cancel"](update, SimpleNamespace(args=[]))
            await asyncio.wait_for(job, 5)
        texts = replies(update)
        self.assertTrue(any("already running" in t for t in texts))
        self.assertTrue(any("Stopped by /cancel" in t for t in texts))

    async def test_model_and_effort_are_persisted_and_validated(self):
        update = owner_update()
        await self.handlers["model"](update, SimpleNamespace(args=["deepseek/deepseek-v4.1-flash"]))
        await self.handlers["effort"](update, SimpleNamespace(args=["max"]))
        await self.handlers["effort"](update, SimpleNamespace(args=["ultra"]))
        state = load_state(self.config.state_file, set(self.config.projects))
        self.assertEqual(state.model, "deepseek/deepseek-v4.1-flash")
        self.assertEqual(state.effort, "max")
        self.assertTrue(any("Effort must be one of" in t for t in replies(update)))


if __name__ == "__main__":
    unittest.main()
