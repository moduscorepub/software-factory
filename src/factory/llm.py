"""The single model runtime: Claude Code in print mode with JSON-schema structured output.

Nested sessions set FACTORY_NESTED so the factory's own hooks stay silent inside them, and load no
user/project settings so installed plugins cannot leak into compiler or reviewer passes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


class LLMError(RuntimeError):
    pass


def available() -> bool:
    return shutil.which("claude") is not None


def ask(
    prompt: str,
    schema: dict,
    cwd: Path,
    *,
    system: str = "",
    tools: str = "",
    allowed: str = "",
    model: str = "",
    timeout: int = 1200,
) -> dict:
    """Run one pass and return the schema-validated object."""
    if not available():
        raise LLMError("`claude` CLI not found on PATH")
    cmd = [
        "claude", "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(schema),
        "--tools", tools,
        "--no-session-persistence",
        "--setting-sources", "",
    ]
    if system:
        cmd += ["--append-system-prompt", system]
    if model:
        cmd += ["--model", model]
    if allowed:
        cmd += [f"--allowedTools={allowed}"]
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env["FACTORY_NESTED"] = "1"
    try:
        proc = subprocess.run(
            cmd, input=prompt, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise LLMError(f"model pass timed out after {timeout}s") from exc
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise LLMError(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:500]}") from exc
    if data.get("is_error") or "structured_output" not in data:
        raise LLMError(f"model pass failed: {str(data.get('result') or data.get('subtype'))[:500]}")
    return data["structured_output"]
