---
name: refactoring
description: Behaviour-preserving refactoring. Use when restructuring code without changing what it does.
---
# Refactoring

- A refactor changes structure, never behaviour. If behaviour must change, that is a task with
  acceptance criteria, not a refactor.
- Pin first: make sure tests cover the behaviour you are moving; add pinning tests where missing and run
  them before touching the code.
- Move in small steps and run the tests after each.
- Watch the quiet semantics: rounding, ordering, default arguments, error types and messages, time
  zones, None and empty handling. That is where harmless-looking refactors regress.
- Public API behaviour changes need an explicit compatibility decision (constitution C-API-004).
- Delete what the refactor made obsolete: no dead code, no aliases.
