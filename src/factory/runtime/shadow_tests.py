"""Shadow tests: turn a reviewer's hypothesis into an executed reproduction in ephemeral worktrees.

Nothing is committed. A reproduction that passes on head refutes the finding. One that fails on head
(and, for differential claims, passes on base) upgrades it to E3 (test) or E4 (runtime command).
"""

from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from factory.core import repo
from factory.core.evidence import CommandRun, E, Finding, Reproduction
from factory.runtime import commands

# ponytail: heuristic for "the reproduction itself is broken"; a per-runner exit-code map if noisy.
BROKEN = re.compile(
    r"ModuleNotFoundError|ImportError|SyntaxError|IndentationError|fixture '[^']+' not found|"
    r"collected 0 items|no tests ran|ERROR collecting|error: unrecognized arguments|command not found"
)


@contextmanager
def worktree(root: Path, sha: str) -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="factory-shadow-"))
    repo.git(root, "worktree", "add", "--detach", "--force", str(path), sha)
    try:
        yield path
    finally:
        repo.git(root, "worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(path, ignore_errors=True)
        repo.git(root, "worktree", "prune", check=False)


def _execute(tree: Path, rep: Reproduction, test_file_cmd: str) -> CommandRun:
    target = None
    if rep.kind == "test":
        target = tree / rep.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rep.code)
        cmd = rep.command or test_file_cmd.format(path=rep.path)
    else:
        cmd = rep.command
    try:
        # Reproductions may reuse a path within one mtime tick; a stale .pyc would run the old test.
        return commands.run("shadow", cmd, tree, timeout=600, env={"PYTHONDONTWRITEBYTECODE": "1"})
    finally:
        if target is not None:
            target.unlink(missing_ok=True)


def _usable(rep: Reproduction, head_tree: Path, test_file_cmd: str) -> str:
    """Empty string when runnable, else the reason it is not."""
    if rep.kind == "command":
        return "" if rep.command.strip() else "empty command"
    p = Path(rep.path)
    if not rep.path or p.is_absolute() or ".." in p.parts:
        return f"unsafe test path {rep.path!r}"
    if (head_tree / p).exists():
        return f"test path {rep.path} already exists"
    if not (rep.command or test_file_cmd):
        return "no commands.test_file configured"
    return ""


def verify(root: Path, findings: list[Finding], base: str, head: str, test_file_cmd: str) -> list[Finding]:
    todo = [f for f in findings if f.reproduction]
    if not todo:
        return findings
    with worktree(root, head) as head_tree, worktree(root, base) as base_tree:
        for f in todo:
            rep = f.reproduction
            if why := _usable(rep, head_tree, test_file_cmd):
                f.detail += f" [shadow test not run: {why}]"
                continue
            on_head = _execute(head_tree, rep, test_file_cmd)
            if on_head.ok:
                f.refuted = True
                f.detail += " [refuted: the shadow reproduction passes on head]"
                continue
            if BROKEN.search(on_head.output):
                f.detail += " [shadow reproduction is itself broken; evidence unchanged]"
                f.output = on_head.output
                continue
            if rep.differential:
                on_base = _execute(base_tree, rep, test_file_cmd)
                if not on_base.ok:
                    f.detail += " [inconclusive: the reproduction also fails on base]"
                    continue
                f.detail += " [reproduced: passes on base, fails on head]"
            else:
                f.detail += " [reproduced on head]"
            f.evidence = E.E3 if rep.kind == "test" else E.E4
            f.output = f"$ {on_head.command}\n{on_head.output}"
    return findings
