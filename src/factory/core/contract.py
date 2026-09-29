"""The Work Contract: the typed, versioned intermediate representation every engine reads.

Confluence renders it, Jira projects its tasks, the harness executes it, the verifier proves it.
Approved versions are immutable; the approval hash covers everything except approval and source.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from factory.core.state import now

if TYPE_CHECKING:
    from factory.core.state import Project

SCHEMA_ID = "factory.work-contract/v0"
HEADER_FIELDS = ("schema", "work_id", "version", "source", "approval")


class ContractError(ValueError):
    def __init__(self, problems: list[str] | str):
        self.problems = [problems] if isinstance(problems, str) else problems
        super().__init__("; ".join(self.problems))


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Intent(_M):
    problem: str
    outcome: str
    non_goals: list[str] = Field(default_factory=list)


class Spec(_M):
    constraints: str = ""
    scope: str = ""
    interfaces: str = ""
    failure_semantics: str = ""
    data: str = ""
    observability: str = ""
    security: str = ""
    compatibility: str = ""
    migration: str = ""
    rollout: str = ""
    rollback: str = ""
    test_obligations: str = ""


class Requirement(_M):
    id: str
    statement: str
    rationale: str = ""


class Acceptance(_M):
    id: str
    requirement: str
    given: str
    when: str
    then: str


class Invariant(_M):
    id: str
    statement: str
    source: str = "project-constitution"


class Decision(_M):
    id: str
    decision: str
    alternatives_rejected: list[str] = Field(default_factory=list)
    rationale: str = ""


class Task(_M):
    id: str
    title: str
    deliverable: str = ""
    requirements: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    likely_files: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)


class Risk(_M):
    id: str
    description: str
    mitigation: str = ""


class Source(_M):
    confluence_page_id: str = ""
    confluence_version: int = 0


class Approval(_M):
    status: Literal["draft", "approved"] = "draft"
    contract_hash: str = ""
    approved_by: str = ""
    approved_at: str = ""


class WorkContract(_M):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: str = Field(SCHEMA_ID, alias="schema")
    work_id: str
    version: int = 1
    title: str
    source: Source = Field(default_factory=Source)
    intent: Intent
    spec: Spec = Field(default_factory=Spec)
    requirements: list[Requirement]
    acceptance: list[Acceptance]
    invariants: list[Invariant] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    tasks: list[Task]
    risks: list[Risk] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    approval: Approval = Field(default_factory=Approval)

    # --- identity ---------------------------------------------------------
    def body_hash(self) -> str:
        body = self.model_dump(mode="json", by_alias=True, exclude={"approval", "source"})
        return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    @property
    def ref(self) -> str:
        return f"{self.work_id}/v{self.version}"

    @property
    def approved(self) -> bool:
        return self.approval.status == "approved"

    def tampered(self) -> bool:
        return self.approved and self.approval.contract_hash != self.body_hash()

    # --- lookups ----------------------------------------------------------
    def task(self, task_id: str) -> Task:
        for t in self.tasks:
            if t.id == task_id:
                return t
        raise ContractError(f"{self.work_id} has no task {task_id}")

    def criteria(self, task: Task) -> list[Acceptance]:
        wanted = set(task.acceptance)
        return [a for a in self.acceptance if a.id in wanted]

    # --- integrity --------------------------------------------------------
    def problems(self) -> list[str]:
        out: list[str] = []
        groups = {
            "requirement": (self.requirements, r"R-\d{3}"),
            "acceptance": (self.acceptance, r"A-\d{3}"),
            "invariant": (self.invariants, r"C-[A-Z0-9]+(-[A-Z0-9]+)*"),
            "decision": (self.decisions, r"D-\d{3}"),
            "task": (self.tasks, r"T-\d{3}"),
            "risk": (self.risks, r"X-\d{3}"),
        }
        for name, (items, pattern) in groups.items():
            seen: set[str] = set()
            for item in items:
                if not re.fullmatch(pattern, item.id):
                    out.append(f"{name} id {item.id!r} must match {pattern}")
                if item.id in seen:
                    out.append(f"duplicate {name} id {item.id}")
                seen.add(item.id)
        if not self.requirements:
            out.append("contract has no requirements")
        if not self.tasks:
            out.append("contract has no tasks")

        reqs = {r.id for r in self.requirements}
        accs = {a.id for a in self.acceptance}
        tasks = {t.id: t for t in self.tasks}
        for a in self.acceptance:
            if a.requirement not in reqs:
                out.append(f"{a.id} references unknown requirement {a.requirement}")
        for r in sorted(reqs - {a.requirement for a in self.acceptance}):
            out.append(f"{r} has no acceptance criterion")
        covered: set[str] = set()
        for t in self.tasks:
            out += [f"{t.id} references unknown requirement {x}" for x in t.requirements if x not in reqs]
            out += [f"{t.id} references unknown acceptance {x}" for x in t.acceptance if x not in accs]
            out += [f"{t.id} depends on unknown task {x}" for x in t.depends_on if x not in tasks]
            if not t.acceptance:
                out.append(f"{t.id} has no acceptance criteria, so it is not independently verifiable")
            covered |= set(t.acceptance)
        for a in sorted(accs - covered):
            out.append(f"{a} is not covered by any task")

        state: dict[str, int] = {}

        def visit(tid: str, path: list[str]) -> None:
            if state.get(tid) == 2 or tid not in tasks:
                return
            if state.get(tid) == 1:
                out.append("task dependency cycle: " + " -> ".join([*path, tid]))
                return
            state[tid] = 1
            for dep in tasks[tid].depends_on:
                visit(dep, [*path, tid])
            state[tid] = 2

        for tid in tasks:
            visit(tid, [])
        return out

    def approve(self, by: str) -> WorkContract:
        issues = self.problems() + [f"unresolved unknown: {u}" for u in self.unknowns]
        if issues:
            raise ContractError(issues)
        approval = Approval(status="approved", contract_hash=self.body_hash(), approved_by=by, approved_at=now())
        return self.model_copy(update={"approval": approval})


def body_schema() -> dict:
    """JSON schema of the compiler-produced part of a contract (no header, no approval)."""
    schema = WorkContract.model_json_schema(by_alias=True)
    for name in HEADER_FIELDS:
        schema["properties"].pop(name, None)
    schema["required"] = [r for r in schema.get("required", []) if r not in HEADER_FIELDS]
    schema["$defs"] = {k: v for k, v in schema["$defs"].items() if k not in ("Approval", "Source")}
    return schema


# --- storage ----------------------------------------------------------------

def _dump(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100))


def work_dir(project: Project, work_id: str) -> Path:
    return project.contracts_dir / work_id


def load(path: Path) -> WorkContract:
    return WorkContract.model_validate(yaml.safe_load(path.read_text()))


def record_source(project: Project, c: WorkContract, page_id: str, page_version: int) -> WorkContract:
    """Stamp where an approved contract is published. Source is outside the hash, so approval holds."""
    updated = c.model_copy(update={"source": Source(confluence_page_id=page_id, confluence_version=page_version)})
    assert updated.body_hash() == c.body_hash()
    _dump(updated.model_dump(mode="json", by_alias=True), work_dir(project, c.work_id) / f"v{c.version}.yaml")
    return updated


def save(project: Project, contract: WorkContract) -> Path:
    path = work_dir(project, contract.work_id) / f"v{contract.version}.yaml"
    if path.exists() and load(path).approved:
        raise ContractError(f"{contract.ref} is approved and immutable; revise to create a new version")
    _dump(contract.model_dump(mode="json", by_alias=True), path)
    return path


def versions(project: Project, work_id: str) -> list[WorkContract]:
    paths = work_dir(project, work_id).glob("v*.yaml")
    return sorted((load(p) for p in paths), key=lambda c: c.version)


def latest(project: Project, work_id: str, approved_only: bool = False) -> WorkContract:
    found = [c for c in versions(project, work_id) if c.approved or not approved_only]
    if not found:
        kind = "approved contract" if approved_only else "contract"
        raise ContractError(f"no {kind} {work_id}")
    return found[-1]


def work_ids(project: Project) -> list[str]:
    if not project.contracts_dir.is_dir():
        return []
    return sorted(p.name for p in project.contracts_dir.iterdir() if any(p.glob("v*.yaml")))


def all_contracts(project: Project) -> list[tuple[Path, WorkContract]]:
    out = []
    for work_id in work_ids(project):
        for path in sorted(work_dir(project, work_id).glob("v*.yaml")):
            out.append((path, load(path)))
    return out


def next_work_id(project: Project) -> str:
    prefix = project.config.work_prefix
    nums = [int(w.rsplit("-", 1)[1]) for w in work_ids(project) if re.fullmatch(rf"{prefix}-\d+", w)]
    return f"{prefix}-{max(nums, default=0) + 1}"


class Links(_M):
    """Where a contract version was projected: Confluence page and Jira keys per task."""

    work_id: str
    version: int
    confluence_page_id: str = ""
    confluence_url: str = ""
    confluence_version: int = 0
    epic_key: str = ""
    tasks: dict[str, str] = Field(default_factory=dict)


def load_links(project: Project, work_id: str) -> Links | None:
    path = work_dir(project, work_id) / "links.yaml"
    return Links.model_validate(yaml.safe_load(path.read_text())) if path.exists() else None


def save_links(project: Project, links: Links) -> None:
    _dump(links.model_dump(mode="json"), work_dir(project, links.work_id) / "links.yaml")


def jira_key(project: Project, work_id: str, task_id: str) -> str:
    links = load_links(project, work_id)
    return links.tasks.get(task_id, "") if links else ""


TASK_REF = re.compile(r"\b([A-Z][A-Z0-9]*-\d+)/(T-\d{3})\b")


def resolve_task(project: Project, ref: str) -> tuple[WorkContract, Task, str]:
    """Resolve a Jira key, WORK-ID/T-001, T-001 or single-task WORK-ID to an approved contract task."""
    if m := TASK_REF.fullmatch(ref):
        contract = latest(project, m.group(1), approved_only=True)
        return contract, contract.task(m.group(2)), jira_key(project, contract.work_id, m.group(2))
    for work_id in work_ids(project):
        links = load_links(project, work_id)
        for task_id, key in (links.tasks.items() if links else []):
            if key == ref:
                contract = latest(project, work_id, approved_only=True)
                return contract, contract.task(task_id), key
    if re.fullmatch(r"T-\d{3}", ref):
        hits = []
        for work_id in work_ids(project):
            try:
                contract = latest(project, work_id, approved_only=True)
            except ContractError:
                continue
            hits += [(contract, t) for t in contract.tasks if t.id == ref]
        if len(hits) == 1:
            contract, task = hits[0]
            return contract, task, jira_key(project, contract.work_id, task.id)
        if len(hits) > 1:
            raise ContractError(f"{ref} is ambiguous; use WORK-ID/{ref}")
    if ref in work_ids(project):
        contract = latest(project, ref, approved_only=True)
        if len(contract.tasks) == 1:
            task = contract.tasks[0]
            return contract, task, jira_key(project, ref, task.id)
        raise ContractError(f"{ref} has {len(contract.tasks)} tasks; name one, e.g. {ref}/{contract.tasks[0].id}")
    raise ContractError(f"cannot resolve {ref!r} to an approved contract task")
