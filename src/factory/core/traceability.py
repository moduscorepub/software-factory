"""Requirement -> acceptance -> task/Jira -> commit -> file:line -> test -> evidence.

Tests prove acceptance criteria by carrying `covers <WORK-ID>/<A-ID>` (in a docstring or comment next to
the test). Commits carry `Factory-Task: <WORK-ID>/<T-ID>` or the Jira key.
"""

from __future__ import annotations

import re
from collections import defaultdict

from rich.tree import Tree

from factory.core import contract as wc
from factory.core import repo
from factory.core.constitution import matches
from factory.core.contract import Task, WorkContract
from factory.core.state import Project

ACC_REF = re.compile(r"\b([A-Z][A-Z0-9]*-\d+)/(A-\d{3})\b")
_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)|^\s*(?:it|test)\(\s*['\"]([^'\"]+)|^\s*func\s+(Test\w+)")


def _test_name(rel: str, lines: list[str], i: int) -> str:
    for j in (i, i - 1, i - 2, i - 3, i + 1, i + 2, i + 3):
        if 0 <= j < len(lines) and (m := _TEST_DEF.match(lines[j])):
            return f"{rel}::{next(g for g in m.groups() if g)}"
    return f"{rel}:{i + 1}"


def test_refs(project: Project) -> dict[str, list[str]]:
    """'WORK/A-001' -> tests that claim to prove it."""
    out: dict[str, list[str]] = defaultdict(list)
    for rel in repo.files(project.root):
        if not matches(rel, project.config.test_globs):
            continue
        try:
            lines = (project.root / rel).read_text().splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(lines):
            for m in ACC_REF.finditer(line):
                name = _test_name(rel, lines, i)
                key = f"{m.group(1)}/{m.group(2)}"
                if name not in out[key]:
                    out[key].append(name)
    return out


def missing(c: WorkContract, task: Task, refs: dict[str, list[str]]) -> list[str]:
    return [a for a in task.acceptance if not refs.get(f"{c.work_id}/{a}")]


def bound_tasks(project: Project, texts: list[str]) -> list[tuple[WorkContract, Task, str]]:
    """Tasks referenced by branch names, PR text or commit messages (WORK/T-001 or Jira key)."""
    blob = "\n".join(texts)
    found: dict[str, tuple[WorkContract, Task, str]] = {}
    refs = {f"{m.group(1)}/{m.group(2)}" for m in wc.TASK_REF.finditer(blob)}
    refs |= {m.group(0).upper() for m in re.finditer(r"\b[A-Za-z][A-Za-z0-9]*-\d+-t-\d{3}\b", blob)}
    for work_id in wc.work_ids(project):
        links = wc.load_links(project, work_id)
        for task_id, key in (links.tasks.items() if links else []):
            if re.search(rf"\b{re.escape(key)}\b", blob):
                refs.add(f"{work_id}/{task_id}")
    for ref in refs:
        ref = re.sub(r"-(T-\d{3})$", r"/\1", ref)  # branch form WORK-1-T-001
        try:
            found[ref] = wc.resolve_task(project, ref)
        except wc.ContractError:
            continue
    return [found[k] for k in sorted(found)]


def commits(project: Project, c: WorkContract, task: Task, jira: str) -> list[dict]:
    pattern = re.escape(f"{c.work_id}/{task.id}") + (f"|{re.escape(jira)}" if jira else "")
    out = []
    for commit in repo.log(project.root, "--all", grep=pattern):
        try:
            added = repo.added_lines(project.root, f"{commit['sha']}^", commit["sha"])
        except repo.GitError:
            added = []
        first: dict[str, int] = {}
        for path, line, _ in added:
            first.setdefault(path, line)
        commit["files"] = [f"{p}:{n}" for p, n in first.items()]
        out.append(commit)
    return out


def tree(project: Project, c: WorkContract, tasks: list[Task]) -> Tree:
    refs = test_refs(project)
    evidence = project.evidence()
    links = wc.load_links(project, c.work_id)
    root = Tree(f"[bold]{c.ref}[/] {c.title} [dim]{c.approval.status} {c.approval.contract_hash[:19]}[/]")
    if links and links.confluence_url:
        root.add(f"confluence {links.confluence_url} (v{links.confluence_version})")
    for task in tasks:
        jira = links.tasks.get(task.id, "") if links else ""
        node = root.add(f"[bold cyan]{task.id}[/]{' / ' + jira if jira else ''} {task.title}")
        for req_id in task.requirements:
            req = next((r for r in c.requirements if r.id == req_id), None)
            rnode = node.add(f"[yellow]{req_id}[/] {req.statement if req else '(unknown)'}")
            for acc in [a for a in c.acceptance if a.requirement == req_id and a.id in task.acceptance]:
                anode = rnode.add(f"[green]{acc.id}[/] then {acc.then}")
                tests = refs.get(f"{c.work_id}/{acc.id}", [])
                for t in tests:
                    anode.add(f"test {t}")
                if not tests:
                    anode.add("[red]no test annotated covers " + f"{c.work_id}/{acc.id}[/]")
                for e in evidence:
                    if acc.id in e.get("refs", []) or f"{c.work_id}/{acc.id}" in e.get("refs", []):
                        anode.add(f"evidence {e.get('id')} {e.get('evidence')} {e.get('title')} [dim]{e['subject']}[/]")
        cnode = node.add("commits")
        for commit in commits(project, c, task, jira):
            k = cnode.add(f"{commit['sha'][:10]} {commit['subject']}")
            for f in commit["files"]:
                k.add(f)
    return root
