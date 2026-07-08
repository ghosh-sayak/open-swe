"""Post a run notification to the triggering GitHub source.

Slack and Linear are retired; the only push-notification channel is the GitHub
PR/issue that triggered the run. Dashboard-triggered runs surface status in the
dashboard UI, so they need no push here.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from .github_app import get_github_app_installation_token
from .github_comments import post_github_comment
from .github_token import get_github_token

logger = logging.getLogger(__name__)


def _coerce_issue_number(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def get_github_target(configurable: Mapping[str, Any]) -> tuple[dict[str, str], int] | None:
    """Resolve the (repo, issue/PR number) to notify from a run's configurable."""
    repo_config = configurable.get("repo")
    if not isinstance(repo_config, Mapping):
        return None
    owner = repo_config.get("owner")
    name = repo_config.get("name")
    if not isinstance(owner, str) or not isinstance(name, str) or not owner or not name:
        return None
    repo = {"owner": owner, "name": name}

    github_pr_or_issue = configurable.get("github_pr_or_issue")
    if isinstance(github_pr_or_issue, Mapping):
        number = _coerce_issue_number(github_pr_or_issue.get("number"))
        target_repo = github_pr_or_issue.get("repo")
        if isinstance(target_repo, Mapping):
            target_owner = target_repo.get("owner")
            target_name = target_repo.get("name")
            if isinstance(target_owner, str) and isinstance(target_name, str):
                repo = {"owner": target_owner, "name": target_name}
        if number is not None:
            return repo, number

    github_issue = configurable.get("github_issue")
    if isinstance(github_issue, Mapping):
        number = _coerce_issue_number(github_issue.get("number"))
        if number is not None:
            return repo, number

    pr_number = _coerce_issue_number(configurable.get("pr_number"))
    if pr_number is not None:
        return repo, pr_number
    return None


async def post_source_notification(config: Mapping[str, Any], message: str) -> bool:
    """Post ``message`` to the run's GitHub source. Returns whether it was posted."""
    configurable = config.get("configurable", {})
    if not isinstance(configurable, Mapping):
        return False
    target = get_github_target(configurable)
    if target is None:
        logger.info("No GitHub source target for run notification")
        return False
    repo, issue_number = target
    token = get_github_token(config) or await get_github_app_installation_token()
    if not token:
        logger.info("No GitHub token available for run notification")
        return False
    await post_github_comment(repo, issue_number, message, token=token)
    logger.info("Posted run notification to GitHub item #%s", issue_number)
    return True
