"""Run configured project commands and turn failures into E2 evidence."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from factory.core.evidence import CommandRun, E, Finding

ORDER = ("build", "lint", "typecheck", "test")


def run(name: str, command: str, cwd: Path, timeout: int = 1800, env: dict[str, str] | None = None) -> CommandRun:
    start = time.monotonic()
    # Project commands run in the project's environment, never the factory's own virtualenv.
    base = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    try:
        proc = subprocess.run(
            command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            env={**base, **(env or {})},
        )
        code, output = proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        code, output = 124, f"timed out after {timeout}s\n{exc.stdout or ''}"
    tail = "\n".join(output.strip().splitlines()[-60:])
    return CommandRun(name=name, command=command, exit_code=code, seconds=time.monotonic() - start, output=tail)


def run_configured(commands: dict[str, str], cwd: Path) -> tuple[list[CommandRun], list[Finding]]:
    runs, findings = [], []
    for name in ORDER:
        if cmd := commands.get(name):
            result = run(name, cmd, cwd)
            runs.append(result)
            if not result.ok:
                findings.append(
                    Finding(
                        check="tests",
                        source="deterministic",
                        title=f"{name} failed: `{cmd}` exited {result.exit_code}",
                        evidence=E.E2,
                        output=result.output,
                    )
                )
    return runs, findings
