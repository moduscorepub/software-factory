---
description: Run the factory's deterministic completion checks and fix what blocks
allowed-tools: Bash(factory verify:*)
---
Run `factory verify`. For every blocking finding, fix the cause (never weaken a test or hide a
failure), then re-run until it passes. Finish by reporting the final evidence table.
