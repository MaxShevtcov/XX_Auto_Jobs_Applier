---
description: Extract patterns and learnings from current session, store as instincts
---

# Learn

Analyze the current session to extract reusable patterns. Look at what was done, what errors occurred, what corrections were given, and what conventions emerged.

## Extraction Focus

1. **Error resolutions** — how build/test/runtime errors were fixed
2. **User corrections** — user preferences or approach corrections
3. **Workflow patterns** — non-obvious command sequences or orders
4. **Config gotchas** — env vars, proxy setup, Docker nuances
5. **Code conventions** — repo-specific patterns noticed during work

## Output

For each pattern found, create an instinct file at `.opencode/learned/YYYY-MM-DD--kebab-id.yaml` using the format from `skills/continuous-learning/SKILL.md`.

If patterns relate to an existing skill in `.opencode/skills/`, flag it as a candidate for `/evolve`.

## Instinct Format Reminder

```yaml
---
id: kebab-case-id
trigger: "when [situation]"
action: "do [this]"
confidence: 0.7
domain: workflow|code-style|testing|config|debugging|security
source: session-extraction
project: xx-auto-jobs-applier
---

# Title
## Evidence
- What happened
```
