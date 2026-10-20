---
name: milestone-check
description: Verify the current milestone's acceptance ("Done when") criteria from docs/build-plan.md §7 before moving to the next milestone.
---

1. Read the current milestone from the top of `docs/implementation-log.md`, then its **Tasks** and **Done when** in `docs/build-plan.md` §7.
2. For each task and each acceptance criterion, find concrete evidence: a file, a passing test, a run id, a doc section. Run `make test` and `make lint`.
3. Produce a checklist: criterion → evidence (path or command output) → met / not met / blocked.
4. If everything is met, propose the smallest first slice of the next milestone. If not, list what remains. Never mark the milestone done on partial evidence — AGENTS.md rule 11.
5. Record the outcome with `/log-progress`.
