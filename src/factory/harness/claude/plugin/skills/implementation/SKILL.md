---
name: implementation
description: Implement a factory task from its Work Contract with the smallest correct change. Use when writing or changing production code for a bound task.
---
# Implementation

- Start from the acceptance criteria, not from the code you would like to write. Each criterion's
  given/when/then is the behaviour to build.
- Read before you write. Find the existing helpers, patterns and conventions and reuse them; a second
  way of doing the same thing is a defect.
- Make the smallest change that satisfies every criterion. Anything outside the task's requirements is
  scope creep: name it, do not build it.
- Decisions recorded in the contract are settled. If one is wrong, say so; never silently diverge.
- No placeholders: no TODO/FIXME, no stubbed branches, no fake fallbacks. The stop gate rejects them.
- Invariants and constitution laws outrank convenience.
- Commit with the `Factory-Task:` trailer the task context names.
