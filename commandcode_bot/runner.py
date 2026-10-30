import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path


MAX_OUTPUT_BYTES = 65536


@dataclass(frozen=True)
class RunResult:
    exit_code: int | None
    output: str
    timed_out: bool = False
    truncated: bool = False


def command_args(executable: str, prompt: str) -> list[str]:
    # The permission boundary is deliberately fixed here, outside Telegram input.
    return [
        executable,
        "-p",
        prompt,
        "--output-format",
        "text",
        "--permission-mode",
        "plan",
        "--skip-onboarding",
        "--trust",
        "--max-turns",
        "30",
    ]


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


async def run_headless(executable: str, directory: Path, prompt: str, timeout: int) -> RunResult:
    try:
        child_env = {key: value for key, value in os.environ.items() if not key.startswith("TELEGRAM_")}
        process = await asyncio.create_subprocess_exec(
            *command_args(executable, prompt),
            cwd=directory,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=child_env,
            **({"start_new_session": True} if os.name == "posix" else {}),
        )
    except OSError as exc:
        return RunResult(None, f"Could not start Command Code: {exc}")

    output = bytearray()
    truncated = False

    async def drain() -> None:
        nonlocal truncated
        assert process.stdout is not None
        while chunk := await process.stdout.read(8192):
            output.extend(chunk)
            if len(output) > MAX_OUTPUT_BYTES:
                del output[:-MAX_OUTPUT_BYTES]
                truncated = True

    try:
        async with asyncio.timeout(timeout):
            await asyncio.gather(drain(), process.wait())
    except TimeoutError:
        await _stop_process(process)
        return RunResult(process.returncode, output.decode("utf-8", errors="replace"), timed_out=True, truncated=truncated)
    except asyncio.CancelledError:
        await _stop_process(process)
        raise

    return RunResult(process.returncode, output.decode("utf-8", errors="replace").strip(), truncated=truncated)
