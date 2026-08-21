---
description: Cluster related instincts into skill/agent updates
---

# Evolve

Cluster instincts from `.opencode/learned/` and propose skill improvements.

## Process

1. Load all `.opencode/learned/*.yaml` files
2. Group by `domain` field
3. For each domain with 2+ instincts at confidence ≥ 0.5:
   - Match against existing `.opencode/skills/*/`
   - Propose updates to existing skills or new skills
4. For each proposed change, show the user:
   - What instincts triggered it
   - What the change looks like
   - Ask for approval before applying

## Domain Mapping

| Instinct Domain | Likely Skill Target |
|----------------|-------------------|
| workflow | coding-standards or tdd-workflow |
| code-style | coding-standards |
| testing | tdd-workflow |
| config | coding-standards (раздел Конфигурация) |
| debugging | build-error-resolver agent |
| security | security-review or security-reviewer agent |
