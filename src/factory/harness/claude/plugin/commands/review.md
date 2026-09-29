---
description: Run the factory evidence review of this branch (or a PR) against its Work Contract
argument-hint: [PR number]
allowed-tools: Bash(factory review:*)
---
Run `factory review $ARGUMENTS`.

Summarise blocking findings (E2 and above) first, then E1 findings that need human review. Treat E0
hypotheses as leads, not facts. For each blocking finding with a reproduction, explain the failing
behaviour and propose the fix.
