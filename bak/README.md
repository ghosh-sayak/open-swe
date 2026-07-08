# `bak/` — quarantined (retired) code

This directory holds code removed from the live application but kept for a glance-level
restore, per `plans/opensandbox-backend-integration.md` §15. It **mirrors the real repo tree**
(a file quarantined from `agent/tools/foo.py` lands at `bak/agent/tools/foo.py`).

**It is tracked in git but excluded from all tooling** so it can never affect the build:
- ruff `extend-exclude = ["bak"]` (pyproject.toml)
- pytest `testpaths = ["tests"]` (never collects `bak/`)
- `.dockerignore` excludes `bak`
- nothing imports it (the import path never reaches `bak/`)

Two quarantine shapes:
- **Fully-dead file** → moved whole via `git mv <path> bak/<path>`.
- **Partially-dead file** → the *entire original file* was copied here as a point-in-time
  snapshot (dead code intact) **before** the live copy was stripped, so recovery has full
  surrounding context. Git history is the deeper safety net.

To restore an entry: reverse the `git mv` (fully-dead), or re-apply the removed portion from the
snapshot here (partial). Or `git revert` the decommission commit wholesale.

---

## Quarantined 2026-07-08 — Slack + Linear retired

**Why:** the agent is triggered exclusively via **GitHub** (PR comments / auto-review) and the
**dashboard/UI** (Agents chat thread API). All Slack- and Linear-specific code is dead weight.
No graph was removed (`agent`, `reviewer`, `analyzer`, `chat`, `scheduler`, and the FastAPI app
all remain); notification fan-out and mid-run message injection were reduced to GitHub + dashboard.

### Fully-dead files (whole-file moves)
_(populated below as files are moved)_

### Partially-dead files (snapshot here; stripped in place)
_(populated below as files are snapshotted)_
