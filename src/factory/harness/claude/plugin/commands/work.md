---
description: Bind this session to a factory task and load its Work Contract context
argument-hint: <JIRA-KEY | WORK-ID/T-ID>
allowed-tools: Bash(factory work:*)
---
Run `factory work $ARGUMENTS --bind` and treat its output as the approved task context.

Then work the task: UNDERSTAND the code involved, PLAN how each acceptance criterion will be proven,
IMPLEMENT the smallest correct change, VERIFY with the project commands, REFLECT on scope, and commit
with the `Factory-Task:` trailer the context names.
