"""Evidence ladder, merge policy and the evidence report.

E0 hypothesis never blocks; E1 source-backed requires human review; E2+ blocks.
A deterministic constitutional law blocks from E1 because the rule itself is not an opinion.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from factory.core.state import now


class E(StrEnum):
    E0 = "E0"
    E1 = "E1"
    E2 = "E2"
    E3 = "E3"
    E4 = "E4"


LADDER = {
    E.E0: "hypothesis",
    E.E1: "source-backed",
    E.E2: "deterministic failure",
    E.E3: "adversarial reproduction",
    E.E4: "runtime reproduction",
}

Check = Literal["contracts", "tests", "constitution", "review"]
CHECKS: tuple[Check, ...] = ("contracts", "tests", "constitution", "review")


class Location(BaseModel):
    path: str
    line: int = 0

    def __str__(self) -> str:
        return f"{self.path}:{self.line}" if self.line else self.path


class Reproduction(BaseModel):
    """An executable form of a reviewer's hypothesis, run only in ephemeral worktrees."""

    kind: Literal["test", "command"]
    path: str = ""  # test file to create (kind=test)
    code: str = ""  # its content
    command: str = ""  # command to run; for tests defaults to commands.test_file
    differential: bool = False  # the behaviour predates the change, so it must pass on base


class Finding(BaseModel):
    id: str = ""
    check: Check
    source: str
    title: str
    detail: str = ""
    evidence: E
    refs: list[str] = Field(default_factory=list)
    locations: list[Location] = Field(default_factory=list)
    deterministic_law: bool = False
    reproduction: Reproduction | None = None
    output: str = ""
    refuted: bool = False

    @property
    def blocking(self) -> bool:
        if self.refuted:
            return False
        return self.evidence >= E.E2 or (self.deterministic_law and self.evidence >= E.E1)

    @property
    def review_required(self) -> bool:
        return not self.blocking and not self.refuted and self.evidence == E.E1


class CommandRun(BaseModel):
    name: str
    command: str
    exit_code: int
    seconds: float
    output: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class Report(BaseModel):
    subject: str
    base: str
    head: str
    contracts: list[str] = Field(default_factory=list)
    ran: list[CommandRun] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    semantic: str = "not run"
    generated_at: str = Field(default_factory=now)

    @property
    def blocking(self) -> bool:
        return any(f.blocking for f in self.findings)

    def checks(self) -> dict[str, tuple[str, str]]:
        """check -> (state, description). State 'skipped' means the check produced no evidence."""
        out: dict[str, tuple[str, str]] = {}
        for check in CHECKS:
            fs = [f for f in self.findings if f.check == check]
            if check == "review" and self.semantic != "ran":
                out[check] = ("skipped", self.semantic)
                continue
            blocking = [f for f in fs if f.blocking]
            review = [f for f in fs if f.review_required]
            if blocking:
                out[check] = ("failure", f"{len(blocking)} blocking: {blocking[0].title}"[:140])
            elif review:
                out[check] = ("success", f"{len(review)} source-backed finding(s) need human review")
            else:
                out[check] = ("success", "evidence complete")
        return out


def render_markdown(report: Report) -> str:
    verdict = "BLOCKED" if report.blocking else "PASS"
    lines = [
        "<!-- factory-review -->",
        f"## factory evidence report: {verdict}",
        "",
        f"**{report.subject}** `{report.base[:10]}..{report.head[:10]}`",
        f"Contracts: {', '.join(report.contracts) or 'none bound'}",
        "",
        "| check | state | detail |",
        "|---|---|---|",
    ]
    for check, (state, desc) in report.checks().items():
        lines.append(f"| factory/{check} | {state} | {desc} |")

    def block(title: str, findings: list[Finding], collapsed: bool = False) -> None:
        if not findings:
            return
        lines.extend(["", f"<details><summary>{title} ({len(findings)})</summary>" if collapsed else f"### {title}"])
        for f in findings:
            where = ", ".join(str(loc) for loc in f.locations[:4])
            refs = f" [{', '.join(f.refs)}]" if f.refs else ""
            lines.append("")
            lines.append(f"- **{f.id} {f.evidence} {LADDER[f.evidence]}**{refs} {f.title} (`{f.source}`)")
            if where:
                lines.append(f"  - at {where}")
            if f.detail:
                lines.append(f"  - {f.detail}")
            if f.output:
                lines.extend(["  ```", *("  " + ln for ln in f.output.strip().splitlines()[-25:]), "  ```"])
        if collapsed:
            lines.extend(["", "</details>"])

    block("Blocking", [f for f in report.findings if f.blocking])
    block("Needs human review", [f for f in report.findings if f.review_required])
    block("Hypotheses (never block)", [f for f in report.findings if f.evidence == E.E0 and not f.refuted], True)
    block("Refuted by shadow tests", [f for f in report.findings if f.refuted], True)
    if report.ran:
        lines.extend(["", "<details><summary>Commands executed</summary>", ""])
        lines += [f"- `{r.command}` exit {r.exit_code} ({r.seconds:.1f}s)" for r in report.ran]
        lines.extend(["", "</details>"])
    lines.extend(["", f"_semantic review: {report.semantic}; generated {report.generated_at}_"])
    return "\n".join(lines) + "\n"
