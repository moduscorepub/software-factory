"""factory: the software-factory control plane.

init -> spec -> approve -> publish -> work -> verify -> review, with trace and doctor alongside.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

import typer
import yaml
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from factory import __version__, llm, verification
from factory.adapters import atlassian, github
from factory.core import compiler, constitution, repo, traceability, workflow
from factory.core import contract as wc
from factory.core.evidence import LADDER, Report, render_markdown
from factory.core.state import FACTORY_DIR, Commands, Config, Project
from factory.harness.claude import hooks, installer
from factory.reviewers import reducer
from factory.runtime import shadow_tests

app = typer.Typer(
    no_args_is_help=True, add_completion=False,
    help="Software-factory control plane: intent -> Work Contract -> Jira -> coding harness -> PR evidence gate.",
)
install_app = typer.Typer(no_args_is_help=True, help="Install a coding-harness integration.")
app.add_typer(install_app, name="install")
console = Console()
err = Console(stderr=True)


def fail(msg: str) -> NoReturn:
    err.print(f"[red]error:[/] {escape(msg)}")
    raise typer.Exit(1)


def _project() -> Project:
    try:
        return Project.require()
    except SystemExit as exc:
        fail(str(exc))


# --- init ---------------------------------------------------------------------------

WORKFLOW = """\
name: factory
on:
  pull_request:
permissions:
  contents: read
  statuses: write
  pull-requests: write
