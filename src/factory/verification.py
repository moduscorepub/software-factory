"""The PR evidence machine: deterministic layer, semantic layer, shadow tests, reducer, report.

obligations (Work Contract acceptance) must be a subset of proven evidence (annotated, passing tests),
and no finding may reach the blocking rungs of the evidence ladder.
"""

from __future__ import annotations

from factory import llm
from factory.core import constitution, repo, traceability, workflow
from factory.core import contract as wc
from factory.core.contract import Task, WorkContract
from factory.core.evidence import E, Finding, Report
from factory.core.state import Project
from factory.reviewers import reducer, semantic
from factory.runtime import commands, shadow_tests

Bound = list[tuple[WorkContract, Task, str]]


def bind(project: Project, base: str, head: str | None, texts: list[str], use_active: bool) -> Bound:
    """Tasks this change claims to implement, from branch, PR text and commit messages."""
    rev = f"{base}..{head or 'HEAD'}"
    texts = [*texts, repo.current_branch(project.root)]
    texts += [f"{c['subject']}\n{c['body']}" for c in repo.log(project.root, rev)]
    bound = traceability.bound_tasks(project, texts)
    if use_active and (active := workflow.bound(project)):
        if not any(c.work_id == active[0].work_id and t.id == active[1].id for c, t, _ in bound):
            bound.append(active)
    return bound


def deterministic(project: Project, base: str, head: str | None, bound: Bound):
    root = project.root
    findings: list[Finding] = []
    changed = repo.changed_files(root, base, head)

    for path, c in wc.all_contracts(project):
        rel = path.relative_to(root).as_posix()
        if c.tampered():
            findings.append(Finding(
                check="contracts", source="deterministic", evidence=E.E2, refs=[c.work_id],
                title=f"approved contract {c.ref} was modified after approval (hash mismatch)",
                detail=f"{rel}: expected {c.approval.contract_hash}, got {c.body_hash()}",
            ))
        elif c.approved and (problems := c.problems()):
            findings.append(Finding(
                check="contracts", source="deterministic", evidence=E.E2, refs=[c.work_id],
                title=f"approved contract {c.ref} fails integrity checks", detail="; ".join(problems),
            ))
    if project.config.require_contract and not bound:
        findings.append(Finding(
            check="contracts", source="deterministic", evidence=E.E2,
            title="change is not bound to an approved Work Contract task",
            detail="Reference WORK-ID/T-ID or the Jira key in the branch name, PR or commit messages.",
        ))
    refs = traceability.test_refs(project)
    for c, task, _ in bound:
        criteria = {a.id: a for a in c.acceptance}
        for acc in traceability.missing(c, task, refs):
            findings.append(Finding(
                check="contracts", source="deterministic", evidence=E.E2, refs=[acc, f"{c.work_id}/{acc}"],
                title=f"{c.work_id}/{acc} has no proving test",
                detail=f"obligation from {task.id}: then {criteria[acc].then}. "
                       f"Annotate the proving test with `covers {c.work_id}/{acc}`.",
            ))

    runs, failures = commands.run_configured(project.config.commands.model_dump(), root)
    findings += failures

    laws = constitution.load(project.constitution_path).laws
    law_runs, violations = constitution.enforce(
        root, [law for law in laws if law.enforcement in ("command", "test", "static")], base, head, changed
    )
    runs += law_runs
    findings += violations

    if spots := workflow.placeholders(project, base, head):
        findings.append(Finding(
            check="review", source="deterministic", evidence=E.E1, locations=spots,
            title=f"{len(spots)} placeholder marker(s) introduced (TODO/FIXME/NotImplementedError)",
        ))
    return runs, findings


def review(project: Project, base: str, head: str, subject: str, bound: Bound,
           ai: bool = True, shadow: bool = True, log=print) -> Report:
    log("deterministic layer: contracts, commands, constitution")
    runs, findings = deterministic(project, base, head, bound)
    status = "disabled (--no-ai)"
    if ai and not llm.available():
        status = "skipped: claude CLI not available"
    elif ai:
        laws = constitution.load(project.constitution_path).laws
        changed = repo.changed_files(project.root, base, head)
        log(f"semantic layer: {', '.join(semantic.REVIEWERS)} (parallel)")
        sem, failures = semantic.run(
            project.root, base, head, repo.diff(project.root, base, head), changed, bound, laws,
            project.config.commands.model_dump(), project.config.model,
        )
        if failures and not sem and len(failures) >= len(semantic.REVIEWERS) - 1:
            status = "failed: " + "; ".join(failures)
        else:
            status = "ran" + (f" (reviewer failures: {'; '.join(failures)})" if failures else "")
            if shadow:
                n = sum(1 for f in sem if f.reproduction)
                log(f"shadow tests: executing {n} reproduction(s) in ephemeral worktrees")
                sem = shadow_tests.verify(project.root, sem, base, head, project.config.commands.test_file)
        findings += sem
    return Report(
        subject=subject, base=base, head=head,
        contracts=[f"{c.work_id}/{t.id}" + (f" ({j})" if j else "") for c, t, j in bound],
        ran=runs, findings=reducer.reduce(findings), semantic=status,
    )
