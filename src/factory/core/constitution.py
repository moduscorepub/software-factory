"""The project constitution: persistent, ID'd law, separate from any feature specification.

Enforcement classes:
  command  a shell `check` must exit 0              -> failure is E2
  static   a `forbid` regex must not appear in added lines of `applies_to` files -> E1, deterministic, blocks
  test     a test suite `check` enforces the law    -> failure is E2
  llm      judged by the constitution reviewer, with evidence
  human    listed for human sign-off, never automated
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from factory.core import repo
from factory.core.evidence import CommandRun, E, Finding, Location
from factory.runtime import commands

Enforcement = Literal["command", "static", "test", "llm", "human"]


class Law(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^C-[A-Z]+-\d{3}$")
    statement: str
    enforcement: Enforcement
    applies_to: list[str] = Field(default_factory=list)
    check: str = ""
    forbid: str = ""

    def applies(self, paths: list[str]) -> bool:
        return not self.applies_to or any(matches(p, self.applies_to) for p in paths)


class Constitution(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: str = Field("factory.constitution/v0", alias="schema")
    version: int = 1
    laws: list[Law]


def matches(path: str, globs: list[str]) -> bool:
    p = PurePosixPath(path)
    return any(p.full_match(g) or p.match(g) for g in globs)


def load(path: Path) -> Constitution:
    if not path.exists():
        return Constitution(laws=[])
    constitution = Constitution.model_validate(yaml.safe_load(path.read_text()))
    ids = [law.id for law in constitution.laws]
    if dupes := sorted({i for i in ids if ids.count(i) > 1}):
        raise ValueError(f"duplicate law ids: {dupes}")
    for law in constitution.laws:
        if law.enforcement in ("command", "test") and not law.check:
            raise ValueError(f"{law.id} is {law.enforcement}-enforced but has no check command")
        if law.enforcement == "static":
            if not law.forbid:
                raise ValueError(f"{law.id} is static-enforced but has no forbid pattern")
            re.compile(law.forbid)
    return constitution


def render(laws: list[Law]) -> str:
    return "\n".join(f"- {law.id} [{law.enforcement}] {law.statement}" for law in laws)


def enforce(
    root: Path, laws: list[Law], base: str, head: str | None, changed: list[str]
) -> tuple[list[CommandRun], list[Finding]]:
    """Run every deterministic law that applies to the change."""
    runs: list[CommandRun] = []
    findings: list[Finding] = []
    added = None
    for law in laws:
        if not law.applies(changed):
            continue
        if law.enforcement in ("command", "test"):
            result = commands.run(law.id, law.check, root)
            runs.append(result)
            if not result.ok:
                findings.append(Finding(
                    check="constitution", source="deterministic", evidence=E.E2, refs=[law.id],
                    deterministic_law=True, title=f"{law.id} check failed: {law.statement}",
                    output=result.output,
                ))
        elif law.enforcement == "static":
            if added is None:
                added = repo.added_lines(root, base, head)
            pattern = re.compile(law.forbid)
            hits = [
                Location(path=p, line=n) for p, n, text in added
                if (not law.applies_to or matches(p, law.applies_to)) and pattern.search(text)
            ]
            if hits:
                findings.append(Finding(
                    check="constitution", source="deterministic", evidence=E.E1, refs=[law.id],
                    deterministic_law=True, locations=hits, title=f"{law.id} violated: {law.statement}",
                    detail=f"added lines match forbidden pattern /{law.forbid}/",
                ))
    return runs, findings


DEFAULT = """\
schema: factory.constitution/v0
version: 1
# Persistent law. Amend only through an explicit, reviewed change to this file.
laws:
  - id: C-ARCH-001
    statement: Domain code must not depend on infrastructure adapters.
    enforcement: llm
  - id: C-TEST-001
    statement: Every acceptance criterion is proven by a test annotated `covers <WORK-ID>/<A-ID>`.
    enforcement: llm
  - id: C-TEST-003
    statement: Every defect fix requires a regression test demonstrating the former failure.
    enforcement: llm
  - id: C-API-004
    statement: Existing public API behaviour cannot change without an explicit compatibility decision.
    enforcement: llm
  - id: C-DATA-002
    statement: Schema migrations must be backwards-compatible during rolling deployment.
    enforcement: llm
    applies_to: ["**/migrations/**"]
  - id: C-OBS-003
    statement: New externally visible failure modes require observable structured telemetry.
    enforcement: llm
  - id: C-SEC-001
    statement: Secrets and private keys are never committed.
    enforcement: static
    forbid: "(AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36}|-----BEGIN [A-Z ]*PRIVATE KEY|xox[abpr]-[A-Za-z0-9-]{10,})"
  - id: C-REL-001
    statement: Production releases are approved by a human owner.
    enforcement: human
"""
