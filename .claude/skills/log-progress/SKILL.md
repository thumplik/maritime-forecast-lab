---
name: log-progress
description: Append a dated entry to docs/implementation-log.md recording what was done, commands run, results, blockers and the next step. Use at the end of any work session or after finishing a milestone task.
---

Append (never rewrite) an entry to `docs/implementation-log.md` using this template. Keep the "Current milestone" line at the top of the file accurate.

```markdown
## YYYY-MM-DD — <short title>

**Milestone:** <n> — <name>
**Done:**
- <task> (<files>)

**Commands run:**
```bash
<exact commands>
```

**Results:** <numbers with run id / manifest; or "none">
**Blockers / open questions:** <or "none">
**Next step:** <smallest next slice>
```

Rules: only record numbers that a command in this entry (or a cited run id) produced. If a step was skipped or failed, say so.
