"""Claude Code hook entrypoints: `factory hook <event>` reads the hook JSON on stdin.

Thin translation of factory.core.workflow into Claude Code's hook protocol. Hooks never break a
session: on internal error they log to stderr and let Claude continue.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from factory.core import constitution, workflow
from factory.core.state import Project


def _context(event: str, text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def session_start(project: Project, session: str, data: dict) -> dict | None:
    project.journal(session, "session_start", {"source": data.get("source", "")})
    b = workflow.bound(project)
    if b:
        return _context("SessionStart", workflow.brief(project, *b))
    laws = constitution.load(project.constitution_path).laws
    return _context("SessionStart", "This repository is bound to the software factory but no task is "
                    "active. Start one with `factory work <JIRA-KEY|WORK/T-ID>`.\n\nConstitution:\n"
                    + constitution.render(laws))


def prompt_submit(project: Project, session: str, data: dict) -> dict | None:
    b = workflow.bound(project)
    if not b:
        return None
    return _context("UserPromptSubmit", workflow.frame(project, *b, project.events(session)))


def pre_tool(project: Project, session: str, data: dict) -> dict | None:
    verdict = workflow.guard(project, data.get("tool_name", ""), data.get("tool_input") or {})
    if not verdict:
        return None
    decision, reason = verdict
    project.journal(session, "guard", {"tool": data.get("tool_name"), "decision": decision, "reason": reason})
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                   "permissionDecisionReason": reason}}


def post_tool(project: Project, session: str, data: dict) -> dict | None:
    tool, inp = data.get("tool_name", ""), data.get("tool_input") or {}
    if tool in workflow.EDIT_TOOLS:
        raw = Path(inp.get("file_path") or inp.get("notebook_path") or "")
        try:
            rel = (raw if raw.is_absolute() else project.root / raw).resolve().relative_to(project.root.resolve())
        except ValueError:
            return None
        project.journal(session, "edit", {"path": rel.as_posix()})
    elif tool == "Bash":
        cmd = inp.get("command", "")
        test_cmd = project.config.commands.test
        if workflow.TEST_CMD.search(cmd) or (test_cmd and test_cmd in cmd):
            resp = data.get("tool_response") or {}
            failed = bool(resp.get("interrupted")) or resp.get("exit_code", resp.get("exitCode", 0)) not in (0, None)
            project.journal(session, "test", {"command": cmd[:300], "failed": failed})
    elif tool in ("TodoWrite", "ExitPlanMode"):
        project.journal(session, "plan", {})
    return None


def stop(project: Project, session: str, data: dict) -> dict | None:
    b = workflow.bound(project)
    if not b:
        return None
    events = project.events(session)
    since = events
    for i, e in enumerate(events):
        if e["kind"] == "gate_pass":
            since = events[i + 1:]
    if not any(e["kind"] == "edit" for e in since):
        return None  # nothing changed since the last pass: no ceremony
    c, task, _ = b
    attempts = sum(1 for e in since if e["kind"] in ("gate_block", "reflect"))
    result = workflow.gate(project, c, task)
    if result.blocking:
        if attempts >= workflow.MAX_GATE_BLOCKS:
            project.journal(session, "gate_giveup", {"reasons": result.blocking})
            return {"systemMessage": f"factory gate: {c.work_id}/{task.id} is NOT complete after {attempts} "
                    "attempts:\n- " + "\n- ".join(result.blocking)}
        project.journal(session, "gate_block", {"reasons": result.blocking})
        return {"decision": "block", "reason": "factory stop gate: the task is not complete.\n- "
                + "\n- ".join(result.blocking) + "\nFix these, re-run the checks, then finish."}
    if not any(e["kind"] == "reflect" for e in since):
        project.journal(session, "reflect", {})
        return {"decision": "block", "reason": "factory REFLECT: deterministic checks pass. Answer each "
                "briefly and act on any problem before finishing:\n- " + "\n- ".join(result.reflect)}
    project.journal(session, "gate_pass", {"acceptance": task.acceptance})
    return {"systemMessage": f"factory gate passed: {c.work_id}/{task.id} has passing evidence for "
            f"{', '.join(task.acceptance)}."}


HANDLERS = {
    "session-start": session_start,
    "prompt-submit": prompt_submit,
    "pre-tool": pre_tool,
    "post-tool": post_tool,
    "stop": stop,
}


def main(event: str) -> None:
    if os.environ.get("FACTORY_NESTED"):
        return
    try:
        data = json.loads(sys.stdin.read() or "{}")
        project = Project.find(Path(data.get("cwd") or os.getcwd()))
        if project is None:
            return
        out = HANDLERS[event](project, data.get("session_id", ""), data)
    except Exception as exc:  # noqa: BLE001 - a hook must never wedge the session
        print(f"factory hook {event} error: {exc}", file=sys.stderr)
        return
    if out:
        print(json.dumps(out))
