"""Evidence reducer: deduplicate findings across reviewers, keep the strongest evidence, number them."""

from __future__ import annotations

from factory.core.evidence import Finding


def _close(a: Finding, b: Finding) -> bool:
    return any(
        x.path == y.path and abs(x.line - y.line) <= 5 for x in a.locations for y in b.locations
    )


def _same(a: Finding, b: Finding) -> bool:
    if a.source == "deterministic" or b.source == "deterministic":
        return False  # deterministic evidence stands on its own
    if a.refs and b.refs:
        return bool(set(a.refs) & set(b.refs)) and _close(a, b)
    return a.check == b.check and _close(a, b)


def _rank(f: Finding) -> tuple:
    return (f.blocking, not f.refuted, f.evidence, f.reproduction is not None)


def reduce(findings: list[Finding]) -> list[Finding]:
    merged: list[Finding] = []
    for f in sorted(findings, key=_rank, reverse=True):
        for m in merged:
            if _same(m, f):
                if f.source not in m.source.split("+"):
                    m.source += "+" + f.source
                m.refs += [r for r in f.refs if r not in m.refs]
                break
        else:
            merged.append(f.model_copy(deep=True))
    for i, f in enumerate(merged, 1):
        f.id = f"E-{i:03d}"
    return merged