jobs:
  evidence:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          fetch-depth: 0
      - uses: moduscorepub/software-factory@main
        env:
          # Either secret enables the semantic reviewers and shadow tests (factory/review).
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
"""


def _detect(root: Path) -> Commands:
    if (root / "pyproject.toml").exists():
        text = (root / "pyproject.toml").read_text()
        uv = (root / "uv.lock").exists() or "[tool.uv]" in text or "[dependency-groups]" in text
        run = "uv run " if uv else "python -m "
        return Commands(
            lint=f"{run}ruff check ." if "ruff" in text else "",
            typecheck=f"{run}mypy ." if "mypy" in text else "",
            test=f"{run}pytest -q",
            test_file=f"{run}pytest -q {{path}}",
        )
    if (root / "package.json").exists():
        scripts = json.loads((root / "package.json").read_text()).get("scripts", {})
        pick = {k: f"npm run {k}" for k in ("build", "lint", "typecheck") if k in scripts}
        test = "npm test" if "test" in scripts else ""
        return Commands(**pick, test=test, test_file=f"{test} -- {{path}}" if test else "")
    if (root / "Cargo.toml").exists():
        return Commands(build="cargo build", lint="cargo clippy -- -D warnings", test="cargo test")
    if (root / "go.mod").exists():
        return Commands(build="go build ./...", lint="go vet ./...", test="go test ./...", test_file="go test ./...")
    return Commands()


@app.command()
def init(
    prefix: str = typer.Option("", help="Work id prefix, e.g. PRICING (default: repository name)."),
    base: str = typer.Option("", help="Base branch (default: the current branch)."),
    protect: bool = typer.Option(False, help="Create a GitHub ruleset requiring the factory/* checks."),
    force: bool = typer.Option(False, help="Rewrite an existing config.yaml."),
) -> None:
    """Bind this repository to the factory."""
    root = repo.toplevel(Path.cwd()) or fail("not inside a git repository")
    d = root / FACTORY_DIR
    existing = d / "config.yaml"
    if existing.exists() and not force:
        if not protect:
            fail("already initialised; --force rewrites config.yaml, --protect alone adds the GitHub ruleset")
        console.print(github.protect(github.slug()))
        return
    old = Config.model_validate(yaml.safe_load(existing.read_text())) if existing.exists() else None
    prefix = prefix or (old.work_prefix if old else re.sub(r"[^A-Z0-9]", "", root.name.upper())[:10] or "WORK")
    base = base or (old.base_branch if old else "") or repo.git(
        root, "symbolic-ref", "--short", "HEAD", check=False).strip() or "main"
    cfg = Config(work_prefix=prefix, base_branch=base, commands=_detect(root))
    d.mkdir(exist_ok=True)
    (d / "config.yaml").write_text(yaml.safe_dump(cfg.model_dump(), sort_keys=False))
    if not (d / "constitution.yaml").exists():
        (d / "constitution.yaml").write_text(constitution.DEFAULT)
    (d / ".gitignore").write_text("state.db\nreports/\ncontracts/*/publish/\n")
    wf = root / ".github" / "workflows" / "factory.yml"
    if not wf.exists():
        wf.parent.mkdir(parents=True, exist_ok=True)
        wf.write_text(WORKFLOW)
    console.print(f"[green]bound[/] {root} prefix={prefix} base={base}")
    for k, v in cfg.commands.model_dump().items():
        console.print(f"  {k:10} {v or '[dim](not set)[/]'}")
    console.print("wrote .factory/config.yaml, .factory/constitution.yaml, .github/workflows/factory.yml")
    if protect:
        console.print(github.protect(github.slug()))


# --- spec / approve / publish -----------------------------------------------------------

def _show_contract(c: wc.WorkContract) -> None:
    console.print(f"\n[bold]{c.ref}[/] {escape(c.title)} [dim]({c.approval.status})[/]")
    console.print(f"[bold]Outcome[/] {escape(c.intent.outcome)}")
    for title, cols, rows in (
        ("Requirements", ["id", "statement"], [[r.id, r.statement] for r in c.requirements]),
        ("Acceptance", ["id", "req", "given / when / then"],
         [[a.id, a.requirement, f"given {a.given}; when {a.when}; then {a.then}"] for a in c.acceptance]),
        ("Tasks", ["id", "task", "acceptance", "depends"],
         [[t.id, t.title, ", ".join(t.acceptance), ", ".join(t.depends_on)] for t in c.tasks]),
        ("Decisions", ["id", "decision"], [[d.id, d.decision] for d in c.decisions]),
    ):
        if rows:
            table = Table(title=title, title_justify="left", show_header=True)
            for col in cols:
                table.add_column(col)
            for row in rows:
                table.add_row(*(escape(str(x)) for x in row))
            console.print(table)
    if c.unknowns:
        console.print("[yellow]Unknowns[/]\n" + "\n".join(f"- {escape(u)}" for u in c.unknowns))


def _approve(project: Project, c: wc.WorkContract) -> wc.WorkContract:
    by = repo.git(project.root, "config", "user.name", check=False).strip() or os.environ.get("USER", "unknown")
    try:
        approved = c.approve(by)
    except wc.ContractError as exc:
        fail("cannot approve:\n- " + "\n- ".join(exc.problems))
    wc.save(project, approved)
    console.print(f"[green]approved[/] {approved.ref} {approved.approval.contract_hash}")
    return approved


@app.command()
def spec(
    request: str = typer.Argument(None, help="The raw request. Omit to use --file or stdin."),
    file: Path = typer.Option(None, "--file", "-f", help="Read the request from a file."),
    revise: str = typer.Option("", help="Revise this WORK-ID into a new version."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Accept recommended answers without asking."),
    approve_now: bool = typer.Option(False, "--approve", help="Approve without prompting."),
) -> None:
    """Compile a raw request into a Work Contract (analyse, clarify, specify, critique, converge)."""
    project = _project()
    text = request or (file.read_text() if file else "" if sys.stdin.isatty() else sys.stdin.read())
    if not text.strip():
        fail("give a request argument, --file, or stdin")
    previous = wc.latest(project, revise) if revise else None

    def answer(q: dict) -> str:
        console.print(f"\n[bold]?[/] {escape(q['question'])}\n  [dim]{escape(q['why_it_matters'])}[/]")
        for i, option in enumerate(q["options"], 1):
            console.print(f"  {i}. {escape(option)}")
        if yes:
            console.print(f"  -> {escape(q['default'])} [dim](recommended)[/]")
            return q["default"]
        raw = typer.prompt("  answer (number or text)", default=q["default"])
        if raw.isdigit() and 1 <= int(raw) <= len(q["options"]):
            return q["options"][int(raw) - 1]
        return raw

    try:
        draft, analysis, critique = compiler.compile_contract(
            project, text, answer, previous, log=lambda m: console.print(f"[dim]> {m}[/]")
        )
    except (llm.LLMError, wc.ContractError) as exc:
        fail(str(exc))
    path = wc.save(project, draft)
    (path.parent / f"compile-v{draft.version}.json").write_text(
        json.dumps({"request": text, "analysis": analysis, "critique": critique}, indent=2)
    )
    _show_contract(draft)
    console.print(f"\ndraft {path.relative_to(project.root)} ({len(critique['issues'])} criticisms converged)")
    if approve_now or (not yes and typer.confirm("Approve this Work Contract?", default=False)):
        _approve(project, draft)
    else:
        console.print(f"review it, then run `factory approve {draft.work_id}`")


@app.command()
def approve(work_id: str) -> None:
    """Human approval: freeze the latest draft of WORK_ID. Only approved contracts are executable."""
    project = _project()
    c = wc.latest(project, work_id)
    if c.approved:
        fail(f"{c.ref} is already approved; revise with `factory spec --revise {work_id}`")
    _show_contract(c)
    _approve(project, c)


@app.command()
def publish(work_id: str, dry_run: bool = typer.Option(False, "--dry-run", help="Write payloads only.")) -> None:
    """Project an approved contract to Confluence (spec page) and Jira (epic + task graph)."""
    project = _project()
    try:
        c = wc.latest(project, work_id, approved_only=True)
        links, message = atlassian.publish(project, c, dry_run)
    except (wc.ContractError, atlassian.AtlassianError) as exc:
        fail(str(exc))
    console.print(message)
    for t in c.tasks:
        console.print(f"  {t.id} {links.tasks.get(t.id, '[dim](not in Jira)[/]')} {escape(t.title)}")


# --- work / verify / trace ------------------------------------------------------------

@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def work(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Jira key, WORK-ID/T-ID, T-ID, or single-task WORK-ID."),
    bind: bool = typer.Option(False, "--bind", help="Only bind the task and print its context."),
    headless: bool = typer.Option(False, help="Run Claude in print mode (no TTY), accepting edits."),
    prompt: str = typer.Option("", help="Extra instruction for the session."),
) -> None:
    """Start a Claude Code session bound to a task. Extra arguments are passed to claude."""
    project = _project()
    try:
        c, task, jira = wc.resolve_task(project, ref)
    except wc.ContractError as exc:
        fail(str(exc))
    root = project.root
    branch = repo.current_branch(root)
    if branch == project.config.base_branch and not repo.is_dirty(root):
        slug = re.sub(r"[^a-z0-9]+", "-", task.title.lower()).strip("-")[:40]
        branch = f"factory/{jira}-{slug}" if jira else f"factory/{c.work_id.lower()}-{task.id.lower()}-{slug}"
        repo.git(root, "checkout", "-b", branch, check=False)
        if repo.current_branch(root) != branch:
            repo.git(root, "checkout", branch)
    project.bind(c.work_id, task.id, jira, branch)
    if bind:
        print(workflow.brief(project, c, task, jira))
        return
    console.print(f"[green]bound[/] {c.work_id}/{task.id} {jira} on {branch}")
    cmd = ["claude"]
    if not installer.installed():
        cmd += ["--plugin-dir", str(installer.PLUGIN_DIR)]
    instruction = (
        f"Implement factory task {c.work_id}/{task.id}{f' ({jira})' if jira else ''}: {task.title}. "
        "The factory session context holds its approved Work Contract; follow it. " + prompt
    ).strip()
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "FACTORY_NESTED")}
    env["FACTORY_BIN"] = installer.factory_bin()
    if headless:
        cmd += ["-p", "--permission-mode", "acceptEdits", *ctx.args,
                "--allowedTools=Bash Read Edit Write Glob Grep TodoWrite"]
        proc = subprocess.run(cmd, cwd=root, env=env, input=instruction, text=True)
    else:
        proc = subprocess.run([*cmd, *ctx.args, instruction], cwd=root, env=env)
    raise typer.Exit(proc.returncode)


def _print_report(report: Report, path: Path | None) -> None:
    verdict = "[red]BLOCKED[/]" if report.blocking else "[green]PASS[/]"
    table = Table(title=f"factory evidence {verdict}  {escape(report.subject)}", title_justify="left")
    for col in ("check", "state", "detail"):
        table.add_column(col)
    for check, (state, desc) in report.checks().items():
        color = {"failure": "red", "success": "green"}.get(state, "yellow")
        table.add_row(f"factory/{check}", f"[{color}]{state}[/]", escape(desc))
    console.print(table)
    console.print(f"contracts: {', '.join(report.contracts) or 'none bound'}")
    for f in report.findings:
        style = "red" if f.blocking else "yellow" if f.review_required else "dim"
        tag = "refuted " if f.refuted else ""
        console.print(f"[{style}]{f.id} {f.evidence} {tag}{LADDER[f.evidence]}[/] "
                      f"{escape(f'[{f.check}/{f.source}]')} {escape(f.title)}")
        if f.locations:
            console.print("    at " + ", ".join(str(loc) for loc in f.locations[:5]), highlight=False)
        if f.detail:
            console.print(f"    {escape(f.detail)}", highlight=False)
        if f.output and f.blocking:
            console.print("\n".join("    | " + ln for ln in f.output.strip().splitlines()[-12:]),
                          highlight=False, markup=False)
    console.print(f"semantic review: {escape(report.semantic)}")
    if path:
        console.print(f"report: {path}")


def _save(project: Project, report: Report, name: str) -> Path:
    md = project.reports_dir / f"{name}.md"
    md.write_text(render_markdown(report))
    (project.reports_dir / f"{name}.json").write_text(report.model_dump_json(indent=2))
    project.record_evidence(report.subject, report.head, [f.model_dump(mode="json") for f in report.findings])
    return md


@app.command()
def verify() -> None:
    """Local completion checks on the working tree: contracts, traceability, commands, constitution."""
    project = _project()
    base = project.base_ref()
    bound = verification.bind(project, base, None, [], use_active=True)
    runs, findings = verification.deterministic(project, base, None, bound)
    report = Report(
        subject=f"local {repo.current_branch(project.root)}", base=base, head="WORKTREE",
        contracts=[f"{c.work_id}/{t.id}" + (f" ({j})" if j else "") for c, t, j in bound],
        ran=runs, findings=reducer.reduce(findings), semantic="not run (use `factory review`)",
    )
    _print_report(report, _save(project, report, "verify"))
    raise typer.Exit(1 if report.blocking else 0)


@app.command()
def trace(ref: str = typer.Argument(..., help="Jira key, WORK-ID/T-ID, T-ID or WORK-ID.")) -> None:
    """Show requirement -> acceptance -> task -> commit -> file:line -> test -> evidence."""
    project = _project()
    try:
        c, task, _ = wc.resolve_task(project, ref)
        tasks = [task]
    except wc.ContractError as exc:
        if ref not in wc.work_ids(project):
            fail(str(exc))
        c = wc.latest(project, ref)
        tasks = c.tasks
    console.print(traceability.tree(project, c, tasks))


# --- review -----------------------------------------------------------------------------

@app.command()
def review(
    pr: int = typer.Argument(None, help="Pull request number. Omit to review the current branch."),
    base: str = typer.Option("", help="Base branch for a local review (default: config base_branch)."),
    post: bool = typer.Option(False, help="Post factory/* commit statuses and the report comment."),
    ai: bool = typer.Option(True, "--ai/--no-ai", help="Run the semantic reviewers."),
    shadow: bool = typer.Option(True, "--shadow/--no-shadow", help="Execute reviewer reproductions."),
) -> None:
    """PR evidence machine: deterministic checks, adversarial reviewers, shadow tests, merge gate."""
    project = _project()
    root = project.root
    texts: list[str] = []
    if pr is not None:
        info = github.pr(pr)
        head = info["headRefOid"]
        try:
            repo.resolve(root, head)
        except repo.GitError:
            repo.git(root, "fetch", "origin", f"pull/{pr}/head")
        base_branch = info["baseRefName"]
        texts = [info["title"], info["body"] or "", info["headRefName"]]
        subject = f"PR #{pr}"
    else:
        head = repo.head_sha(root)
        base_branch = base or project.config.base_branch
        subject = f"branch {repo.current_branch(root)}"
        if repo.is_dirty(root):
            err.print("[yellow]warning:[/] uncommitted changes are not part of the reviewed range")
    base_sha = repo.merge_base(root, base_branch, head)
    if base_sha == head:
        fail(f"nothing to review: {head[:10]} is already on {base_branch}")

    with contextlib.ExitStack() as stack:
        target = project
        if repo.head_sha(root) != head:
            target = Project(stack.enter_context(shadow_tests.worktree(root, head)))
        bound = verification.bind(target, base_sha, head, texts, use_active=pr is None)
        report = verification.review(target, base_sha, head, subject, bound, ai=ai, shadow=shadow,
                                     log=lambda m: console.print(f"[dim]> {m}[/]"))
    path = _save(project, report, f"pr-{pr}" if pr is not None else f"review-{head[:10]}")
    _print_report(report, path)
    if post:
        slug = github.slug()
        url = github.upsert_comment(slug, pr, render_markdown(report)) if pr is not None else ""
        for check, (state, desc) in report.checks().items():
            if state != "skipped":
                github.set_status(slug, head, check, state, desc, url)
        console.print(f"posted factory/* statuses to {slug}@{head[:10]}" + (f" and {url}" if url else ""))
    raise typer.Exit(1 if report.blocking else 0)


# --- doctor / install / schema / hook ------------------------------------------------------

@app.command()
def doctor() -> None:
    """Diagnose the harness, bindings and integrations."""
    rows: list[tuple[str, bool, str]] = []
    ok_py = sys.version_info >= (3, 13)
    rows.append(("python", ok_py, sys.version.split()[0]))
    root = repo.toplevel(Path.cwd())
    rows.append(("git repository", root is not None, str(root or "not inside a git repository")))
    project = Project.find()
    rows.append(("factory binding", project is not None, str(project.root) if project else "run `factory init`"))
    if project:
        try:
            laws = constitution.load(project.constitution_path).laws
            rows.append(("constitution", True, f"{len(laws)} laws"))
        except Exception as exc:  # noqa: BLE001
            rows.append(("constitution", False, str(exc)))
        try:
            contracts = wc.all_contracts(project)
            tampered = [c.ref for _, c in contracts if c.tampered()]
            rows.append(("contracts", not tampered,
                         f"{len(contracts)} version(s)" + (f"; TAMPERED: {', '.join(tampered)}" if tampered else "")))
        except Exception as exc:  # noqa: BLE001
            rows.append(("contracts", False, str(exc)))
        cmds = project.config.commands
        rows.append(("test command", bool(cmds.test), cmds.test or "set commands.test in .factory/config.yaml"))
        rows.append(("shadow test runner", bool(cmds.test_file), cmds.test_file or "set commands.test_file"))
        b = workflow.bound(project)
        rows.append(("active task", True, f"{b[0].work_id}/{b[1].id} {b[2]}" if b else "none"))
        api = atlassian.client(project)
        rows.append(("atlassian", True, f"configured ({api.base})" if api else
                     "not configured: publish runs as a dry run (set ATLASSIAN_EMAIL, ATLASSIAN_API_TOKEN, base_url)"))
    claude = shutil.which("claude")
    version = subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.strip() if claude else ""
    rows.append(("claude CLI", bool(claude), version or "not found: install Claude Code"))
    rows.append(("claude plugin", True, "installed" if installer.installed() else
                 "not installed: `factory work` loads it per session; `factory install claude` for all sessions"))
    rows.append(("factory on PATH", True, shutil.which("factory") or "no: plugin hooks outside `factory work` need it"))
    gh = shutil.which("gh")
    authed = gh and subprocess.run(["gh", "auth", "status"], capture_output=True).returncode == 0
    rows.append(("gh CLI", bool(gh), "authenticated" if authed else "missing or not authenticated (review --post)"))
    table = Table(title=f"factory doctor v{__version__}", title_justify="left")
    for col in ("check", "ok", "detail"):
        table.add_column(col)
    for name, ok, detail in rows:
        table.add_row(name, "[green]ok[/]" if ok else "[red]fail[/]", escape(detail))
    console.print(table)
    raise typer.Exit(0 if all(ok for _, ok, _ in rows) else 1)


@install_app.command("claude")
def install_claude() -> None:
    """Install the factory plugin into Claude Code (hooks, skills, agents, commands)."""
    try:
        console.print(installer.install())
    except RuntimeError as exc:
        fail(str(exc))
    if not shutil.which("factory"):
        err.print("[yellow]warning:[/] `factory` is not on PATH; plugin hooks need it (e.g. `uv tool install`).")


@app.command()
def schema(name: str = typer.Argument(..., help="work-contract | constitution | evidence")) -> None:
    """Print the JSON schema of a factory artifact."""
    models = {"work-contract": wc.WorkContract, "constitution": constitution.Constitution, "evidence": Report}
    if name not in models:
        fail(f"unknown schema {name!r}; choose one of {', '.join(models)}")
    print(json.dumps(models[name].model_json_schema(by_alias=True), indent=2))


@app.command(hidden=True)
def hook(event: str) -> None:
    """Claude Code hook entrypoint (reads hook JSON on stdin)."""
    hooks.main(event)
