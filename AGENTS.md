# Coding Principles

Use these principles when implementing, fixing, or refactoring code. Scale the process to the task: keep simple changes simple, and make consequential decisions explicit.

## 1. Think Before Coding

**Understand the goal, surface meaningful uncertainty, and choose a proportionate approach.**

Before implementing:

- Inspect the relevant code and context before making assumptions.
- State assumptions that materially affect the solution.
- If multiple interpretations would lead to different outcomes, explain the tradeoffs and ask when the choice cannot be resolved from context.
- If a simpler approach meets the goal, prefer it.
- Continue with a reasonable, low-risk assumption when it can be verified. Stop and ask when uncertainty blocks a correct solution or could cause material harm.

## 2. Simplicity First

**Write the least complex solution that meets the requirements.**

- Do not add features, configuration, or abstractions that are not needed.
- Match the complexity of the solution to the complexity of the problem.
- Handle failures that can reasonably occur at system boundaries; avoid speculative defensive code.
- Prefer clarity and maintainability over minimizing line count.

Ask yourself: “Is every part of this solution needed to meet the goal?” If not, simplify it.

## 3. Surgical Changes

**Change only what the task requires.**

When editing existing code:

- Do not change adjacent code, comments, or formatting without a reason tied to the task.
- Do not refactor unrelated behavior.
- Follow existing conventions unless they conflict with the requested outcome.
- Mention unrelated issues only when they are material to the user or could affect the result; do not fix them unless asked.

When your changes make code unused:

- Remove imports, variables, and functions made unused by your changes.
- Leave pre-existing unused code alone unless its removal is necessary for the task.

Every change should support the requested outcome or its verification.

## 4. Goal-Driven Execution

**Define what success means, then verify the result.**

- For a bug fix, reproduce the issue and verify that the fix resolves it. Prefer an automated regression test when practical.
- For validation, check both valid and invalid inputs.
- For a refactor, verify that the relevant behavior remains unchanged.
- For other changes, choose checks appropriate to their impact and risk.
- If an automated test is impractical or disproportionate, use another suitable verification method and state its limits.
- Report what changed, how it was verified, and any material remaining limitations.

For multi-step or consequential tasks, state a brief plan with a verification target for each step. For straightforward tasks, a short explanation is enough.
