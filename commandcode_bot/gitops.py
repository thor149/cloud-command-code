"""Git plumbing for edit tasks. Every task lives on its own ``ccbot/*`` branch in its own worktree,
so the project's main checkout and base branch are never modified by the agent."""

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


BRANCH_PREFIX = "ccbot/"
GIT_TIMEOUT = 120


class GitError(RuntimeError):
    pass


def git(cwd: Path, *args: str, timeout: int = GIT_TIMEOUT, check: bool = True) -> str:
    try:
        done = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
            env={**{k: v for k, v in os.environ.items() if not k.startswith("TELEGRAM_")},
                 "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {args[0]} failed: {exc}") from exc
    if check and done.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])} failed: {(done.stderr or done.stdout).strip()[:500]}")
    return done.stdout.strip()


def _has_remote(repo: Path) -> bool:
    return "origin" in git(repo, "remote").split()


def _fetch_base(repo: Path, base: str, check: bool) -> None:
    # An explicit refspec also updates refs/remotes/origin/<base> in single-branch clones.
    git(repo, "fetch", "--quiet", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}",
        check=check, timeout=180)


def base_ref(repo: Path, base: str) -> str:
    """Prefer the freshly fetched remote base; fall back to the local branch."""
    if _has_remote(repo):
        _fetch_base(repo, base, check=False)
        if git(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{base}", check=False):
            return f"origin/{base}"
    if git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{base}", check=False):
        return base
    raise GitError(f"Base branch {base!r} was not found locally or on origin.")


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:5]
    return "-".join(words)[:40] or "task"


@dataclass(frozen=True)
class NewWorktree:
    branch: str
    path: Path
    base_ref: str


def create_task_worktree(repo: Path, base: str, root: Path, project: str, task: str) -> NewWorktree:
    ref = base_ref(repo, base)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    branch = f"{BRANCH_PREFIX}{project}-{stamp}-{_slug(task)}"
    path = root / project / branch.removeprefix(BRANCH_PREFIX)
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "--quiet", "-b", branch, str(path), ref)
    return NewWorktree(branch, path, ref)


def commit_all(worktree: Path, message: str) -> str | None:
    """Commit anything the agent left uncommitted. Returns the new commit, or None if clean."""
    if not git(worktree, "status", "--porcelain"):
        return None
    git(worktree, "add", "-A")
    git(worktree, "-c", "user.name=Cloud Command Code", "-c", "user.email=ccbot@localhost",
        "commit", "--quiet", "--no-verify", "-m", message[:200])
    return git(worktree, "rev-parse", "--short", "HEAD")


def _merge_base(worktree: Path, ref: str) -> str:
    return git(worktree, "merge-base", ref, "HEAD")


def summary(worktree: Path, ref: str) -> str:
    """Commits and file stats on the task branch relative to its base."""
    start = _merge_base(worktree, ref)
    log = git(worktree, "log", "--oneline", "--no-decorate", f"{start}..HEAD")
    stat = git(worktree, "diff", "--stat", start, "HEAD")
    if not log:
        return "No changes on the task branch yet."
    count = len(log.splitlines())
    return f"{count} commit(s):\n{log}\n\n{stat}"


def full_diff(worktree: Path, ref: str) -> str:
    return git(worktree, "diff", _merge_base(worktree, ref), "HEAD")


def push_branch(worktree: Path, branch: str, base: str) -> str:
    if not branch.startswith(BRANCH_PREFIX) or branch == base:
        raise GitError("Refusing to push anything other than a ccbot/* task branch.")
    if git(worktree, "rev-parse", "--abbrev-ref", "HEAD") != branch:
        raise GitError("The worktree is not on its task branch; not pushing.")
    git(worktree, "push", "--quiet", "-u", "origin", f"HEAD:refs/heads/{branch}", timeout=300)
    return compare_url(git(worktree, "remote", "get-url", "origin"), base, branch)


def remove_task(repo: Path, worktree: Path, branch: str) -> None:
    if not branch.startswith(BRANCH_PREFIX):
        raise GitError("Refusing to delete a branch that is not a ccbot/* task branch.")
    if worktree.exists():
        git(repo, "worktree", "remove", "--force", str(worktree))
    git(repo, "worktree", "prune", check=False)
    if git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False):
        git(repo, "branch", "-D", branch)
    if git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False):
        raise GitError(f"Branch {branch} still exists (is it checked out in another worktree?).")


AGENT_SETTINGS = Path(".commandcode") / "settings.local.json"
# Deny beats every allow, from any settings file, even under --yolo. Verified: with a repository
# shipping allow ["Shell"], this deny still blocked the shell tool and a Read(.env) deny blocked the read.
EDITS_ONLY_POLICY = {
    "permissions": {
        "allow": ["Edit(**)", "Write(**)"],
        "deny": ["Shell", "WebFetch", "WebSearch", "mcp__*"],
    }
}


def prepare_agent_settings(worktree: Path, edit_mode: str) -> None:
    """Pin the worktree's local Command Code permissions for this run. In 'edits' mode the agent may
    edit files only; a deny on Shell overrides any allow rule a repository ships in its own settings."""
    target = worktree / AGENT_SETTINGS
    if edit_mode == "edits":
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps(EDITS_ONLY_POLICY, indent=2) + "\n", encoding="utf-8")
    elif target.exists():
        target.unlink()
    common = Path(git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    exclude = common / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    entry = AGENT_SETTINGS.as_posix()
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if entry not in existing.splitlines():
        exclude.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n") + entry + "\n",
                           encoding="utf-8")


def sync_checkout(repo: Path, base: str) -> str:
    """Fast-forward the main (read-only) checkout so /ask sees current code."""
    if not _has_remote(repo):
        return "No origin remote; nothing to sync."
    if git(repo, "status", "--porcelain"):
        return "The main checkout has local changes; not syncing."
    current = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if current != base:
        return f"The main checkout is on {current}, not {base}; not syncing."
    _fetch_base(repo, base, check=True)
    before = git(repo, "rev-parse", "--short", "HEAD")
    git(repo, "merge", "--ff-only", "--quiet", f"origin/{base}")
    after = git(repo, "rev-parse", "--short", "HEAD")
    return f"Already up to date at {after}." if before == after else f"Updated {before} → {after}."


def compare_url(remote: str, base: str, branch: str) -> str:
    """A GitHub "open pull request" link, only for GitHub remotes (incl. our github-<name> SSH aliases)."""
    match = (re.match(r"^git@(github\.com|github-[a-z0-9_-]+):([^/]+)/(.+?)(?:\.git)?$", remote)
             or re.match(r"^https://(?:[^@/]+@)?(github\.com)/([^/]+)/(.+?)(?:\.git)?/?$", remote)
             or re.match(r"^ssh://git@(github\.com|github-[a-z0-9_-]+)/([^/]+)/(.+?)(?:\.git)?$", remote))
    if not match:
        return f"(no pull-request link: {remote.split('@')[-1]} is not a GitHub remote)"
    _, owner, name = match.groups()
    return f"https://github.com/{owner}/{name}/compare/{base}...{branch}?expand=1"
