"""Semantic reviewers. Each is one focused model pass that must emit findings in the evidence schema.

The model may only claim E0 or E1. E2-E4 are assigned by the factory after something executes.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from factory import llm
from factory.core import constitution
from factory.core.contract import Task, WorkContract
from factory.core.evidence import E, Finding, Location, Reproduction

REVIEWERS = {
    "behaviour": (
        "review",
        "You are the BEHAVIOUR REVIEWER. For each acceptance criterion in scope, trace the implementation "
        "path that claims to satisfy it, citing file:line hops. Report every criterion that is unsatisfied, "
        "partially satisfied, or satisfied only on the happy path. When a criterion is violated, write a "
        "reproduction test that encodes the criterion with its concrete values.",
    ),
    "regression": (
        "review",
        "You are the REGRESSION ADVERSARY. What existing behaviour can this diff unintentionally alter? "
        "Compare the base and head versions of every changed function (`git show {base}:<path>`). Hunt for "
        "quiet semantic changes: boundaries, rounding, ordering, defaults, error types and messages, "
        "None/empty handling, units, time zones, caching. For each suspected regression write a "
        "differential reproduction: a test pinning the BASE behaviour (it must pass on base) with "
        "differential=true.",
    ),
    "tests": (
        "review",
        "You are the TEST ADVERSARY. Which meaningful failure paths of the changed code remain untested? "
        "Report only paths where a defect is plausible and consequential. Where you believe such a path "
        "is actually broken, write a reproduction test.",
    ),
    "constitution": (
        "constitution",
        "You are the ARCHITECTURE AND CONSTITUTION REVIEWER. For each applicable law listed below, decide "
        "compliance or a concrete violation with file:line on both sides. Report violations only.",
    ),
}

RULES = """\
Evidence rules (strict):
- E1 only when you cite file:line locations that demonstrate the problem; otherwise E0.
- Never claim E2, E3 or E4: the factory assigns those only after executing a reproduction.
- A reproduction of kind "test" is a complete, self-contained test file at a NEW path in the project's
  test directory, named test_factory_shadow_<slug> with the project's test file extension, written for
  the project's existing test framework and imports. It PASSES when behaviour is correct and FAILS when
  the defect exists. Leave `command` empty unless the default single-file runner cannot run it.
- A reproduction of kind "command" is a shell command that exits non-zero if and only if the defect
  exists (runtime reproduction).
- differential=true only when the behaviour existed before this change, so the reproduction must pass on
  base.
- refs: acceptance ids (A-xxx), requirement ids (R-xxx) or law ids (C-...) the finding concerns.
- No style, naming or formatting comments. Findings concern behaviour, contracts or law.
- At most 6 findings. No findings is a valid, good answer."""

FINDINGS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "detail", "evidence", "refs", "locations", "reproduction"],
                "properties": {
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "evidence": {"type": "string", "enum": ["E0", "E1"]},
                    "refs": {"type": "array", "items": {"type": "string"}},
                    "locations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["path", "line"],
                            "properties": {"path": {"type": "string"}, "line": {"type": "integer"}},
                        },
                    },
                    "reproduction": {
                        "anyOf": [
                            {"type": "null"},
                            {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["kind", "path", "code", "command", "differential"],
                                "properties": {
                                    "kind": {"type": "string", "enum": ["test", "command"]},
                                    "path": {"type": "string"},
                                    "code": {"type": "string"},
                                    "command": {"type": "string"},
                                    "differential": {"type": "boolean"},
                                },
                            },
                        ]
                    },
                },
            },
        }
    },
}

MAX_DIFF = 80_000


def _scope(bound: list[tuple[WorkContract, Task, str]]) -> str:
    if not bound:
        return "(no Work Contract bound to this change; review against the diff's evident intent)"
    blocks = []
    for c, task, jira in bound:
        blocks.append(yaml.safe_dump({
            "contract": c.ref, "task": f"{task.id} {jira}".strip(), "title": task.title,
            "outcome": c.intent.outcome, "non_goals": c.intent.non_goals,
            "requirements": [r.model_dump() for r in c.requirements if r.id in task.requirements],
            "acceptance": [a.model_dump() for a in c.criteria(task)],
            "invariants": [i.model_dump() for i in c.invariants],
            "decisions": [d.decision for d in c.decisions],
        }, sort_keys=False, allow_unicode=True, width=100))
    return "\n".join(blocks)


def run(tree: Path, base: str, head: str, diff: str, changed: list[str],
        bound: list[tuple[WorkContract, Task, str]], laws: list[constitution.Law],
        commands: dict[str, str], model: str = "") -> tuple[list[Finding], list[str]]:
    """Run all reviewers in parallel. Returns (findings, failure notes)."""
    if len(diff) > MAX_DIFF:
        diff = diff[:MAX_DIFF] + "\n... [diff truncated; read files directly]"
    applicable = [law for law in laws if law.enforcement in ("llm", "test") and law.applies(changed)]
    shared = f"""BASE {base}  HEAD {head}. The working directory is the HEAD tree; use Read/Grep/Glob and
`git show {base}:<path>` / `git diff {base} {head}` to inspect.

WORK CONTRACT SCOPE:
{_scope(bound)}
APPLICABLE CONSTITUTION LAWS:
{constitution.render(applicable) or '(none)'}
PROJECT COMMANDS: test=`{commands.get('test', '')}` single-file test=`{commands.get('test_file', '')}`
CHANGED FILES: {', '.join(changed)}

DIFF:
```diff
{diff}
```

{RULES}"""

    def one(name: str) -> tuple[str, list[Finding] | str]:
        check, role = REVIEWERS[name]
        try:
            out = llm.ask(
                role.format(base=base[:12]) + "\n\n" + shared, FINDINGS_SCHEMA, tree,
                tools="Read,Grep,Glob,Bash", allowed="Bash(git show:*) Bash(git diff:*) Bash(git log:*)",
                model=model,
            )
        except llm.LLMError as exc:
            return name, str(exc)
        findings = []
        for raw in out["findings"]:
            rep = raw.get("reproduction")
            findings.append(Finding(
                check=check, source=name, title=raw["title"], detail=raw["detail"], evidence=E(raw["evidence"]),
                refs=raw["refs"], locations=[Location(**loc) for loc in raw["locations"]],
                reproduction=Reproduction(**rep) if rep else None,
            ))
        return name, findings

    names = [n for n in REVIEWERS if n != "constitution" or applicable]
    findings: list[Finding] = []
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        for name, result in pool.map(one, names):
            if isinstance(result, str):
                failures.append(f"{name}: {result}")
            else:
                findings += result
    return findings, failures
