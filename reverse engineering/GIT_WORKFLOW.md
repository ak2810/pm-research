# GIT WORKFLOW — Commit Discipline

> Place at `c:\users\avych\pm-research\reverse engineering\GIT_WORKFLOW.md`. The agent runs every commit through this protocol.

The reverse-engineering folder lives inside the parent `pm-research` repo. The repo is already wired to `https://github.com/ak2810/pm-research` as `origin`. All commits go to `main`.

---

## When to commit

After every **unit of completed work**. A unit is:
- A new file written (complete; no placeholders).
- A modification that completes a logical change (e.g., "fitted Layer 2 and wrote θ̂ to disk").
- A documentation update (PROGRESS / DECISIONS / EXPERIMENTS / RESULTS).
- A test added (with the implementation it covers).
- A phase acceptance gate passing.

Commits are small enough to be reviewable in <5 minutes. If the diff is large, split.

---

## Commit message format (conventional commits)

```
<type>(<scope>): <imperative summary, <72 chars>>

<optional body — what changed, why, references to PROGRESS.md entry>

<optional footer — refs, breaking changes>
```

**Types**:
- `feat` — new functionality (the most common)
- `fix` — bug fix
- `docs` — documentation only
- `test` — adding or modifying tests
- `refactor` — code change without behavior change
- `chore` — tooling, config, deps
- `analysis` — analytical result (notebook output, plots, statistical findings)
- `model` — model fitting, parameter estimates

**Scopes** (match phase or component):
- `phase0` … `phase7`
- `layer1` … `layer7`
- `tables`, `io`, `models`, `validation`, `tests`, `notebooks`, `docs`

**Examples**:
```
feat(phase0): bootstrap reverse-engineering project structure
feat(phase1): build ohanism_fills table from polygon Parquet
analysis(phase2): maker:taker ratio = 0.87, hypothesis MM confirmed
model(layer2): structural ML fitted, theta_hat converged with PD Hessian
fix(layer1): correct sign on ohanism_side derived column
docs(progress): phase 4 complete, advancing to phase 5
test(digital_option): add boundary test for sigma inversion at p=0.99
```

---

## The commit protocol

For every commit, run **in order**:

```bash
# 1. Pre-commit gates (each must exit 0)
make precommit          # = mypy --strict + ruff check + ruff format --check + pytest -x

# 2. Verify no placeholders snuck in
grep -rEn 'TODO|FIXME|XXX|HACK|NotImplementedError|placeholder|"foo"|"bar"' src/ tests/

# 3. Stage and commit
cd c:/users/avych/pm-research
git add "reverse engineering/"
git status              # human-readable verify
git commit -m "feat(phaseN): ..."

# 4. Push
git push origin main

# 5. Verify push succeeded
git log -1 --pretty=oneline
git status              # should show "Your branch is up to date with 'origin/main'"
```

If any step fails:
- `make precommit` fails → fix issues, do not commit until clean.
- `grep` returns matches → remove the placeholders, do not commit.
- `git commit` fails → check `git status` for unmerged files; resolve.
- `git push` fails → check error:
  - `rejected — non-fast-forward` → `git pull --rebase origin main`, resolve conflicts, retry push.
  - auth error → log to `BLOCKERS.md` with full error output, stop.
  - network → wait 30s, retry once. If still fails, log to `BLOCKERS.md`, stop.

---

## Branch policy

You work directly on `main`. The user prefers a single-line history for this project. Do not create feature branches. If you ever need to revert, use `git revert <hash>`.

---

## What never goes into git

- **The SSH key `*.pem`** — never. `C:/Users/avych/pm-research-key.pem` and any copy stay out of the repo.
- **The local Parquet cache `output/cache/`** — never. It's synced from S3 and is regenerable; it would also be enormous.
- Files >50MB. For these, write the SHA-256 hash and S3 URI into `DECISIONS.md`, then list the path in `.gitignore`. The artifact lives on S3 at `s3://pm-research-data/reverse-engineering/<filename>`.
- AWS credentials, API keys, anything from `.env`.
- Model artifacts in `output/models/` (torch checkpoints, pickles) — gitignored; hash + S3 URI recorded in `DECISIONS.md`.
- `.pyc`, `__pycache__/`, `.mypy_cache/`, `.ruff_cache/`, `.pytest_cache/`, `.ipynb_checkpoints/`, `*.egg-info/`.
- Personal notebooks (use `notebooks/scratch/` for exploration; `.gitignore` that subdirectory).

---

## Daily snapshot commits

At the end of each working session — even if no phase boundary was hit — write a daily snapshot:

```bash
# At end of session
echo "Session end: $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> LOGS/$(date -u +%Y-%m-%d).md
echo "Phase: <N>, sub-step: <X>" >> LOGS/$(date -u +%Y-%m-%d).md
echo "Next action on resume: <Y>" >> LOGS/$(date -u +%Y-%m-%d).md
git add LOGS/
git commit -m "docs(session): EOD checkpoint, phase N sub-step X"
git push origin main
```

This ensures resume-from-anywhere is always clean.

---

## The "stop and log" rule

If at any point you cannot commit cleanly (gate failures, push errors, ambiguity about whether something is committable), you do not push partial state. You:
1. Save the working state to a stash: `git stash push -m "WIP: <reason>"`.
2. Write a `BLOCKERS.md` entry with full context.
3. Stop.

The repo state on `origin/main` is always in a known-good, gate-passing condition. Always.
