"""Tests for agent.utils.repo repo extraction from text."""

from agent.utils.repo import extract_repo_from_text


class TestExtractRepoFromText:
    def test_repo_colon_with_org(self) -> None:
        result = extract_repo_from_text("please use repo:my-org/my-repo")
        assert result == {"owner": "my-org", "name": "my-repo"}

    def test_repo_space_with_org(self) -> None:
        result = extract_repo_from_text("please use repo langchain-ai/langchainjs")
        assert result == {"owner": "langchain-ai", "name": "langchainjs"}

    def test_repo_colon_name_only_uses_default_owner(self) -> None:
        result = extract_repo_from_text("fix bug in repo:langchainplus")
        assert result == {"owner": "langchain-ai", "name": "langchainplus"}

    def test_repo_space_name_only_uses_default_owner(self) -> None:
        result = extract_repo_from_text("fix bug in repo open-swe")
        assert result == {"owner": "langchain-ai", "name": "open-swe"}

    def test_repo_name_only_custom_default_owner(self) -> None:
        result = extract_repo_from_text("repo:my-repo", default_owner="custom-org")
        assert result == {"owner": "custom-org", "name": "my-repo"}

    def test_github_url(self) -> None:
        result = extract_repo_from_text(
            "check https://github.com/langchain-ai/langgraph-api please"
        )
        assert result == {"owner": "langchain-ai", "name": "langgraph-api"}

    def test_explicit_repo_beats_github_url(self) -> None:
        result = extract_repo_from_text(
            "see https://github.com/langchain-ai/langgraph-api but use repo:my-org/my-repo"
        )
        assert result == {"owner": "my-org", "name": "my-repo"}

    def test_no_repo_returns_none(self) -> None:
        result = extract_repo_from_text("please fix the bug")
        assert result is None

    def test_empty_string_returns_none(self) -> None:
        result = extract_repo_from_text("")
        assert result is None

    def test_trailing_slash_stripped(self) -> None:
        result = extract_repo_from_text("repo:my-org/my-repo/")
        assert result == {"owner": "my-org", "name": "my-repo"}
