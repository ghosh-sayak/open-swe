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

### Fully-dead files (whole-file `git mv` moves)

Restore any with the reverse move, e.g. `git mv bak/<path> <path>`.

Source:
- `agent/tools/linear_comment.py`, `linear_create_issue.py`, `linear_delete_issue.py`,
  `linear_get_issue.py`, `linear_get_issue_comments.py`, `linear_list_teams.py`,
  `linear_update_issue.py`, `slack_read_thread_messages.py`, `slack_thread_reply.py`
- `agent/utils/slack.py`, `linear.py`, `slack_feedback.py`, `linear_team_repo_map.py`, `comments.py`
- `agent/middleware/refresh_slack_status.py`
- `agent/webhooks/slack.py`, `linear.py`
- `agent/dashboard/slack_oauth.py`

Tests (whole-file moves):
- `tests/test_account_link.py`, `test_recent_comments.py`, `test_refresh_slack_status_middleware.py`,
  `test_slack_assistants_status.py`, `test_slack_context.py`, `test_slack_feedback.py`,
  `test_slack_oauth.py`, `test_slack_thread_reply_tool.py`

### Partially-dead files (stripped in place; git history is the snapshot)

Slack/Linear portions removed, GitHub/dashboard behavior kept:
- `agent/server.py`, `agent/reviewer.py`, `agent/webapp.py` (all Slack/Linear routes/verifiers/
  handlers + helpers), `agent/completion.py`, `agent/dispatch.py`, `agent/prompt.py`
- `agent/tools/__init__.py`, `open_pull_request.py`, `publish_review.py`, `request_pr_review.py`,
  `schedule_thread_wakeup.py`
- `agent/middleware/__init__.py`, `check_message_queue.py`, `ensure_no_empty_msg.py`,
  `notify_step_limit.py`, `sandbox_circuit_breaker.py` (notification fan-out only — D5 matching
  untouched), `workflow_push_guard.py`
- `agent/reviewer_findings.py` (`ReviewerSlackThread`)
- `agent/utils/auth.py`, `authorship.py`, `multimodal.py`, `reviewer_outcomes.py`, `thread_ops.py`
- `agent/dashboard/routes.py`, `oauth.py`, `user_mappings.py` (Slack-id half), `plan_api.py`,
  `thread_api.py`, `agent_overrides.py`, `agent_usage.py`
- `agent/webhooks/github.py` (Slack params in the PR-review trigger)

New neutral extractions (not quarantined): `agent/utils/github_pr.py` (GitHubPrRef +
parse_github_pr_url, moved out of the retired `utils/slack.py`) and `agent/utils/source_notify.py`
(the GitHub-only run-notification helper that replaced the Slack→Linear→GitHub fan-out).

### Follow-up / behavior notes
- **No mapping writer remains.** `dashboard/user_mappings.upsert_mapping` was written only by the
  Slack OAuth callback (removed). The store's read/delete API is intact and `upsert_mapping` is kept
  as the write API, but a dashboard/admin "link GitHub↔email" flow is needed to populate mappings.
- Retired env vars (no longer read by live code): `SLACK_SIGNING_SECRET`, `SLACK_BOT_TOKEN`,
  `SLACK_BOT_USER_ID`, `SLACK_BOT_USERNAME`, `SLACK_REPO_OWNER`, `SLACK_REPO_NAME`,
  `SLACK_CLIENT_ID`/`SLACK_CLIENT_SECRET` (slack_oauth), `LINEAR_WEBHOOK_SECRET`, `LINEAR_API_KEY`.
- No graph was removed from `langgraph.json` (agent, reviewer, analyzer, chat, scheduler + FastAPI app).
