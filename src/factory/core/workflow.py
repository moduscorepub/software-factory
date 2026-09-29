"""Harness-neutral session discipline: phase machine, task context, tool guard and completion gate.

Harness integrations (Claude Code hooks today) only translate these into their own wire format.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from factory.core import constitution, repo, traceability
from factory.core import contract as wc
from factory.core.constitution import matches
from factory.core.contract import Task, WorkContract
from factory.core.evidence import CommandRun, Location
from factory.core.state import Project
from factory.runtime import commands

PHASES = ("UNDERSTAND", "PLAN", "IMPLEMENT", "VERIFY", "REFLECT", "COMPLETE")
NEXT = {
    "UNDERSTAND": "read the code this task touches and decide how each acceptance criterion will be proven",
    "PLAN": "settle the smallest change set and the test that proves each acceptance criterion",
    "IMPLEMENT": "make the change and add tests annotated `covers <WORK>/<A-ID>`",
    "VERIFY": "run the tests and checks; read failures, never hide them",
    "REFLECT": "resolve the gate findings, or justify departures from the approved plan",
    "COMPLETE": "commit with the Factory-Task trailer",
}
MAX_GATE_BLOCKS = 4


def phase(events: list[dict]) -> str:
    current = "UNDERSTAND"
    for e in events:
        kind = e["kind"]
        if kind == "plan" and current == "UNDERSTAND":
            current = "PLAN"
        elif kind == "edit":
            current = "IMPLEMENT"
        elif kind == "test" and current == "IMPLEMENT":
            current = "VERIFY"
        elif kind in ("gate_block", "reflect"):
            current = "REFLECT"
        elif kind == "gate_pass":
            current = "COMPLETE"
    return current


def bound(project: Project) -> tuple[WorkContract, Task, str] | None:
    b = project.binding()
    if not b:
        return None
    try:
        c = wc.latest(project, b["work"], approved_only=True)
        return c, c.task(b["task"]), b["jira"] or ""
    except wc.ContractError:
        return None


def trailer(c: WorkContract, task: Task, jira: str) -> str:
    return f"Factory-Task: {c.work_id}/{task.id}" + (f" ({jira})" if jira else "")


def brief(project: Project, c: WorkContract, task: Task, jira: str) -> str:
    """Everything the coding session must know: what, why, scope, acceptance, law, obligations."""
    reqs = {r.id: r for r in c.requirements}
    cmds = {k: v for k, v in project.config.commands.model_dump().items() if v and k != "test_file"}
    laws = constitution.load(project.constitution_path).laws
    links = wc.load_links(project, c.work_id)
    keys = links.tasks if links else {}
    lines = [
        f"# Factory task {c.work_id}/{task.id}{f' ({jira})' if jira else ''}: {task.title}",
        f"Contract {c.ref}, approved {c.approval.contract_hash[:19]}"
        + (f", spec {links.confluence_url}" if links and links.confluence_url else ""),
        "", "## Why", c.intent.outcome,
        "", "## Deliverable", task.deliverable or task.title,
        "", "## Scope",
        *[f"- {r}: {reqs[r].statement}" for r in task.requirements if r in reqs],
    ]
    if c.intent.non_goals:
        lines += ["Non-goals (do not build):", *[f"- {n}" for n in c.intent.non_goals]]
    for name in ("constraints", "interfaces", "failure_semantics", "compatibility"):
        if text := getattr(c.spec, name):
            lines.append(f"{name.replace('_', ' ').capitalize()}: {text}")
    lines += ["", f"## Acceptance criteria (each needs a passing test annotated `covers {c.work_id}/<A-ID>`)"]
    lines += [f"- {a.id} ({a.requirement}): given {a.given}; when {a.when}; then {a.then}" for a in c.criteria(task)]
    if c.invariants:
        lines += ["", "## Invariants", *[f"- {i.id}: {i.statement}" for i in c.invariants]]
    if c.decisions:
        lines += ["", "## Decisions already made (do not reopen)", *[f"- {d.id}: {d.decision}" for d in c.decisions]]
    if laws:
        lines += ["", "## Constitution", constitution.render(laws)]
    lines += [
        "", "## Verification obligations",
        f"- kinds: {', '.join(task.verification) or 'unit'}",
        *[f"- {k}: `{v}`" for k, v in cmds.items()],
    ]
    if task.likely_files:
        lines.append(f"- likely files: {', '.join(task.likely_files)}")
    if task.depends_on:
        lines.append(f"- depends on: {', '.join(keys.get(d, d) for d in task.depends_on)}")
    lines += [
        "", "## Discipline",
        "Work through UNDERSTAND -> PLAN -> IMPLEMENT -> VERIFY -> REFLECT -> COMPLETE, scaled to risk.",
        "Before you finish, the factory stop gate runs the project commands, requires an annotated passing",
        "test for every acceptance criterion, rejects TODO/FIXME placeholders, checks the constitution and",
        "asks you to reflect on scope. Commit with the trailer "
        f"`{trailer(c, task, jira)}`.",
    ]
    return "\n".join(lines)


def frame(project: Project, c: WorkContract, task: Task, jira: str, events: list[dict]) -> str:
    """Compact per-prompt execution context: the user speaks naturally, the harness adds discipline."""
    refs = traceability.test_refs(project)
    have = [a if refs.get(f"{c.work_id}/{a}") else f"{a} (no test yet)" for a in task.acceptance]
    current = phase(events)
    changed = repo.changed_files(project.root, project.base_ref())
    return "\n".join([
        f"[factory] task {c.work_id}/{task.id}{f' {jira}' if jira else ''}: {task.title}",
        f"phase {current}; next: {NEXT[current]}",
        f"acceptance: {', '.join(have)}",
        f"invariants: {', '.join(i.id for i in c.invariants) or 'none'}",
        f"changed files vs {project.config.base_branch}: {len(changed)}",
        "Interpret the request within this task's approved scope; say so if it asks for more.",
    ])


# --- PreToolUse guard ----------------------------------------------------------

_DESTRUCTIVE_GIT = re.compile(
    r"\bgit\s+(?:-C\s+\S+\s+)?(?:push\b[^|;&\n]*\s(?:--force(?:-with-lease)?|-f)\b|reset\s+--hard|"
    r"clean\s+-\w*f|checkout\s+(?:-f\s+)?--\s+\.|restore\s+(?:--\S+\s+)*\.(?:\s|$)|branch\s+-D\b|"
    r"stash\s+(?:drop|clear)|filter-branch|filter-repo|update-ref\s+-d|reflog\s+expire)"
)
_RM_RF = re.compile(r"\brm\s+(?:-\w+\s+)*-\w*[rR]\w*\s+(?:-\w+\s+)*(?:/|~|\$HOME|\.|\.\.|\*|\.git)/?(?:\s|$)")
_SECRET_READ = re.compile(
    r"\b(?:cat|less|more|head|tail|bat|base64|xxd|strings)\b[^|;&\n]*"
    r"(?:\.env\b(?!\.example|\.sample|\.template)|id_rsa|id_ed25519|\.pem\b|\.p12\b|\.netrc|\.npmrc|"
    r"\.pypirc|\.aws/credentials|\.ssh/)"
    r"|\bprintenv\b|^\s*env\s*$|\becho\s+[^|;&\n]*\$\{?\w*(?:TOKEN|SECRET|PASSWORD|API_KEY)"
)
_DEPENDENCY_CMD = re.compile(
    r"\b(?:pip3?\s+install\s+(?!-r\b|-e\b|\.)\S|uv\s+add\b|uv\s+pip\s+install\s+(?!-r\b|-e\b|\.)\S|"
    r"poetry\s+add\b|npm\s+(?:i|install|add)\s+(?!-)\S|yarn\s+add\b|pnpm\s+add\b|bun\s+add\b|"
    r"cargo\s+add\b|go\s+get\b|gem\s+install\b|bundle\s+add\b)"
)
SECRET_CONTENT = re.compile(
    r"AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"xox[abpr]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9_-]{32,}"
)
_SECRET_PATH = re.compile(r"(?:^|/)(?:\.env(?:\.(?!example|sample|template)[\w.-]+)?|id_rsa|id_ed25519|"
                          r"[^/]*\.pem|[^/]*\.p12|\.netrc|\.npmrc|\.pypirc)$")
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def guard(project: Project, tool: str, inp: dict) -> tuple[str, str] | None:
    """(decision, reason) where decision is 'deny' or 'ask'; None lets the tool run."""
    b = bound(project)
    if tool == "Bash":
        cmd = inp.get("command", "")
        if _DESTRUCTIVE_GIT.search(cmd):
            return "deny", ("factory: destructive git operation. It can destroy unrecoverable work; use a "
                            "non-destructive alternative (git revert, git stash, a new branch).")
        if _RM_RF.search(cmd):
            return "deny", "factory: recursive delete of a root, home, repository or wildcard path."
        if _SECRET_READ.search(cmd):
            return "deny", "factory: this would expose credentials in the session transcript."
        if _DEPENDENCY_CMD.search(cmd):
            return "ask", "factory: introduces a dependency the approved Work Contract does not mention."
        if b and re.search(r"\bgit\s+commit\b", cmd) and "Factory-Task:" not in cmd and not re.search(
            r"\s(?:-F|--file|--amend\s+--no-edit)\b", cmd
        ):
            return "deny", f"factory: commits for this task must carry the trailer `{trailer(*b)}`."
        return None
    if tool not in (*EDIT_TOOLS, "Read"):
        return None
    raw = inp.get("file_path") or inp.get("notebook_path") or ""
    if not raw:
        return None
    if _SECRET_PATH.search(raw):
        return "deny", "factory: credential file; keep secrets out of the agent's context and edits."
    if tool == "Read":
        return None
    content = inp.get("content") or inp.get("new_string") or "".join(
        e.get("new_string", "") for e in inp.get("edits", []) or []
    )
    if SECRET_CONTENT.search(content or ""):
        return "deny", "factory: the new content contains what looks like a credential."
    path = Path(raw)
    path = path if path.is_absolute() else project.root / path
    try:
        rel = path.resolve().relative_to(project.root.resolve()).as_posix()
    except ValueError:
        return None
    if re.fullmatch(r"\.factory/contracts/[^/]+/v\d+\.yaml", rel) and path.exists() and wc.load(path).approved:
        return "deny", "factory: approved Work Contracts are immutable. Revise with `factory spec --revise`."
    if rel == ".factory/constitution.yaml":
        return "deny", "factory: constitution amendments are explicit, human-reviewed changes, not agent edits."
    cfg = project.config
    if matches(rel, cfg.generated):
        return "deny", "factory: generated file. Change its source or generator instead."
    if matches(rel, cfg.migrations):
        task = b[1] if b else None
        scope = " ".join([*(task.verification if task else []), task.title if task else "",
                          task.deliverable if task else ""]).lower()
        if not task or not (matches(rel, task.likely_files) or "migration" in scope):
            return "ask", "factory: schema migration outside the task's approved scope."
    if matches(rel, cfg.dependency_manifests):
        return "ask", "factory: dependency manifest change; the approved contract lists no new dependency."
    return None


# --- Stop gate -------------------------------------------------------------------

PLACEHOLDER = re.compile(r"\b(?:TODO|FIXME|XXX|HACK)\b|\bNotImplementedError\b|\btodo!\(|\bunimplemented!\(")
TEST_CMD = re.compile(
    r"\b(?:pytest|go\s+test|cargo\s+test|(?:npm|yarn|pnpm|bun)\s+(?:run\s+)?test|jest|vitest|rspec|"
    r"mvn\s+test|gradle\s+test|dotnet\s+test)\b"
)


def placeholders(project: Project, base: str, head: str | None = None) -> list[Location]:
    return [
        Location(path=p, line=n) for p, n, text in repo.added_lines(project.root, base, head)
        if not p.startswith(".factory/") and PLACEHOLDER.search(text)
    ]


@dataclass
class Gate:
    blocking: list[str] = field(default_factory=list)
    reflect: list[str] = field(default_factory=list)
    ran: list[CommandRun] = field(default_factory=list)


def gate(project: Project, c: WorkContract, task: Task) -> Gate:
    """Deterministic completion check for the bound task, plus the questions REFLECT must answer."""
    result = Gate()
    root = project.root
    base = project.base_ref()
    changed = repo.changed_files(root, base)
    cmds = project.config.commands
    if not cmds.test:
        result.blocking.append("no test command configured (commands.test in .factory/config.yaml); "
                               "nothing can prove the work.")
    runs, failures = commands.run_configured(cmds.model_dump(), root)
    result.ran += runs
    for f in failures:
        result.blocking.append(f"{f.title}:\n" + "\n".join(f.output.splitlines()[-15:]))

    refs = traceability.test_refs(project)
    criteria = {a.id: a for a in c.acceptance}
    for acc in traceability.missing(c, task, refs):
        result.blocking.append(f"no test proves {acc} (then {criteria[acc].then}); add one annotated "
                               f"`covers {c.work_id}/{acc}`.")

    if spots := placeholders(project, base):
        result.blocking.append("placeholders introduced: " + ", ".join(str(s) for s in spots[:10]))

    laws = constitution.load(project.constitution_path).laws
    law_runs, violations = constitution.enforce(
        root, [law for law in laws if law.enforcement in ("command", "test", "static")], base, None, changed
    )
    result.ran += law_runs
    for v in violations:
        where = ", ".join(str(loc) for loc in v.locations[:5])
        result.blocking.append(f"{v.title}{' at ' + where if where else ''}")

    tests = project.config.test_globs
    if task.likely_files:
        outside = [p for p in changed if not p.startswith(".factory/") and not matches(p, tests)
                   and not matches(p, task.likely_files)]
        if outside:
            result.reflect.append(f"files outside the plan ({', '.join(task.likely_files)}): "
                                  f"{', '.join(outside)}. Are they required by {task.id}? Revert if not.")
    result.reflect.append("Does the diff add behaviour that no acceptance criterion specifies? Remove it, "
                          "or name it so the contract can be revised.")
    judged = [law for law in laws if law.enforcement in ("llm", "human") and law.applies(changed)]
    if judged:
        result.reflect.append("Check these laws against your diff:\n" + constitution.render(judged))
    return result
