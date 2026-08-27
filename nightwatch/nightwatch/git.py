from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitSnapshot:
    root: str
    head: str | None
    branch: str | None
    status: str
    conflicts: bool

    @property
    def clean(self) -> bool:
        return not self.status.strip()

    def to_dict(self) -> dict[str, object]:
        return {
            "root": self.root,
            "head": self.head,
            "branch": self.branch,
            "status": self.status,
            "conflicts": self.conflicts,
            "clean": self.clean,
        }


CONFLICT_CODES = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}


def _run(args: list[str], cwd: str | Path, timeout: float = 5.0) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {' '.join(args)} failed") from exc
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed")
    return result.stdout.strip()


def repo_root(cwd: str | Path) -> Path:
    return Path(_run(["rev-parse", "--show-toplevel"], cwd)).resolve()


def snapshot(root: str | Path) -> GitSnapshot:
    root = Path(root).resolve()
    status = _run(["status", "--short"], root)
    try:
        head = _run(["rev-parse", "HEAD"], root)
    except GitError:
        head = None
    try:
        branch = _run(["symbolic-ref", "--short", "HEAD"], root)
    except GitError:
        branch = "(detached)"
    conflicts = any(line[:2] in CONFLICT_CODES for line in status.splitlines() if line)
    return GitSnapshot(str(root), head, branch, status, conflicts)


def is_ancestor(ancestor: str, descendant: str, root: str | Path) -> bool:
    try:
        _run(["merge-base", "--is-ancestor", ancestor, descendant], root)
        return True
    except GitError:
        return False


def diff_check(root: str | Path) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git", "diff", "--check"],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"git diff --check failed: {type(exc).__name__}"
    return result.returncode == 0, result.stdout.strip()
