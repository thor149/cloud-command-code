import base64
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

from commandcode_bot import gitops, task
from commandcode_bot.runner import RunResult


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


def make_tar(members, links=()) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for name, target in links:
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            archive.addfile(info)
    return buffer.getvalue()


def header(request, name):
    for key, value in request.header_items():
        if key.lower() == name.lower():
            return value
    return None


class SigningTests(unittest.TestCase):
    def test_hmac_vectors(self):
        self.assertEqual(
            task.sign("test-secret", "GET", "/api/tasks/t_abc", "1700000000", b""),
            "sha256=3c19f8fea134ad6df9b8af38d1d6a080df846afb96c7facb173b31b2c4cf5675")
        self.assertEqual(
            task.sign("test-secret", "POST", "/api/tasks/t_abc/result", "1700000000", b'{"ok":true}'),
            "sha256=dbe4df59d51c3478bec531a4f29d6da1f6b10875582c6f036f96df1599d979e1")

    def test_request_is_signed_over_the_exact_body(self):
        captured = {}

        class Response:
            status = 200

            def read(self):
                return b'{"ok":true}'

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            return Response()

        with patch.object(task.time, "time", return_value=1700000000), \
                patch.object(task.urllib.request, "urlopen", side_effect=fake_urlopen):
            status, body = task.api_request("https://api.test/", "test-secret", "POST",
                                            "/api/tasks/t_abc/result", {"ok": True})
        self.assertEqual((status, body), (200, b'{"ok":true}'))
        request = captured["request"]
        self.assertEqual(request.full_url, "https://api.test/api/tasks/t_abc/result")
        self.assertEqual(request.data, b'{"ok":true}')
        self.assertEqual(header(request, "X-Ccbot-Timestamp"), "1700000000")
        self.assertEqual(header(request, "X-Ccbot-Signature"),
                         "sha256=dbe4df59d51c3478bec531a4f29d6da1f6b10875582c6f036f96df1599d979e1")


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_extracts_relative_members(self):
        blob = make_tar({"projects/slug/sess-1.jsonl": b"{}\n", "projects/slug/sess-1.meta.json": b"{}"})
        task.safe_extract(blob, self.tmp / "home")
        self.assertEqual((self.tmp / "home" / "projects" / "slug" / "sess-1.jsonl").read_bytes(), b"{}\n")

    def test_rejects_traversal_absolute_paths_and_links(self):
        cases = [
            make_tar({"../evil": b"x"}),
            make_tar({"projects/../../evil": b"x"}),
            make_tar({"/tmp/evil": b"x"}),
            make_tar({"C:/evil": b"x"}),
            make_tar({}, links=[("projects/link", "/etc/passwd")]),
        ]
        for blob in cases:
            with self.assertRaises(task.TaskError):
                task.safe_extract(blob, self.tmp / "home")
        self.assertFalse((self.tmp / "evil").exists())

    def test_session_archive_contains_only_the_session(self):
        home = self.tmp / "home"
        session = home / "projects" / "slug"
        session.mkdir(parents=True)
        (session / "sess-1.jsonl").write_text("{}\n")
        (session / "sess-1.meta.json").write_text("{}")
        (session / "other.jsonl").write_text("{}\n")
        target = self.tmp / "out" / "session.tar.gz"
        task.write_session_archive("sess-1", target, home=home)
        with tarfile.open(target, "r:gz") as archive:
            self.assertEqual(sorted(archive.getnames()),
                             ["projects/slug/sess-1.jsonl", "projects/slug/sess-1.meta.json"])


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = self.tmp / "ccbot"
        self.home = self.tmp / "home"
        self.outputs = self.tmp / "github-output"
        self.outputs.write_text("")
        self.calls = []
        self.environ = {
            "GITHUB_OUTPUT": str(self.outputs), "GITHUB_RUN_ID": "12345",
            "GITHUB_REPOSITORY": "owner/repo", "GITHUB_SERVER_URL": "https://github.com",
        }

    def fake_urlopen(self, status, body):
        class Response:
            def __init__(self, code, payload):
                self.status = code
                self._payload = payload

            def read(self):
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def call(request, timeout=None):
            method = request.get_method()
            path = urlsplit(request.full_url).path
            data = request.data or b""
            timestamp = header(request, "X-Ccbot-Timestamp")
            self.assertEqual(header(request, "X-Ccbot-Signature"),
                             task.sign("secret", method, path, timestamp, data))
            self.calls.append({"method": method, "path": path,
                               "payload": json.loads(data) if data else None})
            if status == 410:
                raise HTTPError(request.full_url, 410, "Gone", {}, io.BytesIO(b""))
            return Response(204 if method == "POST" else status, b"" if method == "POST" else body)
        return call

    def task_payload(self, **over):
        data = {
            "id": "t_abc", "kind": "do", "repo": "owner/repo", "name": "name",
            "prompt": "Fix the app", "base": "main", "branch": "ccbot/app-1",
            "continue_branch": True, "session_id": None, "session": None,
            "model": None, "effort": None, "edit_mode": "edits", "timeout_minutes": 15,
        }
        data.update(over)
        return data

    def test_writes_task_session_outputs_and_started(self):
        session = make_tar({"projects/slug/sess-1.jsonl": b"{}\n"})
        payload = self.task_payload(session=base64.b64encode(session).decode("ascii"))
        with patch.object(task.urllib.request, "urlopen",
                          side_effect=self.fake_urlopen(200, json.dumps(payload).encode())), \
                patch.dict(os.environ, self.environ):
            fetched = task.fetch_task("https://api.test", "secret", "t_abc", self.out, home=self.home)
        self.assertEqual(fetched["kind"], "do")
        stored = json.loads((self.out / "task.json").read_text())
        self.assertNotIn("session", stored)
        self.assertEqual(stored["branch"], "ccbot/app-1")
        self.assertEqual((self.home / "projects" / "slug" / "sess-1.jsonl").read_bytes(), b"{}\n")
        self.assertEqual([(call["method"], call["path"]) for call in self.calls],
                         [("GET", "/api/tasks/t_abc"), ("POST", "/api/tasks/t_abc/started")])
        self.assertEqual(self.calls[1]["payload"], {
            "run_id": 12345,
            "run_url": "https://github.com/owner/repo/actions/runs/12345",
        })
        self.assertEqual(self.outputs.read_text(), "kind=do\nref=ccbot/app-1\n")

    def test_ref_is_the_base_unless_continuing(self):
        payload = self.task_payload(continue_branch=False)
        with patch.object(task.urllib.request, "urlopen",
                          side_effect=self.fake_urlopen(200, json.dumps(payload).encode())), \
                patch.dict(os.environ, self.environ):
            task.fetch_task("https://api.test", "secret", "t_abc", self.out, home=self.home)
        self.assertIn("ref=main\n", self.outputs.read_text())

    def test_410_writes_cancelled_and_creates_no_task(self):
        with patch.object(task.urllib.request, "urlopen", side_effect=self.fake_urlopen(410, b"")), \
                patch.dict(os.environ, self.environ):
            self.assertIsNone(task.fetch_task("https://api.test", "secret", "t_abc", self.out, home=self.home))
        self.assertEqual(self.outputs.read_text(), "kind=cancelled\n")
        self.assertFalse((self.out / "task.json").exists())


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = make_repo(self.tmp)
        self.out = self.tmp / "out"
        self.home = self.tmp / "home"
        session = self.home / "projects" / "slug"
        session.mkdir(parents=True)
        (session / "sess-1.jsonl").write_text("{}\n")
        self.seen = []

    def do_task(self, **over):
        data = {
            "id": "t_abc", "kind": "do", "repo": "owner/repo", "name": "name",
            "prompt": "Fix the app", "base": "main", "branch": "ccbot/app-1",
            "continue_branch": False, "session_id": None, "model": None, "effort": None,
            "edit_mode": "edits", "timeout_minutes": 15,
        }
        data.update(over)
        return data

    def test_do_commits_and_produces_a_patch(self):
        async def fake_run(binary, directory, prompt, timeout, **kwargs):
            self.seen.append((directory, prompt, timeout, kwargs))
            (Path(directory) / "app.py").write_text("print('v2')\n")
            return RunResult(0, "Changed app.py.", session_id="sess-1", tool_calls=2)

        with patch.object(task, "run_headless", new=fake_run):
            record = task.run_agent(self.do_task(), self.out, self.repo, "cmd", home=self.home)

        self.assertTrue(record["ok"])
        self.assertEqual(record["branch"], "ccbot/app-1")
        self.assertEqual(record["commits"], 1)
        self.assertEqual(record["session_id"], "sess-1")
        self.assertIsNone(record["error"])
        self.assertEqual(gitops.git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "ccbot/app-1")
        self.assertEqual(len(gitops.git(self.repo, "log", "--oneline", "main..HEAD").splitlines()), 1)

        diff = (self.out / "changes.patch").read_bytes()
        self.assertIn(b"diff --git a/app.py b/app.py", diff)
        self.assertIn(b"+print('v2')", diff)
        stored = json.loads((self.out / "result.json").read_text())
        self.assertEqual(stored, record)

        with tarfile.open(self.out / "session.tar.gz", "r:gz") as archive:
            self.assertEqual(archive.getnames(), ["projects/slug/sess-1.jsonl"])

        directory, prompt, timeout, kwargs = self.seen[0]
        self.assertEqual(Path(directory), self.repo)
        self.assertTrue(kwargs["edit"])
        self.assertEqual(kwargs["edit_mode"], "edits")
        self.assertIn("shell commands are not available", prompt)
        self.assertIn("Fix the app", prompt)
        self.assertEqual(timeout, 15 * 60)

    def test_ask_runs_read_only_and_produces_no_patch(self):
        async def fake_run(binary, directory, prompt, timeout, **kwargs):
            self.seen.append((directory, prompt, timeout, kwargs))
            return RunResult(0, "The answer.", session_id="sess-2", tool_calls=1)

        with patch.object(task, "run_headless", new=fake_run):
            record = task.run_agent(self.do_task(kind="ask", prompt="Where is auth?"),
                                    self.out, self.repo, "cmd", home=self.home)

        self.assertTrue(record["ok"])
        self.assertIsNone(record["branch"])
        self.assertEqual(record["commits"], 0)
        self.assertFalse((self.out / "changes.patch").exists())
        self.assertEqual(gitops.git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        _, prompt, _, kwargs = self.seen[0]
        self.assertFalse(kwargs["edit"])
        self.assertIn("read-only", prompt)

    def test_failed_run_is_reported_and_not_committed(self):
        async def fake_run(binary, directory, prompt, timeout, **kwargs):
            (Path(directory) / "app.py").write_text("half-written\n")
            return RunResult(1, "boom", session_id="sess-3")

        with patch.object(task, "run_headless", new=fake_run):
            record = task.run_agent(self.do_task(), self.out, self.repo, "cmd", home=self.home)

        self.assertFalse(record["ok"])
        self.assertEqual(record["commits"], 0)
        self.assertIsNone(record["branch"])
        self.assertIn("exited with code 1", record["error"])
        self.assertEqual((self.out / "changes.patch").read_bytes(), b"")
        self.assertEqual(len(gitops.git(self.repo, "log", "--oneline", "main..HEAD").splitlines()), 0)
        self.assertIn("app.py", gitops.git(self.repo, "status", "--porcelain"))

    def test_agent_cli_survives_a_missing_task_file(self):
        record = task.agent(self.out, self.repo, "cmd", home=self.home)
        self.assertFalse(record["ok"])
        self.assertIn("task.json", record["error"])
        self.assertTrue((self.out / "result.json").is_file())
        self.assertTrue((self.out / "session.tar.gz").is_file())

    @unittest.skipUnless(os.name == "posix", "the fake CLI is a shell script")
    def test_do_with_a_real_fake_cli(self):
        script = self.tmp / "fake-cmd"
        result = json.dumps({"type": "result", "subtype": "success",
                             "sessionId": "sess-real", "finalText": "done"})
        script.write_text("#!/bin/sh\nprintf '%s\\n' '" + result + "'\n"
                          "printf \"print('v3')\\n\" > app.py\n")
        script.chmod(0o755)
        record = task.run_agent(self.do_task(), self.out, self.repo, str(script), home=self.home)
        self.assertTrue(record["ok"], record)
        self.assertIn(b"+print('v3')", (self.out / "changes.patch").read_bytes())


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def do_task(self, **over):
        data = {
            "id": "t_abc", "kind": "do", "repo": "owner/repo", "name": "name",
            "prompt": "Fix the app", "base": "main", "branch": "ccbot/app-1",
            "continue_branch": False, "session_id": None, "model": None, "effort": None,
            "edit_mode": "edits", "timeout_minutes": 15,
        }
        data.update(over)
        return data

    def test_validation_rejects_workflow_changes(self):
        patch = (
            "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
            "index 111..222 100644\n"
            "--- a/.github/workflows/ci.yml\n"
            "+++ b/.github/workflows/ci.yml\n"
            "@@ -1 +1 @@\n-old\n+new\n"
        )
        with self.assertRaises(task.TaskError):
            task.validate_patch(patch.encode())
        with self.assertRaises(task.TaskError):
            task.validate_patch(b"x" * (task.MAX_PATCH_BYTES + 1))
        task.validate_patch(b"diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n")

    def test_missing_artifact_posts_a_failed_result(self):
        posted = []

        def fake_api(base, secret, method, path, payload=None):
            posted.append((method, path, payload))
            return 204, b""

        with patch.object(task, "api_request", new=fake_api):
            result = task.publish("https://api.test", "secret", "t_abc", self.do_task(),
                                  self.tmp / "missing", self.tmp)

        self.assertFalse(result["ok"])
        self.assertIn("result.json", result["error"])
        self.assertIsNone(result["branch"])
        self.assertEqual(posted, [("POST", "/api/tasks/t_abc/result", result)])

    def test_invalid_result_types_post_a_failed_result(self):
        artifact = self.tmp / "artifact"
        artifact.mkdir()
        (artifact / "result.json").write_text(json.dumps({"ok": "yes"}))
        posted = []

        def fake_api(base, secret, method, path, payload=None):
            posted.append(payload)
            return 204, b""

        with patch.object(task, "api_request", new=fake_api):
            result = task.publish("https://api.test", "secret", "t_abc", self.do_task(),
                                  artifact, self.tmp)
        self.assertFalse(result["ok"])
        self.assertIn("invalid", result["error"])
        self.assertEqual(posted, [result])

    def test_applies_pushes_and_opens_a_pr(self):
        origin, _seed = self._origin_with_main()
        work = self.tmp / "work"
        subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
        git(work, "switch", "-q", "-c", "ccbot/app-1")
        (work / "app.py").write_text("print('v2')\n")
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "ccbot: fix")
        diff = subprocess.run(["git", "format-patch", "main..HEAD", "--stdout"], cwd=work,
                              check=True, capture_output=True).stdout

        artifact = self.tmp / "artifact"
        artifact.mkdir()
        session = make_tar({"projects/slug/sess-1.jsonl": b"{}\n"})
        (artifact / "result.json").write_text(json.dumps({
            "ok": True, "exit_code": 0, "timed_out": False, "text": "Changed app.py.",
            "session_id": "sess-1", "tool_calls": 4, "branch": "ccbot/app-1",
            "commits": 1, "error": None,
        }))
        (artifact / "changes.patch").write_bytes(diff)
        (artifact / "session.tar.gz").write_bytes(session)

        publish_repo = self.tmp / "publish"
        subprocess.run(["git", "clone", "-q", str(origin), str(publish_repo)], check=True)

        posted = []

        def fake_api(base, secret, method, path, payload=None):
            posted.append(payload)
            return 204, b""

        real_run = subprocess.run

        def fake_run(args, *rest, **kwargs):
            if args and Path(str(args[0])).name in ("gh", "gh.exe"):
                if list(args[1:3]) == ["pr", "create"]:
                    return subprocess.CompletedProcess(args, 0, "https://github.com/owner/repo/pull/7\n", "")
                return subprocess.CompletedProcess(args, 0, "", "")
            return real_run(args, *rest, **kwargs)

        with patch.object(task, "api_request", new=fake_api), \
                patch.object(task.subprocess, "run", side_effect=fake_run):
            result = task.publish("https://api.test", "secret", "t_abc", self.do_task(),
                                  artifact, publish_repo, token="token", repo="owner/repo")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["pr_url"], "https://github.com/owner/repo/pull/7")
        self.assertEqual(result["branch"], "ccbot/app-1")
        self.assertEqual(result["commits"], 1)
        self.assertEqual(base64.b64decode(result["session"]), session)
        pushed = subprocess.run(["git", "rev-parse", "--verify", "refs/heads/ccbot/app-1"],
                                cwd=origin, capture_output=True, text=True)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(posted, [result])

    def _origin_with_main(self):
        origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        seed = self.tmp / "seed"
        seed.mkdir()
        git(seed, "init", "-q", "-b", "main")
        (seed / "app.py").write_text("print('v1')\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "init")
        git(seed, "remote", "add", "origin", str(origin))
        git(seed, "push", "-q", "origin", "main")
        subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=origin, check=True)
        return origin, seed


if __name__ == "__main__":
    unittest.main()
