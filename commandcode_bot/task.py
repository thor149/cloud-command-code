"""GitHub Actions half of Cloud Command Code: fetch a task, run it, publish the result.

Standard library only: this module runs on a fresh ``ubuntu-latest`` runner where nothing is
installed with pip. It is split into three CLI commands (see :func:`main`):

``fetch``    download a task, restore its session, announce the run, expose step outputs
``agent``    run Command Code in the target checkout and collect the untrusted artifact
``publish``  re-fetch the task, apply and push the patch, open a PR, post the result
"""

import argparse
import asyncio
import base64
import binascii
import hashlib
import hmac
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

from . import gitops
from .prompts import ASK_PREFIX, EDIT_PREFIX, PLAN_PREFIX
from .runner import EXIT_MEANINGS, RunResult, run_headless


MAX_TEXT_CHARS = 60_000
MAX_SESSION_BYTES = 20 * 1024 * 1024
MAX_SESSION_UNPACKED = 200 * 1024 * 1024
MAX_PATCH_BYTES = 5 * 1024 * 1024
DEFAULT_TIMEOUT_MINUTES = 15
HTTP_TIMEOUT = 60

BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class TaskError(RuntimeError):
    pass


# --------------------------------------------------------------------------- signing and HTTP


def sign(secret: str, method: str, path: str, timestamp: str, body: bytes) -> str:
    """The ``X-Ccbot-Signature`` value from spec §1."""
    body_hash = hashlib.sha256(body).hexdigest()
    message = f"{method}\n{path}\n{timestamp}\n{body_hash}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def api_request(base: str, secret: str, method: str, path: str,
                payload: object | None = None) -> tuple[int, bytes]:
    """Call the Worker API with the spec's HMAC headers. Returns ``(status, body)``."""
    body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))
    request = urllib.request.Request(base.rstrip("/") + path, data=body if payload is not None else None,
                                     method=method)
    request.add_header("X-Ccbot-Timestamp", timestamp)
    request.add_header("X-Ccbot-Signature", sign(secret, method, path, timestamp, body))
    request.add_header("User-Agent", "cloud-command-code")
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        raise TaskError(f"{method} {path} failed: {exc}") from exc


def set_output(name: str, value: str) -> None:
    """Append a single-line step output for the GitHub Actions runner."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        if "\n" in value or "\r" in value:
            raise TaskError(f"Step output {name} contains a newline.")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def default_out() -> Path:
    runner_temp = os.environ.get("RUNNER_TEMP")
    return Path(runner_temp) / "ccbot" if runner_temp else Path(tempfile.gettempdir()) / "ccbot"


def commandcode_home() -> Path:
    return Path.home() / ".commandcode"


def command_executable() -> str:
    override = os.environ.get("COMMAND_CODE_BIN")
    if override:
        return override
    for name in ("cmd", "commandcode", "cmdc"):
        found = shutil.which(name)
        if found:
            return found
    return "cmd"


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise TaskError(f"{name} is not set.")
    return value


# --------------------------------------------------------------------------- archives


def _check_tar_members(archive: tarfile.TarFile) -> None:
    total = 0
    for member in archive.getmembers():
        name = member.name.replace("\\", "/")
        parts = PurePosixPath(name).parts
        if (not name or name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in parts
                or "\x00" in name):
            raise TaskError(f"Unsafe path in session archive: {member.name!r}")
        if member.issym() or member.islnk() or member.isdev():
            raise TaskError(f"Links and devices are not allowed in a session archive: {member.name!r}")
        total += max(member.size, 0)
    if total > MAX_SESSION_UNPACKED:
        raise TaskError(f"Session archive expands to more than {MAX_SESSION_UNPACKED} bytes.")


def safe_extract(blob: bytes, destination: Path) -> None:
    """Extract a session tarball, refusing absolute paths, ``..`` and links."""
    if len(blob) > MAX_SESSION_BYTES:
        raise TaskError(f"Session archive is larger than {MAX_SESSION_BYTES} bytes.")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
            _check_tar_members(archive)
            archive.extractall(destination, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise TaskError(f"Invalid session archive: {exc}") from exc


def valid_session_archive(blob: bytes) -> bool:
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
            _check_tar_members(archive)
            return bool(archive.getmembers())
    except (tarfile.TarError, OSError, TaskError):
        return False


def write_session_archive(session_id: str | None, target: Path, home: Path | None = None) -> None:
    """Write ``projects/*/<session_id>.*`` from ``home`` into a gzipped tarball."""
    home = Path(home or commandcode_home())
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(target, "w:gz") as archive:
        if session_id and SESSION_RE.fullmatch(session_id):
            for path in sorted((home / "projects").glob(f"*/{session_id}.*")):
                if path.is_file():
                    archive.add(path, arcname=path.relative_to(home).as_posix())


