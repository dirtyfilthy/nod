---
name: plan
description: Make a concise implementation plan before changing code or systems. Use when a task is broad, ambiguous, complex, risky, or likely to touch multiple files or systems; when the right approach is unclear or there are trade-offs to weigh; or before any change that could break something non-obvious. Skip for single-file edits with an obvious fix.
---

# Plan

Use this skill when a task is broad, ambiguous, risky, or likely to touch multiple files or systems.

## Planning checklist

1. Restate the goal in one or two sentences.
2. Gather enough context to avoid guessing. Read the relevant code, config, docs, or logs first.
3. Identify the smallest safe change that satisfies the goal.
4. List the concrete steps in execution order.
5. Call out trade-offs, risks, and anything that needs the user's decision.
6. Define how the work will be verified: tests, smoke checks, manual checks, or deployment checks.

Keep the plan proportional. A small edit needs a small plan; an architectural change needs a more careful one.

Do not treat the plan as permission to overbuild. Prefer existing patterns, narrow edits, and clear verification.
