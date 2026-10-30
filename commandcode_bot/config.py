import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
BRANCH_NAME = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
MODEL_NAME = re.compile(r"^[A-Za-z0-9._/:-]{1,100}$")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class Project:
    name: str
    path: Path
    writable: bool = False
    base: str = "main"


@dataclass(frozen=True)
class Config:
    token: str
    allowed_user_id: int
    projects: dict[str, Project]
    command_bin: str
    timeout_seconds: int
    edit_timeout_seconds: int = 2700
    state_file: Path = Path("state.json")
    worktree_root: Path = Path("worktrees")
    default_model: str | None = None
    default_effort: str | None = None
    edit_permission: str = "auto-accept"


def _bounded_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}.")
    return value


def parse_projects(raw: object) -> dict[str, Project]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError("Project configuration must be a nonempty name-to-project object.")

    projects: dict[str, Project] = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not PROJECT_NAME.match(name):
            raise ValueError(f"Invalid project name: {name!r}")
        if isinstance(entry, str):
            entry = {"path": entry}
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError(f"Project {name} needs an absolute directory path.")

        path = Path(entry["path"]).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise ValueError(f"Project {name} directory does not exist: {path}")

        writable = entry.get("writable", False)
        if not isinstance(writable, bool):
            raise ValueError(f"Project {name}: 'writable' must be true or false.")
        base = entry.get("base", "main")
        if not isinstance(base, str) or not BRANCH_NAME.match(base) or ".." in base:
            raise ValueError(f"Project {name}: invalid base branch {base!r}.")
        if writable and not (path / ".git").exists():
            raise ValueError(f"Project {name} is writable but {path} is not a git checkout.")

        projects[name] = Project(name, path.resolve(), writable, base)
    return projects


def optional_model(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if not MODEL_NAME.match(value):
        raise ValueError(f"Invalid model id: {value!r}")
    return value


def optional_effort(value: str | None) -> str | None:
    value = (value or "").strip().lower()
    if not value:
        return None
    if value not in EFFORT_LEVELS:
        raise ValueError(f"Effort must be one of: {', '.join(EFFORT_LEVELS)}.")
    return value


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
    projects = parse_projects(raw)

    command_bin = os.environ.get("COMMAND_CODE_BIN", "cmd").strip()
    if not command_bin or shutil.which(command_bin) is None:
        raise ValueError(f"Command Code executable not found: {command_bin!r}")

    state_dir = Path.home() / ".local" / "state" / "commandcode-bot"
    state_file = Path(os.environ.get("STATE_FILE", str(state_dir / "state.json"))).expanduser()
    worktree_root = Path(os.environ.get("WORKTREE_ROOT", str(Path.home() / "worktrees"))).expanduser()
    if not state_file.is_absolute() or not worktree_root.is_absolute():
        raise ValueError("STATE_FILE and WORKTREE_ROOT must be absolute paths.")

    edit_permission = os.environ.get("EDIT_PERMISSION", "auto-accept").strip().lower()
    if edit_permission not in ("auto-accept", "yolo"):
        raise ValueError("EDIT_PERMISSION must be 'auto-accept' or 'yolo'.")

    return Config(
        token=token,
        allowed_user_id=user_id,
        projects=projects,
        command_bin=command_bin,
        timeout_seconds=_bounded_int("RUN_TIMEOUT_SECONDS", 900, 30, 3600),
        edit_timeout_seconds=_bounded_int("EDIT_TIMEOUT_SECONDS", 2700, 30, 7200),
        state_file=state_file,
        worktree_root=worktree_root,
        default_model=optional_model(os.environ.get("DEFAULT_MODEL")),
        default_effort=optional_effort(os.environ.get("DEFAULT_EFFORT")),
        edit_permission=edit_permission,
    )
