"""Small persistent state: what survives a service restart."""

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path


LOG = logging.getLogger(__name__)


@dataclass
class EditTask:
    branch: str
    worktree: str
    base: str
    session_id: str | None = None
    created: str = ""


@dataclass
class ProjectState:
    ask_session: str | None = None
    task: EditTask | None = None


@dataclass
class BotState:
    active_project: str | None = None
    model: str | None = None
    effort: str | None = None
    projects: dict[str, ProjectState] = field(default_factory=dict)

    def project(self, name: str) -> ProjectState:
        return self.projects.setdefault(name, ProjectState())

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: object) -> "BotState":
        if not isinstance(data, dict):
            raise ValueError("state must be an object")
        raw_projects = data.get("projects") or {}
        if not isinstance(raw_projects, dict):
            raise ValueError("projects must be an object")
        projects = {}
        for name, raw in raw_projects.items():
            if not isinstance(name, str) or not isinstance(raw, dict):
                continue
            task = raw.get("task")
            projects[name] = ProjectState(ask_session=_text(raw.get("ask_session")), task=_task(task))
        return cls(
            active_project=_text(data.get("active_project")),
            model=_text(data.get("model")),
            effort=_text(data.get("effort")),
            projects=projects,
        )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _task(raw: object) -> EditTask | None:
    if not isinstance(raw, dict):
        return None
    branch, worktree, base = (_text(raw.get(key)) for key in ("branch", "worktree", "base"))
    if not (branch and worktree and base):
        return None
    return EditTask(branch, worktree, base, _text(raw.get("session_id")), _text(raw.get("created")) or "")


def load_state(path: Path, known_projects: set[str]) -> BotState:
    try:
        state = BotState.from_json(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return BotState()
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
        LOG.warning("Ignoring unreadable state file %s (%s); starting fresh.", path, type(exc).__name__)
        try:
            path.replace(path.with_name(path.name + ".corrupt"))
        except OSError:
            pass
        return BotState()

    # Forget projects that were removed from projects.json since the last run.
    state.projects = {name: value for name, value in state.projects.items() if name in known_projects}
    if state.active_project not in known_projects:
        state.active_project = None
    return state


def save_state(path: Path, state: BotState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state.to_json(), indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
