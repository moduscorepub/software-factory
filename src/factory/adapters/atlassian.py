"""Project an approved Work Contract onto Confluence (REST v2) and Jira (REST v3).

Confluence holds the human-readable approved intent. Jira is a clean projection of tasks[]: a short
Why / Deliverable / Acceptance / Dependencies / Contract / Evidence body, with machine metadata in the
`factory.contract` issue property instead of the visible issue. Without credentials, publish writes the
exact payloads to .factory/contracts/<WORK>/publish/ (dry run).

Credentials: ATLASSIAN_EMAIL, ATLASSIAN_API_TOKEN, and ATLASSIAN_BASE_URL or atlassian.base_url.
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from itertools import batched

from factory.core import contract as wc
from factory.core.contract import Links, Task, WorkContract
from factory.core.state import Project


class AtlassianError(RuntimeError):
    pass


class Client:
    def __init__(self, base: str, email: str, token: str):
        self.base = base.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()

    def __call__(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Authorization": self.auth, "Accept": "application/json",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise AtlassianError(f"{method} {path}: HTTP {exc.code} {exc.read().decode()[:500]}") from exc
        return json.loads(raw) if raw else {}


def client(project: Project) -> Client | None:
    base = os.environ.get("ATLASSIAN_BASE_URL") or project.config.atlassian.base_url
    email, token = os.environ.get("ATLASSIAN_EMAIL"), os.environ.get("ATLASSIAN_API_TOKEN")
    return Client(base, email, token) if base and email and token else None


def search_confluence(project: Project, text: str, limit: int = 5) -> list[str]:
    """Related existing pages for spec intake; empty when Confluence is not configured."""
    api = client(project)
    if api is None:
        return []
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", text)][:8]
    if not words:
        return []
    cql = 'type = page and siteSearch ~ "' + " ".join(words).replace('"', "") + '"'
    space = project.config.atlassian.confluence_space_id
    data = api("GET", "/wiki/rest/api/search", params={"cql": cql, "limit": limit})
    out = []
    for r in data.get("results", []):
        if space and str(r.get("content", {}).get("space", {}).get("id", space)) != space:
            continue
        excerpt = re.sub(r"<[^>]+>|@@@\w*@@@", "", r.get("excerpt", ""))[:400]
        out.append(f"- {r.get('title')}: {excerpt}")
    return out


# --- Confluence storage format ----------------------------------------------

def _e(text: str) -> str:
    return html.escape(text or "")


def _paras(text: str) -> str:
    return "".join(f"<p>{_e(p.strip())}</p>" for p in text.split("\n\n") if p.strip())


def _table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{_e(c)}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><tbody><tr>{head}</tr>{body}</tbody></table>"


def render_page(c: WorkContract, links: Links | None) -> str:
    keys = links.tasks if links else {}
    a = c.approval
    out = [
        '<ac:structured-macro ac:name="info"><ac:rich-text-body>'
        f"<p><strong>Work Contract {_e(c.ref)}</strong> | status: {_e(a.status)} | "
        f"approved by {_e(a.approved_by)} at {_e(a.approved_at)}</p>"
        f"<p>contract hash <code>{_e(a.contract_hash)}</code></p>"
        f"<p>machine contract: <code>.factory/contracts/{_e(c.work_id)}/v{c.version}.yaml</code> | "
        f"factory-contract: <code>{_e(c.work_id.lower())}/v{c.version}</code></p>"
        "</ac:rich-text-body></ac:structured-macro>",
        "<h2>Intent</h2>",
        f"<p><strong>Problem.</strong> {_e(c.intent.problem)}</p>",
        f"<p><strong>Outcome.</strong> {_e(c.intent.outcome)}</p>",
    ]
    if c.intent.non_goals:
        out.append("<p><strong>Non-goals.</strong></p><ul>"
                   + "".join(f"<li>{_e(n)}</li>" for n in c.intent.non_goals) + "</ul>")
    spec = [(k, v) for k, v in c.spec.model_dump().items() if v]
    if spec:
        out.append("<h2>Specification</h2>")
        for key, text in spec:
            out.append(f"<h3>{_e(key.replace('_', ' ').capitalize())}</h3>{_paras(text)}")
    out += [
        "<h2>Requirements</h2>",
        _table(["ID", "Requirement", "Rationale"], [[r.id, r.statement, r.rationale] for r in c.requirements]),
        "<h2>Acceptance criteria</h2>",
        _table(["ID", "Req", "Given", "When", "Then"],
               [[x.id, x.requirement, x.given, x.when, x.then] for x in c.acceptance]),
    ]
    if c.invariants:
        out += ["<h2>Invariants</h2>",
                _table(["ID", "Statement", "Source"], [[i.id, i.statement, i.source] for i in c.invariants])]
    if c.decisions:
        out += ["<h2>Decisions</h2>", _table(
            ["ID", "Decision", "Rejected alternatives", "Rationale"],
            [[d.id, d.decision, "; ".join(d.alternatives_rejected), d.rationale] for d in c.decisions])]
    out += ["<h2>Tasks</h2>", _table(
        ["ID", "Jira", "Task", "Deliverable", "Acceptance", "Depends on", "Verification"],
        [[t.id, keys.get(t.id, ""), t.title, t.deliverable, ", ".join(t.acceptance),
          ", ".join(keys.get(d, d) for d in t.depends_on), ", ".join(t.verification)] for t in c.tasks])]
    if c.risks:
        out += ["<h2>Risks</h2>",
                _table(["ID", "Risk", "Mitigation"], [[r.id, r.description, r.mitigation] for r in c.risks])]
    trace = []
    for r in c.requirements:
        accs = [x.id for x in c.acceptance if x.requirement == r.id]
        tasks = [keys.get(t.id, t.id) for t in c.tasks if set(t.acceptance) & set(accs)]
        trace.append([r.id, ", ".join(accs), ", ".join(tasks)])
    out += ["<h2>Traceability</h2>", _table(["Requirement", "Acceptance", "Tasks"], trace)]
    if c.unknowns:
        out += ["<h2>Unknowns</h2><ul>" + "".join(f"<li>{_e(u)}</li>" for u in c.unknowns) + "</ul>"]
    return "".join(out)


# --- Jira (Atlassian Document Format) -----------------------------------------

def _text(t: str, link: str = "", code: bool = False) -> dict:
    node: dict = {"type": "text", "text": t}
    marks = ([{"type": "link", "attrs": {"href": link}}] if link else []) + ([{"type": "code"}] if code else [])
    if marks:
        node["marks"] = marks
    return node


def _h(t: str) -> dict:
    return {"type": "heading", "attrs": {"level": 3}, "content": [_text(t)]}


def _p(*nodes: dict) -> dict:
    return {"type": "paragraph", "content": list(nodes)}


def _ul(items: list[str]) -> dict:
    return {"type": "bulletList",
            "content": [{"type": "listItem", "content": [_p(_text(i))]} for i in items]}


def issue_fields(project: Project, c: WorkContract, task: Task, keys: dict[str, str], page_url: str,
                 epic: str) -> dict:
    cfg = project.config.atlassian
    criteria = {a.id: a for a in c.acceptance}
    deps = [keys.get(d, d) for d in task.depends_on]
    doc = [
        _h("Why"), _p(_text(c.intent.outcome)),
        _h("Deliverable"), _p(_text(task.deliverable or task.title)),
        _h("Acceptance"), _ul([f"{a}: then {criteria[a].then}" for a in task.acceptance if a in criteria]),
        _h("Dependencies"), _ul(deps) if deps else _p(_text("none")),
        _h("Contract"),
        _p(_text("Confluence spec", link=page_url)) if page_url else _p(_text("Confluence spec not published")),
        _p(_text(f"factory-contract: {c.work_id.lower()}/v{c.version}", code=True)),
        _h("Evidence required"),
        _ul([*task.verification, f"tests annotated covers {c.work_id}/<A-ID> for each acceptance criterion"]),
    ]
    fields = {
        "project": {"key": cfg.jira_project},
        "issuetype": {"name": cfg.task_type},
        "summary": task.title[:250],
        "description": {"type": "doc", "version": 1, "content": doc},
        "labels": ["factory", c.work_id.lower()],
    }
    if epic:
        fields["parent"] = {"key": epic}
    return fields


def issue_property(c: WorkContract, task: Task) -> dict:
    return {
        "work_id": c.work_id, "version": c.version, "task_id": task.id,
        "contract_hash": c.approval.contract_hash, "requirements": task.requirements,
        "acceptance": task.acceptance, "depends_on": task.depends_on, "verification": task.verification,
    }


def epic_fields(project: Project, c: WorkContract, page_url: str) -> dict:
    cfg = project.config.atlassian
    content = [_p(_text(c.intent.outcome))]
    if page_url:
        content.append(_p(_text("Confluence spec", link=page_url)))
    return {
        "project": {"key": cfg.jira_project},
        "issuetype": {"name": cfg.epic_type},
        "summary": f"{c.work_id}: {c.title}"[:250],
        "description": {"type": "doc", "version": 1, "content": content},
        "labels": ["factory", c.work_id.lower()],
    }


# --- publish --------------------------------------------------------------------

def publish(project: Project, c: WorkContract, dry_run: bool = False) -> tuple[Links, str]:
    if not c.approved:
        raise AtlassianError(f"{c.ref} is not approved; only approved contracts are executable")
    links = wc.load_links(project, c.work_id) or Links(work_id=c.work_id, version=c.version)
    links.version = c.version
    api = None if dry_run else client(project)
    if api is None:
        out = wc.work_dir(project, c.work_id) / "publish"
        out.mkdir(parents=True, exist_ok=True)
        (out / "confluence.html").write_text(render_page(c, links))
        issues = {
            t.id: {"fields": issue_fields(project, c, t, links.tasks, links.confluence_url, links.epic_key),
                   "properties": {"factory.contract": issue_property(c, t)}}
            for t in c.tasks
        }
        (out / "jira.json").write_text(json.dumps(
            {"epic": epic_fields(project, c, links.confluence_url), "issues": issues}, indent=2))
        wc.save_links(project, links)
        reason = "dry run" if dry_run else "no Atlassian credentials"
        return links, f"{reason}: wrote {out.relative_to(project.root)}/confluence.html and jira.json"

    cfg = project.config.atlassian
    if not cfg.confluence_space_id or not cfg.jira_project:
        raise AtlassianError("set atlassian.confluence_space_id and atlassian.jira_project in .factory/config.yaml")
    title = f"{c.work_id}: {c.title}"

    def put_page(body: str) -> None:
        if links.confluence_page_id:
            current = api("GET", f"/wiki/api/v2/pages/{links.confluence_page_id}")
            resp = api("PUT", f"/wiki/api/v2/pages/{links.confluence_page_id}", {
                "id": links.confluence_page_id, "status": "current", "title": title,
                "body": {"representation": "storage", "value": body},
                "version": {"number": current["version"]["number"] + 1, "message": f"factory {c.ref}"},
            })
        else:
            payload = {"spaceId": cfg.confluence_space_id, "status": "current", "title": title,
                       "body": {"representation": "storage", "value": body}}
            if cfg.confluence_parent_id:
                payload["parentId"] = cfg.confluence_parent_id
            resp = api("POST", "/wiki/api/v2/pages", payload)
            links.confluence_page_id = str(resp["id"])
        links.confluence_version = int(resp["version"]["number"])
        links.confluence_url = api.base + "/wiki" + resp["_links"]["webui"]

    put_page(render_page(c, links))

    if links.epic_key:
        api("PUT", f"/rest/api/3/issue/{links.epic_key}", {"fields": epic_fields(project, c, links.confluence_url)})
    else:
        links.epic_key = api("POST", "/rest/api/3/issue",
                             {"fields": epic_fields(project, c, links.confluence_url)})["key"]

    new = [t for t in c.tasks if t.id not in links.tasks]
    for chunk in batched(new, 50, strict=False):
        resp = api("POST", "/rest/api/3/issue/bulk", {"issueUpdates": [
            {"fields": issue_fields(project, c, t, links.tasks, links.confluence_url, links.epic_key)}
            for t in chunk]})
        if resp.get("errors"):
            raise AtlassianError(f"bulk create errors: {json.dumps(resp['errors'])[:800]}")
        for t, issue in zip(chunk, resp["issues"], strict=True):
            links.tasks[t.id] = issue["key"]
    wc.save_links(project, links)

    created = {t.id for t in new}
    for t in c.tasks:
        key = links.tasks[t.id]
        api("PUT", f"/rest/api/3/issue/{key}",
            {"fields": issue_fields(project, c, t, links.tasks, links.confluence_url, links.epic_key)})
        api("PUT", f"/rest/api/3/issue/{key}/properties/factory.contract", issue_property(c, t))
        if t.id in created:
            for dep in t.depends_on:
                api("POST", "/rest/api/3/issueLink", {"type": {"name": "Blocks"},
                    "outwardIssue": {"key": links.tasks[dep]}, "inwardIssue": {"key": key}})

    put_page(render_page(c, links))  # second render carries the Jira keys
    wc.save_links(project, links)
    wc.record_source(project, c, links.confluence_page_id, links.confluence_version)
    return links, f"published {links.confluence_url} and {len(c.tasks)} Jira task(s) under {links.epic_key}"
