"""Spec compiler: one strong model, several deterministic passes, one typed output.

intake + gap analysis -> clarification -> specification -> adversarial critique -> convergence
-> deterministic validation (with bounded repair). Human approval happens outside, in the CLI.
"""

from __future__ import annotations

from collections.abc import Callable

import yaml

from factory import llm
from factory.core import constitution, contract
from factory.core.contract import Decision, WorkContract
from factory.core.state import Project

TOOLS = "Read,Grep,Glob"

SYSTEM = (
    "You are the specification compiler of a software factory. You turn vague human intent into a "
    "precise, testable Work Contract that coding agents execute and PR verifiers check against. "
    "Interpret what the requester needs, not their literal wording. Never invent requirements the "
    "request does not imply; surface hidden decisions instead. Every requirement must be provable by "
    "an automated test or an explicit manual check. Prefer the smallest scope that achieves the outcome."
)

ANALYSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["interpreted_outcome", "repository_facts", "assumptions", "conflicts",
                 "hidden_decisions", "questions"],
    "properties": {
        "interpreted_outcome": {"type": "string"},
        "repository_facts": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "conflicts": {"type": "array", "items": {"type": "string"}},
        "hidden_decisions": {"type": "array", "items": {"type": "string"}},
        "questions": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["question", "why_it_matters", "options", "default"],
                "properties": {
                    "question": {"type": "string"},
                    "why_it_matters": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}, "minItems": 2},
                    "default": {"type": "string"},
                },
            },
        },
    },
}

CRITIQUE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["issues"],
    "properties": {
        "issues": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["category", "target", "problem", "fix"],
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["contradiction", "underspecified", "untestable", "architecture",
                                 "failure-case", "scope-expansion", "task-granularity", "regression-risk"],
                    },
                    "target": {"type": "string"},
                    "problem": {"type": "string"},
                    "fix": {"type": "string"},
                },
            },
        }
    },
}

CONTRACT_RULES = """\
Contract rules:
- title: short imperative name of the work.
- intent: problem (the current pain), outcome (the observable result), non_goals (explicit exclusions
  that stop scope creep).
- spec: fill each section that applies (constraints, scope, interfaces, failure_semantics, data,
  observability, security, compatibility, migration, rollout, rollback, test_obligations); use "" only
  when a section genuinely does not apply.
- requirements R-001...: one testable statement each, with rationale.
- acceptance A-001...: Given/When/Then with concrete values, each tracing to exactly one requirement;
  every requirement has at least one. Cover failure paths where bad input matters, and add criteria that
  protect existing behaviour the change could plausibly break.
- invariants: constitution laws (use their exact ids) this work must respect, source
  "project-constitution"; work-specific invariants use ids C-WORK-001... with source "work".
- decisions D-001...: every resolved question and every material choice you made, with rejected
  alternatives and rationale.
- tasks T-001...: the smallest independently verifiable units. Each has a one-sentence deliverable,
  requirements, acceptance (at least one), depends_on, likely_files (real repository paths where
  possible) and verification kinds (unit, integration, contract, e2e, manual).
- risks X-001... with mitigations.
- unknowns: only questions that block implementation; empty when safe assumptions (recorded as
  decisions) resolve them.
"""


def _yaml(data) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


def _context(project: Project, request: str, previous: WorkContract | None) -> str:
    laws = constitution.load(project.constitution_path).laws
    existing = []
    for work_id in contract.work_ids(project):
        c = contract.latest(project, work_id)
        existing.append(f"- {c.ref} [{c.approval.status}] {c.title}")
    parts = [
        f"REQUEST (verbatim; may be terse or sloppy):\n<<<\n{request}\n>>>",
        f"PROJECT CONSTITUTION:\n{constitution.render(laws) or '(none)'}",
        f"EXISTING WORK CONTRACTS:\n{chr(10).join(existing) or '(none)'}",
    ]
    if previous:
        parts.append(
            f"THIS REVISES {previous.ref}; the request describes the change. Keep unchanged ids stable.\n"
            + _yaml(previous.model_dump(mode="json", by_alias=True, exclude={"approval", "source"}))
        )
    try:
        from factory.adapters import atlassian

        pages = atlassian.search_confluence(project, request)
    except Exception as exc:  # noqa: BLE001 - optional context; compile without it
        pages = [f"(confluence search failed: {exc})"]
    if pages:
        parts.append("RELATED CONFLUENCE MATERIAL:\n" + "\n".join(pages))
    return "\n\n".join(parts)


def _pass(project: Project, prompt: str, schema: dict) -> dict:
    return llm.ask(prompt, schema, project.root, system=SYSTEM, tools=TOOLS, model=project.config.model)


