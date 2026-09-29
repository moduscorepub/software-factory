"""Project binding (.factory/config.yaml) and local SQLite state (journal, task binding, gate attempts)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from factory.core import repo

FACTORY_DIR = ".factory"


class Commands(BaseModel):
    build: str = ""
    lint: str = ""
    typecheck: str = ""
    test: str = ""
    # Runs one test file; {path} is replaced. Used for shadow tests.
    test_file: str = ""


class Atlassian(BaseModel):
    base_url: str = ""  # https://your-site.atlassian.net
    confluence_space_id: str = ""
    confluence_parent_id: str = ""
    jira_project: str = ""
    epic_type: str = "Epic"
    task_type: str = "Task"


class Config(BaseModel):
    work_prefix: str
    base_branch: str = "main"
    model: str = ""  # claude model alias for compiler and reviewers; empty = CLI default
    commands: Commands = Field(default_factory=Commands)
    test_globs: list[str] = Field(
        default_factory=lambda: [
            "tests/**", "test/**", "**/test_*.py", "**/*_test.py", "**/*_test.go",
            "**/*.test.*", "**/*.spec.*",
        ]
    )
    generated: list[str] = Field(default_factory=list)
    migrations: list[str] = Field(default_factory=lambda: ["**/migrations/**", "**/alembic/versions/**"])
    dependency_manifests: list[str] = Field(
        default_factory=lambda: [
            "pyproject.toml", "requirements*.txt", "package.json", "Cargo.toml", "go.mod", "Gemfile",
        ]
    )
    require_contract: bool = True
    atlassian: Atlassian = Field(default_factory=Atlassian)


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


_SCHEMA = """
create table if not exists journal(
    id integer primary key, ts text not null, session text, work text, task text, kind text not null, data text
);
create table if not exists binding(
    id integer primary key check (id = 1), work text, task text, jira text, branch text, ts text
);
create table if not exists evidence(
    id integer primary key, ts text not null, subject text, sha text, finding text not null
);
"""


class Project:
    def __init__(self, root: Path):
        self.root = root
        self.dir = root / FACTORY_DIR
        self.config = Config.model_validate(yaml.safe_load((self.dir / "config.yaml").read_text()) or {})
        self._db: sqlite3.Connection | None = None

    @classmethod
    def find(cls, start: Path | None = None) -> Project | None:
        start = (start or Path.cwd()).resolve()
        for d in (start, *start.parents):
            if (d / FACTORY_DIR / "config.yaml").is_file():
                return cls(d)
        return None

    @classmethod
    def require(cls, start: Path | None = None) -> Project:
        project = cls.find(start)
        if project is None:
            raise SystemExit("Not a factory-bound repository. Run `factory init` first.")
        return project

    @property
    def contracts_dir(self) -> Path:
        return self.dir / "contracts"

    @property
    def constitution_path(self) -> Path:
        return self.dir / "constitution.yaml"

    @property
    def reports_dir(self) -> Path:
        path = self.dir / "reports"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def base_ref(self, head: str = "HEAD") -> str:
        return repo.merge_base(self.root, self.config.base_branch, head)

    # --- SQLite -----------------------------------------------------------
    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            self._db = sqlite3.connect(self.dir / "state.db", timeout=10, isolation_level=None)
            self._db.row_factory = sqlite3.Row
            self._db.executescript(_SCHEMA)
        return self._db

    def journal(self, session: str, kind: str, data: dict | None = None) -> None:
        b = self.binding()
        self.db.execute(
            "insert into journal(ts, session, work, task, kind, data) values (?,?,?,?,?,?)",
            (now(), session, b and b["work"], b and b["task"], kind, json.dumps(data or {})),
        )

    def events(self, session: str) -> list[dict]:
        rows = self.db.execute(
            "select kind, data, ts from journal where session = ? order by id", (session,)
        ).fetchall()
        return [{"kind": r["kind"], "ts": r["ts"], **json.loads(r["data"] or "{}")} for r in rows]

    def bind(self, work: str, task: str, jira: str, branch: str) -> None:
        self.db.execute(
            "insert or replace into binding(id, work, task, jira, branch, ts) values (1,?,?,?,?,?)",
            (work, task, jira, branch, now()),
        )

    def binding(self) -> dict | None:
        row = self.db.execute("select work, task, jira, branch from binding where id = 1").fetchone()
        return dict(row) if row else None

    def record_evidence(self, subject: str, sha: str, findings: list[dict]) -> None:
        ts = now()
        self.db.executemany(
            "insert into evidence(ts, subject, sha, finding) values (?,?,?,?)",
            [(ts, subject, sha, json.dumps(f)) for f in findings],
        )

    def evidence(self) -> list[dict]:
        """Findings from the most recent run of each subject."""
        rows = self.db.execute(
            "select e.subject, e.sha, e.finding from evidence e join "
            "(select subject, max(ts) ts from evidence group by subject) m "
            "on e.subject = m.subject and e.ts = m.ts"
        ).fetchall()
        return [{"subject": r["subject"], "sha": r["sha"], **json.loads(r["finding"])} for r in rows]
