---
name: debugging
description: Root-cause debugging discipline. Use when a test fails, behaviour is wrong, or the factory stop gate reports a failure.
---
# Debugging

1. Reproduce: get a command that fails deterministically, and keep it.
2. Read the actual error and the code path it names. Do not reason from the symptom alone.
3. Form one hypothesis and find the cheapest observation that would refute it.
4. Fix the root cause in the shared code path, not in the one caller that surfaced it.
5. Keep the reproduction as a regression test annotated with the criterion it protects.
6. Re-run the full configured test command, not only the one test.

Never suppress an exception, loosen an assertion, or special-case the failing input to get green.