def analyse(project: Project, request: str, previous: WorkContract | None = None) -> dict:
    return _pass(project, _context(project, request, previous) + """

Inspect the repository in the working directory (Read, Grep, Glob) to learn the code this request
touches. Then produce:
1. interpreted_outcome: what the requester actually needs.
2. repository_facts: facts that matter (paths, existing behaviour, conventions, tests).
3. assumptions: things that can safely be assumed without asking.
4. conflicts: where the request conflicts with the repository or the constitution.
5. hidden_decisions: architectural or product decisions buried in the request.
6. questions: ask ONLY where different answers materially change the implementation. Zero questions
   is a good answer. At most 5, each with 2-4 concrete options and a recommended default.
""", ANALYSIS_SCHEMA)


def clarify(analysis: dict, answer: Callable[[dict], str]) -> list[Decision]:
    decisions = []
    for i, q in enumerate(analysis["questions"], 1):
        choice = answer(q).strip() or q["default"]
        decisions.append(Decision(
            id=f"D-{i:03d}",
            decision=f"{q['question']} -> {choice}",
            alternatives_rejected=[o for o in q["options"] if o != choice],
            rationale=q["why_it_matters"],
        ))
    return decisions


def specify(project: Project, request: str, analysis: dict, decisions: list[Decision],
            previous: WorkContract | None) -> dict:
    resolved = _yaml([d.model_dump() for d in decisions]) if decisions else "(none)"
    return _pass(project, _context(project, request, previous) + f"""

ANALYSIS:
{_yaml(analysis)}
RESOLVED DECISIONS (include verbatim with these ids):
{resolved}
Compile the Work Contract body. You may inspect the repository to choose likely_files.

{CONTRACT_RULES}""", contract.body_schema())


def critique(project: Project, request: str, body: dict) -> dict:
    laws = constitution.load(project.constitution_path).laws
    return _pass(project, f"""You are an adversarial reviewer of a Work Contract. Attack it; do not
rewrite it. Inspect the repository to check any claim about existing code.

Find only material issues: contradictions, underspecified behaviour, impossible or untestable
acceptance criteria, architecture or constitution violations, unhandled failure cases, accidental scope
expansion beyond the request, tasks that are not independently verifiable, and existing behaviour the
work could break without an acceptance criterion protecting it. No issues is a valid answer.

ORIGINAL REQUEST:
<<<
{request}
>>>
CONSTITUTION:
{constitution.render(laws) or '(none)'}
CONTRACT:
{_yaml(body)}""", CRITIQUE_SCHEMA)


def converge(project: Project, request: str, body: dict, issues: dict, keep: list[Decision]) -> dict:
    ids = ", ".join(d.id for d in keep) or "none"
    return _pass(project, f"""Converge this Work Contract. Resolve the criticisms into the contract
instead of debating them: apply every fix that is correct; reject fixes that would expand scope beyond
the request (record a rejection as a decision only when it is material). Then re-compile tasks into the
smallest independently verifiable units. Keep ids stable where meaning is unchanged. Keep decisions
{ids} verbatim.

ORIGINAL REQUEST:
<<<
{request}
>>>
CONTRACT:
{_yaml(body)}
CRITICISMS:
{_yaml(issues)}

{CONTRACT_RULES}""", contract.body_schema())


def _problems(project: Project, c: WorkContract) -> list[str]:
    known = {law.id for law in constitution.load(project.constitution_path).laws}
    out = c.problems()
    for inv in c.invariants:
        if inv.source == "project-constitution" and inv.id not in known:
            out.append(f"invariant {inv.id} claims project-constitution but no such law exists")
    return out


def compile_contract(
    project: Project,
    request: str,
    answer: Callable[[dict], str],
    previous: WorkContract | None = None,
    log: Callable[[str], None] = print,
) -> tuple[WorkContract, dict, dict]:
    """Returns (draft contract, analysis, critique)."""
    work_id = previous.work_id if previous else contract.next_work_id(project)
    version = previous.version + 1 if previous else 1

    log("pass 1/5 intake + gap analysis")
    analysis = analyse(project, request, previous)
    log("pass 2/5 clarification")
    decisions = clarify(analysis, answer)
    log("pass 3/5 specification")
    body = specify(project, request, analysis, decisions, previous)
    log("pass 4/5 adversarial critique (fresh context)")
    issues = critique(project, request, body)
    log(f"pass 5/5 convergence ({len(issues['issues'])} criticisms)")
    body = converge(project, request, body, issues, decisions)

    for attempt in range(3):
        # Clarified answers are law: restore them verbatim whatever the model did.
        mine = {d.id for d in decisions}
        body["decisions"] = [d.model_dump() for d in decisions] + [
            d for d in body.get("decisions", []) if d.get("id") not in mine
        ]
        draft = WorkContract(work_id=work_id, version=version, **body)
        problems = _problems(project, draft)
        if not problems:
            return draft, analysis, issues
        if attempt == 2:
            break
        log(f"repair {attempt + 1}: {len(problems)} integrity problem(s)")
        body = _pass(project, "The Work Contract fails deterministic validation. Fix exactly these "
                     "problems and change nothing else:\n- " + "\n- ".join(problems)
                     + f"\n\nCONTRACT:\n{_yaml(body)}", contract.body_schema())
    raise contract.ContractError(problems)
