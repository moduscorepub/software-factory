---
name: investigation
description: Investigate unfamiliar code before changing it. Use in the UNDERSTAND phase, or whenever a request touches code you have not read.
---
# Investigation

- Map before you move: entry point, call path, then the behaviour in question, citing file:line.
- Find the tests that pin current behaviour, and note the behaviour nothing pins; regressions hide there.
- Check the constitution and the contract's invariants for rules this code must keep.
- Separate what you read from what you infer, and label inference.
- Stop when you can say which files change, why, and how each acceptance criterion will be proven.
  Delegate wide searches to the `investigator` agent.
