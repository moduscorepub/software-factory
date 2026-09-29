"""GitHub through the `gh` CLI: PR facts, factory/* commit statuses, the report comment, rulesets.

The merge gate itself is GitHub's: a ruleset requiring the factory/* status checks.
"""

from __future__ import annotations

import json
import subprocess

from factory.core.evidence import CHECKS

MARKER = "<!-- factory-review -->"


class GitHubError(RuntimeError):
    pass


def gh(*args: str, payload: dict | None = None) -> str:
    proc = subprocess.run(
        ["gh", *args], input=json.dumps(payload) if payload is not None else None,
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise GitHubError(f"gh {' '.join(args[:3])}: {proc.stderr.strip()[:500]}")
    return proc.stdout


def slug() -> str:
    return gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner").strip()


def pr(number: int) -> dict:
    return json.loads(gh("pr", "view", str(number), "--json",
                         "number,title,body,baseRefName,headRefName,headRefOid,url"))


def set_status(repo: str, sha: str, check: str, state: str, description: str, url: str = "") -> None:
    payload = {"state": state, "context": f"factory/{check}", "description": description[:140]}
    if url:
        payload["target_url"] = url
    gh("api", "-X", "POST", f"repos/{repo}/statuses/{sha}", "--input", "-", payload=payload)


def upsert_comment(repo: str, number: int, body: str) -> str:
    ids = gh("api", f"repos/{repo}/issues/{number}/comments", "--paginate",
             "-q", f'.[] | select(.body | startswith("{MARKER}")) | .id').split()
    if ids:
        out = gh("api", "-X", "PATCH", f"repos/{repo}/issues/comments/{ids[0]}", "--input", "-",
                 payload={"body": body})
    else:
        out = gh("api", "-X", "POST", f"repos/{repo}/issues/{number}/comments", "--input", "-",
                 payload={"body": body})
    return json.loads(out).get("html_url", "")


def protect(repo: str) -> str:
    """Create or update a ruleset on the default branch requiring every factory/* check."""
    ruleset = {
        "name": "factory evidence gate",
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [{
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": False,
                "required_status_checks": [{"context": f"factory/{c}"} for c in CHECKS],
            },
        }],
    }
    existing = json.loads(gh("api", f"repos/{repo}/rulesets"))
    for r in existing:
        if r.get("name") == ruleset["name"]:
            gh("api", "-X", "PUT", f"repos/{repo}/rulesets/{r['id']}", "--input", "-", payload=ruleset)
            return f"updated ruleset {r['id']}"
    created = json.loads(gh("api", "-X", "POST", f"repos/{repo}/rulesets", "--input", "-", payload=ruleset))
    return f"created ruleset {created['id']}"
