# PROGRESS — Live Tracking

> Place at `c:\users\avych\pm-research\reverse engineering\PROGRESS.md`. The agent updates this **after every meaningful action**, before doing anything else. The human operator reads this to know where the project stands.

---

## Format

This file has three top-level sections that are always present:

```markdown
## CURRENT
**Phase**: <N> — <name>
**Sub-step**: <X.Y> — <name>
**Started**: <ISO timestamp UTC>

## JUST DID
<one paragraph: what action was just taken, what file(s) changed, what was produced, what tests pass>

## NEXT
<one paragraph: the immediately next action; expected output; acceptance criterion>
```

After these three, the file maintains a chronological log of completed sub-steps:

```markdown
---

## HISTORY (most recent first)

### <ISO timestamp> — <phase>.<sub-step> COMPLETE
<one paragraph summary; commit hash>

### <ISO timestamp> — <phase>.<sub-step> COMPLETE
...
```

---

## Update rules

1. **Update CURRENT, JUST DID, NEXT after every meaningful action.** No exceptions.
2. **Add to HISTORY when a sub-step completes (acceptance criterion met).** Include the commit hash for traceability.
3. **Timestamps in ISO-8601 UTC** (e.g., `2026-05-27T14:32:11Z`).
4. **The file must always parse as valid Markdown.** A broken progress file is a continuous-gate failure.
5. **Maximum size**: if HISTORY exceeds 200 entries, roll the oldest 100 to `LOGS/PROGRESS_ARCHIVE_<date>.md` and trim.

---

## Sub-step naming convention

`<phase>.<analytical-section>.<step>` matching the methodology:

- `0.4.1` — Phase 0, section 4 (first actions), sub-step 1 (create folder structure)
- `1.1` — Phase 1, sub-step 1 (reconcile fill counts)
- `4.4.b` — Phase 4 Layer 1 OLS combination
- `4.6` — Phase 4 Layer 2 structural ML estimation
- `5.4` — Phase 5 Layer 3 GBT residual model
- `7.2` — Phase 7 Layer 7 paper twin

---

## Initial state (when project starts)

```markdown
## CURRENT
**Phase**: 0 — Bootstrap
**Sub-step**: 0.4.1 — Create folder structure
**Started**: <ISO timestamp UTC>

## JUST DID
Read MASTER_PROMPT.md in full. Read parent project's docs/VERIFIED_FACTS.md. Validated environment (Python 3.12.x, AWS credentials loaded, S3 access confirmed). Project context understood; methodology internalized.

## NEXT
Create the folder structure under `c:\users\avych\pm-research\reverse engineering\` exactly as specified in MASTER_PROMPT.md §4.1. Initialize empty working documents (PROGRESS.md, DECISIONS.md, EXPERIMENTS.md, BLOCKERS.md, RESULTS.md). Write METHODOLOGY.md verbatim from MASTER_PROMPT.md §9. Acceptance: `ls -la "c:/users/avych/pm-research/reverse engineering/"` shows every expected entry; `make precommit` runs (and passes on empty scaffold).

---

## HISTORY (most recent first)

(empty — project just started)
```

---

## Anti-patterns (the agent must not do these)

- ❌ Update CURRENT but forget JUST DID. → Both must be current.
- ❌ Write vague NEXT like "continue with analysis". → NEXT specifies the exact action and its acceptance.
- ❌ Skip HISTORY when a sub-step completes. → Every completion logged.
- ❌ Update PROGRESS.md as part of an unrelated commit. → PROGRESS updates can ride along with the work-commit (`docs(progress)` scope), or be a standalone commit, but never lost.
- ❌ Use timestamps in local time. → Always UTC ISO-8601.
- ❌ Edit HISTORY entries retroactively. → Append-only. If a correction is needed, append a new entry with the correction.
