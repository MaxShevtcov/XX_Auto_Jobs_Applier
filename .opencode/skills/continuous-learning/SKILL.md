---
name: continuous-learning
description: Instinct-based learning system that extracts patterns from sessions, creates atomic instincts with confidence scoring, and evolves them into skill refinements. Adapted from ECC continuous-learning-v2.
---

# Continuous Learning

Learn from each session and improve skills/agents over time. Every development session generates reusable patterns — this skill ensures they don't get lost.

## Core Loop

```
Session work → extract patterns → store instincts → evolve skills → better future sessions
```

## How It Works

### 1. Pattern Extraction (end of each session)

When a task is substantially complete, review the session for:

**Pattern Types**

| Type | What to look for | Example |
|------|-----------------|---------|
| Error resolution | How you fixed a build/test error | "playwright install chromium after fresh venv" |
| User correction | User corrected your approach | "Use `QdrantClient.query()` not `.search()` for hybrid" |
| Workflow quirk | Non-obvious command/order | `set -a && source .env && set +a` before ingestion |
| Repo convention | Code style or structure rule | "DTOs in `api/*/dto.ts`, services in `services/`" |
| Config gotcha | Environment setup nuance | `OPENAI_MODEL` falls back to `gpt-4o-mini` on 404 |

### 2. Instinct Storage

Store each pattern as an atomic instinct in `.opencode/learned/`:

```yaml
# .opencode/learned/YYYY-MM-DD--short-name.yaml
---
id: playwright-install-after-fresh-venv
trigger: after creating a fresh venv or cloning the repo
action: run playwright install chromium before running browser automation
confidence: 0.8
domain: workflow
source: session-observation
project: xx-auto-jobs-applier
---

# Playwright install after fresh venv

## Evidence
- Fresh venv without browsers broke job_applier tests/run
- Resolved by running playwright install chromium
```

### 3. Confidence Scoring

| Score | Meaning | Behavior |
|-------|---------|----------|
| 0.3 | Tentative | Mention if relevant |
| 0.5 | Moderate | Apply when context matches |
| 0.7 | Strong | Auto-apply |
| 0.9 | Near-certain | Treat as rule |

**Increases**: pattern repeated, user agrees. **Decreases**: contradicted, not observed.

### 4. Skill Evolution

When 3+ related instincts accumulate (`/evolve`), cluster them into skill updates:

1. Create `.opencode/skills/<name>/SKILL.md` with the patterns
2. Register it in `opencode.json` → `skills.paths`
3. Add an agent in `opencode.json` → `agent` if a specialized role emerges
4. Update `AGENTS.md` if the pattern affects workflow commands

## Commands

| Invocation | What it does |
|------------|-------------|
| `/learn` | Extract patterns from current session, store as instincts |
| `/evolve` | Cluster related instincts into skill/agent proposals |
| `/instinct-status` | List all stored instincts with confidence scores |

## Instinct File Format

```yaml
---
id: kebab-case-unique-id
trigger: "situation that triggers this"
action: "what the agent should do"
confidence: 0.7  # 0.3-0.9
domain: workflow|code-style|testing|config|debugging
source: session-extraction
project: xx-auto-jobs-applier
---

# Human-readable title

## Evidence
- What happened in the session
- Why this pattern matters
- How it was resolved

## Example
```bash
# concrete command or code snippet
```
```

## Evolution Trigger

Run `/evolve` when you notice:
- 3+ instincts in the same domain (e.g., "testing")
- A pattern has been seen 5+ times (confidence ≥ 0.7)
- A workflow is stable enough to codify
- Skills feel incomplete or missing important patterns
