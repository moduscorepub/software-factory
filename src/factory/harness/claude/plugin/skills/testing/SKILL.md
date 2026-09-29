---
name: testing
description: Write tests that prove Work Contract acceptance criteria and pin existing behaviour. Use when adding or changing tests for a factory task.
---
# Testing

- Every acceptance criterion gets at least one test. Annotate it in the docstring or an adjacent
  comment with `covers <WORK-ID>/<A-ID>`; that annotation is how the factory traces requirement to test.
- Use the criterion's concrete values. A test that would pass against a broken implementation proves
  nothing.
- Test the failure path the criterion implies (bad input, boundary, empty, duplicate), not only the
  happy path.
- Bug fixes: first write a test that fails on the old code and watch it fail, then fix.
- Existing behaviour the change could break gets a pinning test before you change it.
- Never weaken, skip or delete a failing test to get green. Fix the code or report the conflict.
- Run the tests you wrote. A test that never executed is not evidence.