# --------------------------------------------------------------------------- fetch


def ref_for(task: dict) -> str:
    """The ref the agent job must check out: the task branch when continuing, else the base."""
    if task.get("continue_branch"):
        branch = task.get("branch")
        if not isinstance(branch, str) or not BRANCH_RE.match(branch) or ".." in branch:
            raise TaskError(f"Invalid task branch: {branch!r}")
        return branch
    base = task.get("base")
    if not isinstance(base, str) or not BRANCH_RE.match(base) or ".." in base:
        raise TaskError(f"Invalid base branch: {base!r}")
    return base


def fetch_task(api: str, secret: str, task_id: str, out: Path | str, *,
               post_started: bool = True, restore_session: bool = True,
               home: Path | None = None) -> dict | None:
    """Fetch a task, write ``task.json`` and set step outputs. Returns None when cancelled (410)."""
    path = f"/api/tasks/{task_id}"
    status, body = api_request(api, secret, "GET", path)
    if status == 410:
        set_output("kind", "cancelled")
        return None
    if status != 200:
        raise TaskError(f"GET {path} returned {status}: {body[:500]!r}")
    try:
        task = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TaskError(f"GET {path} returned invalid JSON: {exc}") from exc
    if not isinstance(task, dict):
        raise TaskError(f"GET {path} did not return an object.")

    raw_session = task.pop("session", None)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "task.json").write_text(json.dumps(task, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if restore_session and raw_session:
        if not isinstance(raw_session, str):
            raise TaskError("Task session must be base64 text.")
        try:
            blob = base64.b64decode(raw_session, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise TaskError(f"Task session is not valid base64: {exc}") from exc
        safe_extract(blob, Path(home or commandcode_home()))

    if post_started:
        run_id = _required_env("GITHUB_RUN_ID")
        repository = _required_env("GITHUB_REPOSITORY")
        try:
            run_number = int(run_id)
        except ValueError as exc:
            raise TaskError(f"GITHUB_RUN_ID is not a number: {run_id!r}") from exc
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
        status, reply = api_request(api, secret, "POST", f"{path}/started",
                                    {"run_id": run_number,
                                     "run_url": f"{server}/{repository}/actions/runs/{run_id}"})
        if status != 204:
            raise TaskError(f"POST {path}/started returned {status}: {reply[:500]!r}")

    kind = task.get("kind")
    if not isinstance(kind, str) or not kind:
        raise TaskError("Task has no kind.")
    set_output("kind", kind)
    set_output("ref", ref_for(task))
    return task


# --------------------------------------------------------------------------- agent


def _task_text(task: dict, key: str) -> str:
    value = task.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TaskError(f"Task is missing {key}.")
    return value


def _optional_str(task: dict, key: str) -> str | None:
    value = task.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise TaskError(f"Task {key} must be text.")
    return value


def _base(task: dict) -> str:
    base = task.get("base")
    if not isinstance(base, str) or not BRANCH_RE.match(base) or ".." in base:
        raise TaskError(f"Invalid base branch: {base!r}")
    return base


def _branch(task: dict) -> str:
    branch = task.get("branch")
    if not isinstance(branch, str) or not BRANCH_RE.match(branch) or ".." in branch:
        raise TaskError(f"Invalid task branch: {branch!r}")
    if not branch.startswith(gitops.BRANCH_PREFIX):
        raise TaskError(f"Refusing to touch a branch outside {gitops.BRANCH_PREFIX}: {branch!r}")
    return branch


def _timeout(task: dict) -> tuple[int, float]:
    raw = task.get("timeout_minutes", DEFAULT_TIMEOUT_MINUTES)
    try:
        minutes = int(raw)
    except (TypeError, ValueError) as exc:
        raise TaskError(f"Invalid timeout_minutes: {raw!r}") from exc
    if not 1 <= minutes <= 120:
        raise TaskError(f"timeout_minutes is out of range: {minutes}")
    return minutes, float(minutes * 60)


def _first_line(text: str, limit: int = 120) -> str:
    stripped = (text or "").strip()
    return (stripped.splitlines()[0] if stripped else "task")[:limit]


def _git_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("TELEGRAM_")}
    env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    return env


def _format_patch(directory: Path, start: str) -> bytes:
    done = subprocess.run(["git", "format-patch", f"{start}..HEAD", "--stdout"], cwd=directory,
                          capture_output=True, env=_git_env(), timeout=120)
    if done.returncode != 0:
        raise gitops.GitError(
            f"git format-patch failed: {done.stderr.decode('utf-8', errors='replace').strip()[:500]}")
    return done.stdout


def _commit_count(directory: Path, start: str) -> int:
    try:
        return int(gitops.git(directory, "rev-list", "--count", f"{start}..HEAD") or 0)
    except ValueError as exc:
        raise gitops.GitError("git rev-list returned a non-number.") from exc


def _failure(result: RunResult, timeout_minutes: int) -> str:
    if result.timed_out:
        return f"Command Code timed out after {timeout_minutes} minutes."
    if result.cancelled:
        return "Command Code was cancelled."
    if result.exit_code is None:
        return "Command Code could not be started."
    meaning = EXIT_MEANINGS.get(result.exit_code)
    return f"Command Code exited with code {result.exit_code}" + (f" ({meaning})" if meaning else "") + "."


def run_agent(task: dict, out: Path | str, directory: Path | str, command_bin: str | None = None,
              *, home: Path | None = None) -> dict:
    """Run one task in ``directory`` and write ``result.json``/``changes.patch``/``session.tar.gz``."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    directory = Path(directory)
    command_bin = command_bin or command_executable()

    kind = task.get("kind")
    if kind not in ("ask", "plan", "do"):
        raise TaskError(f"Unknown task kind: {kind!r}")
    prompt = _task_text(task, "prompt")
    base = _base(task)
    timeout_minutes, timeout_seconds = _timeout(task)
    session_id = _optional_str(task, "session_id")
    model = _optional_str(task, "model")
    effort = _optional_str(task, "effort")
    if session_id is not None and not SESSION_RE.fullmatch(session_id):
        raise TaskError(f"Invalid session id: {session_id!r}")

    branch: str | None = None
    patch = b""
    commits = 0

    if kind == "do":
        branch = _branch(task)
        edit_mode = task.get("edit_mode", "edits")
        if edit_mode not in EDIT_PREFIX:
            raise TaskError(f"Invalid edit_mode: {edit_mode!r}")
        if task.get("continue_branch"):
            current = gitops.git(directory, "rev-parse", "--abbrev-ref", "HEAD")
            if current != branch:
                gitops.git(directory, "switch", branch)
        else:
            gitops.git(directory, "switch", "-c", branch)
        start = gitops.git(directory, "rev-parse", "HEAD")
        gitops.prepare_agent_settings(directory, edit_mode)
        result = asyncio.run(run_headless(
            command_bin, directory, EDIT_PREFIX[edit_mode].format(branch=branch, base=base) + prompt,
            timeout_seconds, edit=True, edit_mode=edit_mode, session_id=session_id,
            model=model, effort=effort))
        if result.ok:
            gitops.commit_all(directory, f"ccbot: {_first_line(prompt)}")
            patch = _format_patch(directory, start)
            commits = _commit_count(directory, start)
    else:
        branch = None
        prefix = ASK_PREFIX if kind == "ask" else PLAN_PREFIX
        result = asyncio.run(run_headless(
            command_bin, directory, prefix + prompt, timeout_seconds, edit=False,
            session_id=session_id, model=model, effort=effort))

    session_out = (result.session_id
                   if isinstance(result.session_id, str) and SESSION_RE.fullmatch(result.session_id)
                   else None)
    record = {
        "ok": result.ok,
        "exit_code": result.exit_code,
        "timed_out": bool(result.timed_out),
        "text": (result.output or "")[:MAX_TEXT_CHARS],
        "session_id": session_out,
        "tool_calls": int(result.tool_calls or 0),
        "branch": branch if (kind == "do" and result.ok) else None,
        "commits": commits if result.ok else 0,
        "error": None if result.ok else _failure(result, timeout_minutes),
    }
    (out / "result.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    if kind == "do":
        (out / "changes.patch").write_bytes(patch)
    write_session_archive(session_out, out / "session.tar.gz", home=home)
    return record


def _failed_record(error: str) -> dict:
    return {
        "ok": False, "exit_code": None, "timed_out": False, "text": "", "session_id": None,
        "tool_calls": 0, "branch": None, "commits": 0, "error": error[:2000],
    }


def agent(out: Path | str, directory: Path | str, command_bin: str | None = None,
          *, home: Path | None = None) -> dict:
    """CLI entry for the agent job: always leaves a result.json behind, even on failure."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        task = json.loads((out / "task.json").read_text(encoding="utf-8"))
        if not isinstance(task, dict):
            raise TaskError("task.json is not an object.")
        return run_agent(task, out, directory, command_bin, home=home)
    except Exception as exc:  # noqa: BLE001 - the artifact must always contain a result
        message = str(exc) if isinstance(exc, TaskError) else f"{type(exc).__name__}: {exc}"
        record = _failed_record(message)
        (out / "result.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        write_session_archive(None, out / "session.tar.gz", home=home)
        return record


# --------------------------------------------------------------------------- publish


def patch_paths(patch: str) -> list[str]:
    """Every repository path mentioned in a git patch."""
    paths: list[str] = []
    for line in patch.splitlines():
        try:
            if line.startswith("diff --git "):
                parts = shlex.split(line[len("diff --git "):])
            elif line.startswith(("--- ", "+++ ")):
                parts = shlex.split(line[4:])
            elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
                parts = shlex.split(line.split(" ", 2)[2])
            else:
                continue
        except ValueError as exc:
            raise TaskError(f"The patch contains an unparseable path: {line[:120]!r}") from exc
        for raw in parts:
            path = raw.replace("\\", "/")
            if path in ("/dev/null", "dev/null"):
                continue
            if path[:2] in ("a/", "b/"):
                path = path[2:]
            if path:
                paths.append(path)
    return paths


def validate_patch(blob: bytes) -> None:
    """Reject oversized patches and anything that touches workflow files."""
    if len(blob) > MAX_PATCH_BYTES:
        raise TaskError(f"The patch is larger than {MAX_PATCH_BYTES // (1024 * 1024)} MB.")
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TaskError("The patch is not UTF-8 text.") from exc
    for path in patch_paths(text):
        if "\x00" in path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
            raise TaskError(f"The patch contains an unsafe path: {path!r}")
        parts = PurePosixPath(path).parts
        if ".." in parts:
            raise TaskError(f"The patch contains an unsafe path: {path!r}")
        if tuple(part.lower() for part in parts[:2]) == (".github", "workflows"):
            raise TaskError(f"The patch modifies a GitHub workflow, which is not allowed: {path}")


def _read_artifact(artifact: Path | str) -> tuple[dict, bytes | None, bytes | None]:
    """Read the agent artifact. Its contents are untrusted: types and sizes are checked here."""
    artifact = Path(artifact)
    result_path = artifact / "result.json"
    if not result_path.is_file():
        return _failed_record("The agent job produced no result: result.json is missing "
                              "from the ccbot-out artifact."), None, None
    try:
        raw = json.loads(result_path.read_text(encoding="utf-8"))
        record = _clean_result(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _failed_record(f"The agent result is unreadable: {exc}"), None, None
    except TaskError as exc:
        return _failed_record(f"The agent result is invalid: {exc}"), None, None

    patch = None
    patch_path = artifact / "changes.patch"
    if patch_path.is_file():
        try:
            with patch_path.open("rb") as handle:
                patch = handle.read(MAX_PATCH_BYTES + 1)
        except OSError as exc:
            return _failed_record(f"The patch could not be read: {exc}"), None, None

    session = None
    session_path = artifact / "session.tar.gz"
    if session_path.is_file():
        try:
            with session_path.open("rb") as handle:
                blob = handle.read(MAX_SESSION_BYTES + 1)
        except OSError:
            blob = b""
        if len(blob) <= MAX_SESSION_BYTES and blob and valid_session_archive(blob):
            session = blob
        elif blob:
            print("ccbot: ignoring an invalid session archive.", file=sys.stderr)
    return record, patch, session


def _clean_result(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise TaskError("result.json is not an object")

    ok = raw.get("ok")
    if not isinstance(ok, bool):
        raise TaskError("result.json: ok must be true or false")
    exit_code = raw.get("exit_code")
    if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
        raise TaskError("result.json: exit_code must be an integer or null")
    timed_out = raw.get("timed_out", False)
    if not isinstance(timed_out, bool):
        raise TaskError("result.json: timed_out must be true or false")
    text = raw.get("text", "")
    if not isinstance(text, str):
        raise TaskError("result.json: text must be a string")
    session_id = raw.get("session_id")
    if session_id is not None and (not isinstance(session_id, str)
                                   or not SESSION_RE.fullmatch(session_id)):
        raise TaskError("result.json: session_id is invalid")
    tool_calls = raw.get("tool_calls", 0)
    if isinstance(tool_calls, bool) or not isinstance(tool_calls, int) or tool_calls < 0:
        raise TaskError("result.json: tool_calls must be a non-negative integer")
    commits = raw.get("commits", 0)
    if isinstance(commits, bool) or not isinstance(commits, int) or commits < 0:
        raise TaskError("result.json: commits must be a non-negative integer")
    branch = raw.get("branch")
    if branch is not None and not isinstance(branch, str):
        raise TaskError("result.json: branch must be a string or null")
    error = raw.get("error")
    if error is not None and not isinstance(error, str):
        raise TaskError("result.json: error must be a string or null")
    return {
        "ok": ok, "exit_code": exit_code, "timed_out": timed_out,
        "text": text[:MAX_TEXT_CHARS], "session_id": session_id, "tool_calls": tool_calls,
        "branch": branch, "commits": commits, "error": error[:2000] if error else None,
    }


def _append_error(old: str | None, new: str) -> str:
    return "; ".join(part for part in (old, new) if part)[:2000]


def _checkout_branch(repo_dir: Path, base: str, branch: str, continue_branch: bool) -> None:
    # The workflow checks out with fetch-depth: 0 and persist-credentials: false, so every remote
    # ref is already local and no authenticated fetch is possible (or needed) here.
    source = f"origin/{branch if continue_branch else base}"
    if not gitops.git(repo_dir, "rev-parse", "--verify", "--quiet", source, check=False):
        source = "HEAD"
    gitops.git(repo_dir, "switch", "-C", branch, source)


def _apply_patch(repo_dir: Path, patch: bytes) -> None:
    handle, name = tempfile.mkstemp(suffix=".patch")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(patch)
        try:
            gitops.git(repo_dir, "-c", "user.name=Cloud Command Code",
                       "-c", "user.email=ccbot@localhost", "am", "--3way", name)
        except gitops.GitError:
            gitops.git(repo_dir, "am", "--abort", check=False)
            raise
    finally:
        Path(name).unlink(missing_ok=True)


def _push_branch(repo_dir: Path, branch: str, token: str | None) -> None:
    if not token:
        raise TaskError("GITHUB_TOKEN is not set; the branch cannot be pushed.")
    basic = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    gitops.git(repo_dir, "-c", f"http.extraheader=AUTHORIZATION: basic {basic}",
               "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}", timeout=300)


def _open_pr(repo_dir: Path, repo: str, base: str, branch: str, prompt: str, summary: str,
             token: str | None) -> str | None:
    if not REPO_RE.match(repo or ""):
        raise TaskError(f"Invalid repository name: {repo!r}")
    env = {**_git_env(), "GH_TOKEN": token or "", "GITHUB_TOKEN": token or ""}
    listed = subprocess.run(
        ["gh", "pr", "list", "--repo", repo, "--head", branch, "--state", "open",
         "--json", "url", "--jq", ".[0].url // empty"],
        cwd=repo_dir, capture_output=True, text=True, env=env, timeout=120)
    if listed.returncode != 0:
        raise TaskError(f"gh pr list failed: {(listed.stderr or listed.stdout).strip()[:500]}")
    existing = listed.stdout.strip()
    if existing:
        return existing
    title = f"ccbot: {_first_line(prompt, 140)}"
    body = (prompt.strip() + "\n\n---\n\n" + (summary or "").strip()).strip()[:MAX_TEXT_CHARS]
    if not body:
        body = "Task from Cloud Command Code."
    created = subprocess.run(
        ["gh", "pr", "create", "--repo", repo, "--base", base, "--head", branch,
         "--title", title, "--body", body],
        cwd=repo_dir, capture_output=True, text=True, env=env, timeout=120)
    if created.returncode != 0:
        raise TaskError(f"gh pr create failed: {(created.stderr or created.stdout).strip()[:500]}")
    lines = [line.strip() for line in created.stdout.splitlines() if line.strip()]
    return lines[-1] if lines else None


def publish_task(task: dict, artifact: Path | str, repo_dir: Path | str, *,
                 token: str | None = None, repo: str | None = None) -> dict:
    """Validate the artifact, push an accepted patch and open a PR. Returns the result payload."""
    kind = task.get("kind")
    if kind not in ("ask", "plan", "do"):
        raise TaskError(f"Unknown task kind: {kind!r}")
    base = _base(task)
    branch = _branch(task) if kind == "do" else None
    prompt = task.get("prompt") if isinstance(task.get("prompt"), str) else ""
    repo = repo or task.get("repo") or ""
    repo_dir = Path(repo_dir)

    record, patch, session = _read_artifact(artifact)
    pushed = False
    pr_url = None

    if kind == "do" and record["ok"] and patch:
        try:
            if not branch:
                raise TaskError("The task has no branch.")
            validate_patch(patch)
            _checkout_branch(repo_dir, base, branch, bool(task.get("continue_branch")))
            _apply_patch(repo_dir, patch)
            _push_branch(repo_dir, branch, token)
            pushed = True
        except (TaskError, gitops.GitError, OSError, subprocess.SubprocessError) as exc:
            record = {**record, "ok": False,
                      "error": _append_error(record["error"], f"Could not publish the changes: {exc}")}

    if pushed:
        try:
            pr_url = _open_pr(repo_dir, repo or "", base, branch or "", prompt, record["text"], token)
        except (TaskError, OSError, subprocess.SubprocessError) as exc:
            record = {**record, "ok": False, "error": _append_error(
                record["error"], f"The branch was pushed but no pull request was opened: {exc}")}

    return {
        "ok": record["ok"],
        "exit_code": record["exit_code"],
        "timed_out": record["timed_out"],
        "text": record["text"],
        # A session id without its files cannot be resumed, so the two travel together.
        "session_id": record["session_id"] if session else None,
        "session": base64.b64encode(session).decode("ascii") if session else None,
        "tool_calls": record["tool_calls"],
        "branch": branch if pushed else None,
        "commits": record["commits"] if pushed else 0,
        "pr_url": pr_url,
        "error": record["error"],
    }


def publish(api: str, secret: str, task_id: str, task: dict, artifact: Path | str,
            repo_dir: Path | str, *, token: str | None = None, repo: str | None = None) -> dict:
    try:
        result = publish_task(task, artifact, repo_dir, token=token, repo=repo)
    except (TaskError, gitops.GitError, OSError) as exc:
        result = _failed_record(f"Publishing failed: {exc}")
    status, body = api_request(api, secret, "POST", f"/api/tasks/{task_id}/result", result)
    if status != 204:
        raise TaskError(f"POST /api/tasks/{task_id}/result returned {status}: {body[:500]!r}")
    return result


def delete_run_artifacts(repo: str, run_id: str, token: str | None) -> None:
    """Delete every artifact of this workflow run. Best effort: retention is one day anyway."""
    if not repo or not run_id or not token:
        return
    env = {**_git_env(), "GH_TOKEN": token, "GITHUB_TOKEN": token}
    try:
        listed = subprocess.run(
            ["gh", "api", f"repos/{repo}/actions/runs/{run_id}/artifacts",
             "--paginate", "--jq", ".artifacts[].id"],
            capture_output=True, text=True, env=env, timeout=120)
        if listed.returncode != 0:
            print(f"ccbot: could not list artifacts: "
                  f"{(listed.stderr or listed.stdout).strip()[:300]}", file=sys.stderr)
            return
        for artifact_id in listed.stdout.split():
            if not artifact_id.isdigit():
                continue
            subprocess.run(["gh", "api", "-X", "DELETE", f"repos/{repo}/actions/artifacts/{artifact_id}"],
                           capture_output=True, text=True, env=env, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"ccbot: could not delete artifacts: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m commandcode_bot.task")
    commands = parser.add_subparsers(dest="command", required=True)

    fetch_parser = commands.add_parser("fetch", help="fetch a task from the Worker")
    fetch_parser.add_argument("--out", default=str(default_out()))
    fetch_parser.add_argument("--no-started", action="store_true",
                              help="do not POST /started (used by the publish job)")
    fetch_parser.add_argument("--no-session", action="store_true",
                              help="do not restore the session archive")

    agent_parser = commands.add_parser("agent", help="run the task in the current checkout")
    agent_parser.add_argument("--out", default=str(default_out()))

    publish_parser = commands.add_parser("publish", help="push the patch and post the result")
    publish_parser.add_argument("--out", default=str(default_out()))
    publish_parser.add_argument("--artifact", default=str(default_out().parent / "ccbot-artifact"))
    publish_parser.add_argument("--repo", default=str(Path.cwd()))

    args = parser.parse_args(argv)

    try:
        if args.command == "fetch":
            api = _required_env("CCBOT_API")
            secret = _required_env("CCBOT_SECRET")
            task = fetch_task(api, secret, _required_env("CCBOT_TASK_ID"), args.out,
                              post_started=not args.no_started,
                              restore_session=not args.no_session)
            print("Task was cancelled." if task is None else f"Fetched {task.get('kind')} task.")
            return 0

        if args.command == "agent":
            record = agent(args.out, Path.cwd(), command_executable())
            print(f"Agent finished: ok={record['ok']} exit={record['exit_code']}")
            return 0

        api = _required_env("CCBOT_API")
        secret = _required_env("CCBOT_SECRET")
        task_id = _required_env("CCBOT_TASK_ID")
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        task = fetch_task(api, secret, task_id, args.out, post_started=False, restore_session=False)
        if task is None:
            delete_run_artifacts(repository, run_id, token)
            print("Task was cancelled; nothing to publish.")
            return 0
        result = publish(api, secret, task_id, task, args.artifact, args.repo,
                         token=token, repo=task.get("repo") or repository)
        delete_run_artifacts(repository, run_id, token)
        print(f"Published result: ok={result['ok']} pr={result['pr_url']}")
        return 0
    except (TaskError, gitops.GitError) as exc:
        print(f"ccbot: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
