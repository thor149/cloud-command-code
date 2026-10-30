import asyncio
import json
import os
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path


MAX_OUTPUT_BYTES = 65536
PLAN_MAX_TURNS = 40
EDIT_MAX_TURNS = 150
EDIT_PERMISSIONS = ("auto-accept", "yolo")

ProgressCallback = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class RunResult:
    exit_code: int | None
    output: str
    timed_out: bool = False
    truncated: bool = False
    cancelled: bool = False
    session_id: str | None = None
    tool_calls: int = 0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not (self.timed_out or self.cancelled)


def command_args(
    executable: str,
    prompt: str,
    *,
    edit: bool = False,
    edit_permission: str = "auto-accept",
    session_id: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> list[str]:
    # The permission boundary is fixed here from the bot's own state, never from Telegram text.
    args = [executable, "-p", prompt, "--output-format", "json", "--skip-onboarding", "--trust", "--no-auto-update"]
    if edit:
        if edit_permission not in EDIT_PERMISSIONS:
            raise ValueError(f"edit_permission must be one of {EDIT_PERMISSIONS}")
        args += ["--yolo"] if edit_permission == "yolo" else ["--permission-mode", "auto-accept"]
        args += ["--tools-all", "--max-turns", str(EDIT_MAX_TURNS)]
    else:
        args += ["--permission-mode", "plan", "--max-turns", str(PLAN_MAX_TURNS)]
    if session_id:
        args += ["--session", session_id]
    if model:
        args += ["--model", model]
    if effort:
        args += ["--effort", effort]
    return args


class StreamParser:
    """Reads Command Code's NDJSON event stream (``--output-format json``)."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.final_text: str | None = None
        self.subtype: str | None = None
        self.error: str | None = None
        self.tool_calls = 0
        self.last_tool: str | None = None
        self.plain: list[str] = []
        self._pending = b""

    def feed(self, chunk: bytes) -> list[str]:
        """Consume bytes; return the names of tools that started in this chunk."""
        self._pending += chunk
        *lines, self._pending = self._pending.split(b"\n")
        started: list[str] = []
        for line in lines:
            tool = self._line(line)
            if tool:
                started.append(tool)
        return started

    def finish(self) -> None:
        if self._pending.strip():
            self._line(self._pending)
        self._pending = b""

    def _line(self, raw: bytes) -> str | None:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        try:
            item = json.loads(text)
        except json.JSONDecodeError:
            self.plain.append(text)
            return None
        if not isinstance(item, dict):
            return None

        if item.get("type") == "result":
            self.subtype = item.get("subtype")
            self.session_id = item.get("sessionId") or self.session_id
            if isinstance(item.get("finalText"), str):
                self.final_text = item["finalText"]
            message = item.get("error") or item.get("message")
            if message:
                self.error = message if isinstance(message, str) else json.dumps(message)[:2000]
            return None

        event = item.get("event") if item.get("type") == "event" else None
        if not isinstance(event, dict):
            return None
        kind = event.get("type")
        if kind == "run_start" and event.get("sessionId"):
            self.session_id = event["sessionId"]
        elif kind == "tool_running":
            self.tool_calls += 1
            self.last_tool = str(event.get("toolName") or "tool")
            return self.last_tool
        elif kind == "run_end" and isinstance(event.get("result"), dict):
            final = event["result"].get("finalText")
            if isinstance(final, str) and self.final_text is None:
                self.final_text = final
        elif kind in ("error", "run_error") and event.get("message"):
            self.error = str(event["message"])
        return None

    def text(self) -> str:
        if self.final_text:
            return self.final_text.strip()
        parts = [p for p in (self.error, "\n".join(self.plain[-40:])) if p]
        return "\n".join(parts).strip()


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        process.kill()
    await process.wait()


async def run_headless(
    executable: str,
    directory: Path,
    prompt: str,
    timeout: float,
    *,
    edit: bool = False,
    edit_permission: str = "auto-accept",
    session_id: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    cancel: asyncio.Event | None = None,
    on_tool: ProgressCallback | None = None,
) -> RunResult:
    try:
        child_env = {key: value for key, value in os.environ.items() if not key.startswith("TELEGRAM_")}
        child_env["GIT_TERMINAL_PROMPT"] = "0"
        process = await asyncio.create_subprocess_exec(
            *command_args(executable, prompt, edit=edit, edit_permission=edit_permission,
                          session_id=session_id, model=model, effort=effort),
            cwd=directory,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=child_env,
            **({"start_new_session": True} if os.name == "posix" else {}),
        )
    except OSError as exc:
        return RunResult(None, f"Could not start Command Code: {exc}")

    parser = StreamParser()

    async def drain() -> None:
        assert process.stdout is not None
        while chunk := await process.stdout.read(8192):
            for tool in parser.feed(chunk):
                if on_tool is not None:
                    try:
                        await on_tool(tool)
                    except Exception:  # progress is best-effort; never break the run
                        pass
        parser.finish()

    def result(**extra) -> RunResult:
        text = parser.text()
        truncated = len(text.encode("utf-8")) > MAX_OUTPUT_BYTES
        if truncated:
            text = text.encode("utf-8")[-MAX_OUTPUT_BYTES:].decode("utf-8", errors="ignore")
        return RunResult(process.returncode, text, truncated=truncated, session_id=parser.session_id,
                         tool_calls=parser.tool_calls, **extra)

    async def discard(future: asyncio.Future) -> None:
        future.cancel()
        try:
            await future
        except (asyncio.CancelledError, Exception):
            pass

    work = asyncio.ensure_future(asyncio.gather(drain(), process.wait()))
    waiters = {work}
    stopper = None
    if cancel is not None:
        stopper = asyncio.ensure_future(cancel.wait())
        waiters.add(stopper)
    try:
        done, _ = await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        await _stop_process(process)
        await discard(work)
        raise
    finally:
        if stopper is not None:
            stopper.cancel()

    if work in done:
        work.result()
        return result()

    await _stop_process(process)
    await discard(work)
    if stopper is not None and stopper in done:
        return result(cancelled=True)
    return result(timed_out=True)
