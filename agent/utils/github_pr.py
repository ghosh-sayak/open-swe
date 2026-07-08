"""GitHub pull-request URL helpers (provider-neutral)."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class GitHubPrRef:
    owner: str
    repo: str
    number: int
    url: str


def parse_github_pr_url(url: str) -> GitHubPrRef | None:
    cleaned_url = url.strip().strip("<>")
    if "|" in cleaned_url:
        cleaned_url = cleaned_url.split("|", 1)[0]

    parsed = urlparse(cleaned_url)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return None

    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 4 or path_parts[2] != "pull":
        return None

    try:
        number = int(path_parts[3])
    except ValueError:
        return None

    owner = path_parts[0]
    repo = path_parts[1]
    return GitHubPrRef(
        owner=owner,
        repo=repo,
        number=number,
        url=f"https://github.com/{owner}/{repo}/pull/{number}",
    )
