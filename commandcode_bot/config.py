import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    token: str
    allowed_user_id: int
    projects: dict[str, Path]
    command_bin: str
    timeout_seconds: int


def load_config() -> Config:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or token == "replace-with-botfather-token":
        raise ValueError("Set TELEGRAM_BOT_TOKEN to the token from BotFather.")

    try:
        user_id = int(os.environ["TELEGRAM_USER_ID"])
    except (KeyError, ValueError) as exc:
        raise ValueError("Set TELEGRAM_USER_ID to your numeric Telegram user ID.") from exc
    if user_id <= 0:
        raise ValueError("TELEGRAM_USER_ID must be positive.")

    project_file = Path(os.environ.get("PROJECTS_FILE", "projects.json")).expanduser()
    try:
        raw = json.loads(project_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read project configuration: {project_file}") from exc
    if not isinstance(raw, dict) or not raw:
        raise ValueError("Project configuration must be a nonempty name-to-path object.")

    projects: dict[str, Path] = {}
    for name, location in raw.items():
        if not isinstance(name, str) or not name or name.lower() != name or not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"Invalid project name: {name!r}")
        if not isinstance(location, str):
            raise ValueError(f"Project {name} needs an absolute directory path.")
        path = Path(location).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise ValueError(f"Project {name} directory does not exist: {path}")
        projects[name] = path.resolve()

    command_bin = os.environ.get("COMMAND_CODE_BIN", "cmd").strip()
    if not command_bin or shutil.which(command_bin) is None:
        raise ValueError(f"Command Code executable not found: {command_bin!r}")

    try:
        timeout = int(os.environ.get("RUN_TIMEOUT_SECONDS", "900"))
    except ValueError as exc:
        raise ValueError("RUN_TIMEOUT_SECONDS must be an integer.") from exc
    if not 30 <= timeout <= 3600:
        raise ValueError("RUN_TIMEOUT_SECONDS must be between 30 and 3600.")

    return Config(token, user_id, projects, command_bin, timeout)

