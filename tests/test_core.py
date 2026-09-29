import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from factory.core import contract as wc
from factory.core import traceability, workflow
from factory.core.evidence import E, Finding, Location, Report, Reproduction
from factory.core.state import Config, Project
from factory.reviewers import reducer
from factory.runtime import shadow_tests


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def project(tmp_path: Path) -> Project:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / ".factory").mkdir()
    cfg = Config(work_prefix="W", commands={"test_file": f"{sys.executable} -m pytest -q -p no:cacheprovider {{path}}"})
    (tmp_path / ".factory" / "config.yaml").write_text(yaml.safe_dump(cfg.model_dump()))
    return Project(tmp_path)


def contract(**over) -> wc.WorkContract:
    data = {
        "work_id": "W-1", "title": "t",
        "intent": {"problem": "p", "outcome": "o"},
        "requirements": [{"id": "R-001", "statement": "s"}, {"id": "R-002", "statement": "s"}],
        "acceptance": [
            {"id": "A-001", "requirement": "R-001", "given": "g", "when": "w", "then": "t"},
            {"id": "A-002", "requirement": "R-002", "given": "g", "when": "w", "then": "t"},
        ],
        "tasks": [
            {"id": "T-001", "title": "a", "requirements": ["R-001"], "acceptance": ["A-001"]},
            {"id": "T-002", "title": "b", "requirements": ["R-002"], "acceptance": ["A-002"], "depends_on": ["T-001"]},
        ],
    }
    data.update(over)
    return wc.WorkContract.model_validate(data)


def test_contract_integrity_catches_broken_graphs():
    assert contract().problems() == []
    broken = contract(
        acceptance=[{"id": "A-001", "requirement": "R-001", "given": "g", "when": "w", "then": "t"},
                    {"id": "A-003", "requirement": "R-009", "given": "g", "when": "w", "then": "t"}],
        tasks=[{"id": "T-001", "title": "a", "acceptance": ["A-001"], "depends_on": ["T-002"]},
               {"id": "T-002", "title": "b", "acceptance": ["A-001"], "depends_on": ["T-001"]}],
    )
    problems = "\n".join(broken.problems())
    assert "R-002 has no acceptance criterion" in problems
    assert "A-003 references unknown requirement R-009" in problems
    assert "A-003 is not covered by any task" in problems
    assert "task dependency cycle" in problems


def test_approval_freezes_content_and_detects_tampering(project):
    with pytest.raises(wc.ContractError):
        contract(unknowns=["which currency?"]).approve("me")
    approved = contract().approve("me")
    wc.save(project, approved)
    with pytest.raises(wc.ContractError):
        wc.save(project, approved)  # immutable once approved
    assert not approved.tampered()
    edited = approved.model_copy(update={"title": "quietly changed"})
    assert edited.tampered()
    assert not wc.record_source(project, approved, "123", 4).tampered()


def test_evidence_ladder_policy():
    def f(e, **kw):
        return Finding(check="review", source="x", title="t", evidence=e, **kw)

    assert not f(E.E0).blocking
    assert f(E.E1).review_required and not f(E.E1).blocking
    assert f(E.E2).blocking and f(E.E3).blocking and f(E.E4).blocking
    assert f(E.E1, deterministic_law=True).blocking
    assert not f(E.E3, refuted=True).blocking
    report = Report(subject="s", base="b", head="h", findings=[f(E.E3)], semantic="ran")
    assert report.checks()["review"][0] == "failure" and report.checks()["tests"][0] == "success"
    assert Report(subject="s", base="b", head="h").checks()["review"][0] == "skipped"


def test_guard_blocks_danger_but_not_ordinary_work(project):
    deny = [
        "git push --force origin main", "git reset --hard HEAD~3", "git clean -fdx", "rm -rf ~",
        "cat .env", "echo $GITHUB_TOKEN",
    ]
    for cmd in deny:
        assert workflow.guard(project, "Bash", {"command": cmd})[0] == "deny", cmd
    assert workflow.guard(project, "Bash", {"command": "pip install requests"})[0] == "ask"
    for cmd in ("git status", "pytest -q", "git push origin feature", "rm -rf build/", "pip install -e ."):
        assert workflow.guard(project, "Bash", {"command": cmd}) is None, cmd
    assert workflow.guard(project, "Read", {"file_path": str(project.root / ".env")})[0] == "deny"
    assert workflow.guard(project, "Read", {"file_path": str(project.root / ".env.example")}) is None
    wc.save(project, contract().approve("me"))
    target = project.root / ".factory/contracts/W-1/v1.yaml"
    assert workflow.guard(project, "Edit", {"file_path": str(target), "new_string": "x"})[0] == "deny"
    assert workflow.guard(project, "Write", {"file_path": str(project.root / "a.py"), "content": "ok"}) is None


def test_traceability_maps_annotations_to_tests(project):
    tests = project.root / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text('def test_bulk():\n    """covers W-1/A-001"""\n')
    refs = traceability.test_refs(project)
    assert refs["W-1/A-001"] == ["tests/test_x.py::test_bulk"]
    c = contract()
    assert traceability.missing(c, c.tasks[0], refs) == []
    assert traceability.missing(c, c.tasks[1], refs) == ["A-002"]


def test_reducer_merges_duplicates_keeping_strongest_evidence():
    loc = [Location(path="a.py", line=10)]
    weak = Finding(check="review", source="tests", title="x", evidence=E.E0, refs=["A-001"], locations=loc)
    strong = Finding(check="review", source="behaviour", title="y", evidence=E.E3, refs=["A-001"],
                     locations=[Location(path="a.py", line=13)])
    other = Finding(check="review", source="regression", title="z", evidence=E.E1, locations=[Location(path="b.py")])
    out = reducer.reduce([weak, strong, other])
    assert [(f.id, f.evidence, f.source) for f in out] == [
        ("E-001", "E3", "behaviour+tests"), ("E-002", "E1", "regression"),
    ]


def test_shadow_tests_classify_by_executed_behaviour(project):
    root = project.root
    (root / "calc.py").write_text("def f():\n    return 1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    (root / "calc.py").write_text("def f():\n    return 2\n")
    git(root, "commit", "-qam", "head")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()

    def finding(code: str, differential: bool) -> Finding:
        rep = Reproduction(kind="test", path="test_shadow.py", code=code, differential=differential)
        return Finding(check="review", source="regression", title="t", evidence=E.E0, reproduction=rep)

    pins_base = finding("from calc import f\ndef test_f():\n    assert f() == 1\n", True)
    passes = finding("from calc import f\ndef test_f():\n    assert f() == 2\n", False)
    fails_both = finding("from calc import f\ndef test_f():\n    assert f() == 3\n", True)
    shadow_tests.verify(root, [pins_base, passes, fails_both], base, head, project.config.commands.test_file)
    assert pins_base.evidence == E.E3 and pins_base.blocking
    assert passes.refuted and not passes.blocking
    assert fails_both.evidence == E.E0 and "inconclusive" in fails_both.detail
    assert not (root / "test_shadow.py").exists()
