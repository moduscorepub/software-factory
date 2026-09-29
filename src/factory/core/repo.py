"""Thin git helpers. Everything the factory knows about a change comes from here."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(root: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


def toplevel(start: Path) -> Path | None:
    proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start, capture_output=True, text=True)
    return Path(proc.stdout.strip()) if proc.returncode == 0 else None


def current_branch(root: Path) -> str:
    return git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip()


def head_sha(root: Path) -> str:
    return git(root, "rev-parse", "HEAD").strip()


def resolve(root: Path, ref: str) -> str:
    return git(root, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def merge_base(root: Path, base: str, head: str = "HEAD") -> str:
    """Merge-base of head with base, preferring the remote-tracking branch when it exists."""
    for candidate in (f"origin/{base}", base):
        out = git(root, "merge-base", head, candidate, check=False).strip()
        if out:
            return out
    return resolve(root, head)


def is_dirty(root: Path) -> bool:
    return bool(git(root, "status", "--porcelain").strip())


def untracked(root: Path) -> list[str]:
    return [p for p in git(root, "ls-files", "--others", "--exclude-standard").splitlines() if p]


def files(root: Path) -> list[str]:
    """Tracked plus untracked, non-ignored files."""
    tracked = [p for p in git(root, "ls-files").splitlines() if p]
    return sorted(set(tracked) | set(untracked(root)))


def changed_files(root: Path, base: str, head: str | None = None) -> list[str]:
    """Files changed from base to head; head=None means the working tree including untracked files."""
    if head:
        out = git(root, "diff", "--name-only", "--diff-filter=d", base, head)
        return sorted(p for p in out.splitlines() if p)
    out = git(root, "diff", "--name-only", "--diff-filter=d", base)
    return sorted({p for p in out.splitlines() if p} | set(untracked(root)))


def diff(root: Path, base: str, head: str | None = None) -> str:
    return git(root, "diff", base, *([head] if head else []))


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def added_lines(root: Path, base: str, head: str | None = None) -> list[tuple[str, int, str]]:
    """(path, line, text) for every line added from base to head (or working tree + untracked)."""
    out: list[tuple[str, int, str]] = []
    raw = git(root, "diff", "-U0", "--no-color", "--diff-filter=d", base, *([head] if head else []))
    path, line = "", 0
    for row in raw.splitlines():
        if row.startswith("+++ "):
            path = row[6:] if row.startswith("+++ b/") else ""
        elif m := _HUNK.match(row):
            line = int(m.group(1))
        elif row.startswith("+") and path:
            out.append((path, line, row[1:]))
            line += 1
    if head is None:
        for rel in untracked(root):
            try:
                text = (root / rel).read_text()
            except (UnicodeDecodeError, OSError):
                continue
            out.extend((rel, i, t) for i, t in enumerate(text.splitlines(), 1))
    return out


def log(root: Path, rev_range: str, grep: str = "") -> list[dict]:
    """Commits in rev_range as dicts with sha, subject, body."""
    args = ["log", "--format=%H%x1f%s%x1f%b%x1e", rev_range]
    if grep:
        args += ["-E", f"--grep={grep}"]
    out = git(root, *args, check=False)
    commits = []
    for rec in out.split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) == 3:
            commits.append({"sha": parts[0], "subject": parts[1], "body": parts[2]})
    return commits
