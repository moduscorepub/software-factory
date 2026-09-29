# software-factory

A software-factory control plane. Not three AI agents: one compiler pipeline.

```
raw intent ──► Work Contract ──► Confluence + Jira ──► coding harness ──► PR evidence gate
            (spec compiler)      (projection)         (Claude Code)       (GitHub merge gate)
```

Everything hangs off one typed, versioned, hash-approved intermediate representation, the
**Work Contract**. The spec compiler produces it, Jira is a projection of its `tasks[]`, the Claude Code
harness executes it, and the PR verifier proves against it. Completion is simply:

```
obligations (acceptance criteria)  ⊆  proven evidence (executed, annotated tests; no blocking findings)
```

## Install

Requires Python 3.13+, git, [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (the model
runtime for the compiler, reviewers and coding sessions) and `gh` (for posting PR checks).

```bash
uv tool install git+https://github.com/moduscorepub/software-factory
factory install claude     # optional: enable the harness in every Claude Code session
factory doctor
```

## The loop

```bash
factory init --prefix PRICING                  # bind a repo: config, constitution, PR workflow
factory spec "need bulk discount on carts lol"  # compile intent -> Work Contract, clarify, approve
factory publish PRICING-1                       # Confluence page + Jira epic/task graph
factory work PRICING-1/T-001                    # Claude Code session bound to the task (or a Jira key)
factory verify                                  # local completion checks
factory review 183 --post                       # PR evidence machine -> factory/* status checks
factory trace PRICING-1                         # requirement -> test -> commit -> evidence
```

| command | what it does |
|---|---|
| `init` | Writes `.factory/config.yaml` (detected build/lint/typecheck/test commands), `.factory/constitution.yaml` and `.github/workflows/factory.yml`. `--protect` adds a GitHub ruleset requiring the `factory/*` checks. |
| `spec` | Spec compiler. `--revise WORK-ID` creates the next version, `--yes` accepts recommended answers, `--approve` approves. |
| `approve` | Human approval: validates integrity, freezes the content hash. Only approved contracts are executable. |
| `publish` | Confluence REST v2 page + Jira REST v3 epic, bulk-created tasks, dependency links and `factory.contract` issue properties. Dry run without credentials. |
| `work` | Binds the task, creates a branch, launches Claude Code with the factory plugin. `--headless` for print mode, `--bind` to only bind and print the task context. |
| `verify` | Deterministic checks on the working tree. |
| `review` | Deterministic layer + semantic reviewers + shadow tests + reducer. `--post` sets GitHub statuses and upserts the report comment. |
| `trace` | Traceability tree for a task or contract. |
| `doctor` | Diagnoses the harness, bindings and integrations. |
| `schema` | JSON schema for `work-contract`, `constitution` or `evidence`. |

## 1. Spec compiler

One strong model, several deterministic passes, one typed output (`factory/core/compiler.py`):

1. **Intake and gap analysis**: reads the repository (Read/Grep/Glob), the constitution, existing contracts
   and related Confluence pages; separates what must be known from what can safely be assumed.
2. **Clarification**: asks only questions whose answers materially change the implementation (at most five,
   zero is fine). Every answer is recorded verbatim as a decision.
3. **Specification**: problem, outcome, non-goals, constraints, interfaces, failure semantics, data,
   observability, security, compatibility, migration, rollout, rollback, test obligations; requirements,
   Given/When/Then acceptance, invariants, decisions, tasks, risks.
4. **Adversarial critique** in a fresh context: contradictions, untestable criteria, scope creep,
   constitution violations, unprotected existing behaviour.
5. **Convergence**: criticisms are resolved into the contract, not debated.
6. **Deterministic validation** with bounded repair: unique well-formed IDs, every requirement has
   acceptance, every acceptance is covered by a task, no dangling references, acyclic task graph, invariants
   cite real constitution laws.
7. **Human approval** freezes a `sha256` over everything except `approval` and `source`.

```yaml
schema: factory.work-contract/v0
work_id: PRICING-1
version: 1
title: Apply automatic 5% bulk discount to carts of 10+ units
intent: {problem: ..., outcome: ..., non_goals: [...]}
requirements: [{id: R-001, statement: ..., rationale: ...}]
acceptance: [{id: A-001, requirement: R-001, given: ..., when: ..., then: ...}]
invariants: [{id: C-API-004, statement: ..., source: project-constitution}]
decisions: [{id: D-001, decision: ..., alternatives_rejected: [...], rationale: ...}]
tasks: [{id: T-001, title: ..., deliverable: ..., requirements: [R-001], acceptance: [A-001],
         depends_on: [], likely_files: [pricing/cart.py], verification: [unit]}]
risks: [{id: X-001, description: ..., mitigation: ...}]
unknowns: []
approval: {status: approved, contract_hash: "sha256:...", approved_by: ..., approved_at: ...}
```

Contracts live in `.factory/contracts/<WORK-ID>/v<N>.yaml`, committed with the code. Approved versions are
immutable: `verify`/`review` fail on a hash mismatch and the harness refuses agent edits to them.

## 2. Constitution

Persistent law with stable IDs, separate from any feature spec (`.factory/constitution.yaml`):

| enforcement | how |
|---|---|
| `command` / `test` | `check` shell command must exit 0; failure is E2 and blocks |
| `static` | `forbid` regex must not appear in added lines of `applies_to` files; E1 but deterministic, so it blocks |
| `llm` | judged by the constitution reviewer, with evidence |
| `human` | listed for human sign-off, never automated |

Amendments are explicit, reviewed changes to that file; the coding agent is blocked from editing it.

## 3. Projection: Confluence and Jira

Confluence holds the readable approved spec (storage format: status panel, intent, specification sections,
requirement/acceptance/decision/task/risk tables, traceability). Jira stays clean: an epic per contract and one
task per contract task with a short **Why / Deliverable / Acceptance / Dependencies / Contract / Evidence
required** body; machine metadata goes into the `factory.contract` issue property, dependencies become
`Blocks` links. Re-publishing updates in place (`links.yaml` records page and issue keys).

```yaml
# .factory/config.yaml
atlassian:
  base_url: https://your-site.atlassian.net
  confluence_space_id: "123456"
  confluence_parent_id: ""
  jira_project: PRICING
```

Credentials come from `ATLASSIAN_EMAIL` and `ATLASSIAN_API_TOKEN`. Without them, `publish` writes the exact
payloads to `.factory/contracts/<WORK-ID>/publish/`.

## 4. Coding harness (Claude Code plugin)

`src/factory/harness/claude/plugin/` is a standard Claude Code plugin: commands (`/factory:work`,
`/factory:verify`, `/factory:review`), skills (implementation, testing, debugging, investigation,
refactoring), agents (investigator, verifier, reviewer) and hooks. The hooks are thin: the logic is
harness-neutral in `factory/core/workflow.py`, so other harnesses can reuse it.

| hook | behaviour |
|---|---|
| SessionStart | injects the bound task: why, deliverable, scope, acceptance, invariants, decisions, constitution, verification obligations, commit trailer |
| UserPromptSubmit | adds a compact frame: task, phase, acceptance with/without tests, invariants, repo state |
| PreToolUse | denies destructive git, recursive deletes of roots, credential exposure, secrets in content, edits to approved contracts, the constitution or generated files, and trailer-less commits; asks before new dependencies, manifest edits and out-of-scope migrations |
| PostToolUse | journals edits, test runs and plans into SQLite |
| Stop | runs the project commands, requires an annotated passing test for every acceptance criterion, rejects TODO/FIXME placeholders, enforces deterministic laws, then holds one REFLECT round on scope and judged laws before allowing completion |

The session moves through `UNDERSTAND -> PLAN -> IMPLEMENT -> VERIFY -> REFLECT -> COMPLETE`, derived from the
journal rather than demanded as ceremony. Tests prove criteria by carrying `covers <WORK-ID>/<A-ID>`; commits
carry `Factory-Task: <WORK-ID>/<T-ID>`.

## 5. PR evidence machine

```
PR diff + Work Contract + constitution
  ├─ deterministic: contract integrity/tamper, obligations ⊆ annotated tests, build/lint/typecheck/test,
  │                 command/static laws, placeholders
  ├─ semantic (parallel): behaviour reviewer, regression adversary, test adversary, constitution reviewer
  ├─ shadow tests: reviewer reproductions run in ephemeral head/base worktrees, never committed
  └─ reducer: dedupe, strongest evidence wins -> evidence report -> factory/* status checks
```

Every finding carries an evidence class. Models may only claim E0 or E1; E2 to E4 require execution.

| class | meaning | merge policy |
|---|---|---|
| E0 | hypothesis | never blocks |
| E1 | source-backed (file:line) | human review required; blocks only for deterministic laws |
| E2 | deterministic failure | blocks |
| E3 | shadow test reproduces it (differential claims must also pass on base) | blocks |
| E4 | runtime command reproduces it | blocks |

A reproduction that passes on head **refutes** its finding. Checks: `factory/contracts`, `factory/tests`,
`factory/constitution`, `factory/review`. GitHub enforces them through a ruleset (`factory init --protect`);
the factory never merges anything.

The generated workflow runs the composite action in this repository. Add an `ANTHROPIC_API_KEY` or
`CLAUDE_CODE_OAUTH_TOKEN` secret to enable the semantic layer in CI. Without one, the deterministic checks are
posted and `factory/review` is left unset, so a required `factory/review` keeps blocking until someone runs
`factory review <PR> --post` with model access.

## Vertical proof

The v0 acceptance test from the design, run for real in
[software-factory-demo](https://github.com/moduscorepub/software-factory-demo) (seeded from
`examples/pricing`):

1. `factory spec "need bulk discount on carts lol. like if ppl buy loads they get 5% off, 10+ items i think.
   promo codes still gotta work" --yes --approve` asked three material questions (how discounts stack, what
   counts as an item, compatibility), converged five adversarial criticisms and approved a contract with 7
   requirements, 12 Given/When/Then criteria and 1 task, in about three minutes.
2. `factory publish PRICING-1` rendered the Confluence page and Jira payloads (dry run: no Atlassian
   credentials were available).
3. `factory work PRICING-1/T-001 --headless` ran Claude Code under the plugin. The journal records the
   SessionStart context, every edit and test run, one REFLECT hold, then `gate_pass` with a proving test for
   all 12 criteria.
4. A subtle regression was committed on purpose: an empty-cart fast path that skips promo-code validation.
   All 15 tests still pass.
5. `factory review 1 --post`: the deterministic checks pass; all four semantic reviewers flagged the fast
   path independently; the regression adversary's differential shadow test passed on base and failed on head
   (E3), and the reducer merged the four into one finding. `factory/review` failed and
   [PR #1](https://github.com/moduscorepub/software-factory-demo/pull/1) is blocked from merging by the ruleset.

## Layout

```
src/factory/
  cli.py                  typer CLI
  llm.py                  Claude Code print mode with JSON-schema structured output
  verification.py         deterministic + semantic + shadow + reducer pipeline
  core/  contract.py constitution.py compiler.py workflow.py evidence.py traceability.py state.py repo.py
  adapters/  atlassian.py (Confluence v2, Jira v3)  github.py (gh: statuses, comments, rulesets)
  harness/claude/  hooks.py installer.py plugin/
  reviewers/  semantic.py reducer.py
  runtime/  commands.py shadow_tests.py
action.yml                GitHub composite action
examples/pricing/         seed project used by the vertical proof
```

Python, Pydantic, Typer, SQLite (journal, task binding, evidence). No agent framework, no vector database.

## Deliberately left out of v0

Autonomous backlog management, agent swarms, long-term memory, embeddings, a web dashboard, harnesses beyond
Claude Code, a self-modifying constitution, and automatic merge.

## License

MIT
