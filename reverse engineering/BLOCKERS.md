# BLOCKERS

Current blockers. Empty = no blockers. Updated whenever a blocker is
encountered or resolved. Each entry records: timestamp, phase, task,
what was attempted, what failed, what's needed.

---

## BLOCKER-001 — Missing local .env file (Phase 0 acceptance gate item)

**Timestamp**: 2026-05-28T17:00:00Z
**Phase**: 0
**Task**: `make sync` — sync one partition per feed from S3 to local cache

**What was attempted**: Running S3 sync requires AWS credentials. Checked:
- `C:\Users\avych\pm-research\.env` — file does not exist (only `.env.example`)
- `C:\Users\avych\.aws\credentials` — directory does not exist
- Environment variables `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` — not set

**What failed**: Cannot run `make sync` or connect to S3 without credentials.

**What's needed**: Create `C:\Users\avych\pm-research\.env` from `.env.example`
with valid AWS credentials having `s3:GetObject` + `s3:ListBucket` on
`s3://pm-research-data/`. The credentials are available on the EC2 instance
(check `/var/pm-research/.env` or the EC2 IAM role).

**Workaround**: All other Phase 0 acceptance items are complete. Once .env is
created with valid credentials, run `make sync` and update RESULTS.md.

**Does not block Phase 0 commit**: All code, docs, tests, GPU check, and EC2
health check are complete. Committing now; mark BLOCKER-001 resolved after
.env is populated.

