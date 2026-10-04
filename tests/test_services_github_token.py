"""The scan child's side of the token file (M2d-2 plan sections 0.2 #5, 4.6).

``github_token()`` re-reads ``WHYGRAPH_GITHUB_TOKEN_FILE`` on every call, and
every ``gh`` / network ``git`` subprocess gets the token current at that
moment in its own environment; nothing writes it into ``os.environ``.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from whygraph.cli.commands.scan import _apply_github_token
from whygraph.services.git import Repository
from whygraph.services.git.credentials import (
    TOKEN_ENV_VAR,
    TOKEN_FILE_ENV,
    github_git_config,
)
from whygraph.services.git.commands import GitFetchRefsCmd
from whygraph.services.github import GitHubClient
from whygraph.services.github.commands import paginate_graphql
from whygraph.services.github.pull_requests import PullRequests
from whygraph.services.github.token import github_token, token_env

FIRST, SECOND = "ghs_first_token_0001", "ghs_second_token_0002"


@pytest.fixture
def token_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "9.token"
    path.write_text(FIRST)
    for var in ("GH_TOKEN", "GITHUB_TOKEN", TOKEN_ENV_VAR):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(TOKEN_FILE_ENV, str(path))
    return path


class RecordingShell:
    """A ``Shell`` stand-in: records each call's env, rewrites the token file after it."""

    def __init__(self, responses: list, token_file: Path | None = None) -> None:
        self.responses = list(responses)
        self.envs: list[dict | None] = []
        self.kwargs: list[dict] = []
        self.token_file = token_file

    def run(self, cmd, *, env=None, **kwargs):  # noqa: ANN001, ANN201
        self.envs.append(env)
        self.kwargs.append(kwargs)
        if self.token_file is not None:
            self.token_file.write_text(SECOND)  # the portal refreshed it
        response = self.responses.pop(0)
        if hasattr(cmd, "parse"):
            return response
        return subprocess.CompletedProcess(cmd, 0, "", "")


def test_github_token_rereads_the_file_on_every_call(token_file: Path) -> None:
    assert github_token() == FIRST
    token_file.write_text(SECOND + "\n")
    assert github_token() == SECOND
    token_file.unlink()
    assert github_token() is None  # never a stale value


def test_without_a_file_the_environment_token_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(TOKEN_FILE_ENV, raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "ghp_env")
    assert github_token() == "ghp_env"
    assert token_env("GH_TOKEN") is None  # inherit, as before
    assert github_token({}) is None


def test_every_graphql_page_gets_the_current_token(token_file: Path) -> None:
    page = {"repository": {"pullRequests": {"nodes": [{"n": 1}], "pageInfo": {}}}}
    more = {
        "repository": {
            "pullRequests": {
                "nodes": [{"n": 0}],
                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
            }
        }
    }
    shell = RecordingShell([more, page], token_file=token_file)
    nodes = list(
        paginate_graphql(
            shell,  # type: ignore[arg-type]
            query="q",
            path=("repository", "pullRequests"),
            variables={"owner": "o", "name": "n"},
        )
    )
    assert nodes == [{"n": 0}, {"n": 1}]
    assert [e["GH_TOKEN"] for e in shell.envs] == [FIRST, SECOND]  # type: ignore[index]
    assert "GH_TOKEN" not in os.environ


def test_the_count_query_gets_the_current_token(token_file: Path) -> None:
    shell = RecordingShell([{"repository": {"pullRequests": {"totalCount": 3}}}])
    assert len(PullRequests("o", "n", shell=shell)) == 3  # type: ignore[arg-type]
    assert shell.envs[0]["GH_TOKEN"] == FIRST  # type: ignore[index]


def test_check_auth_uses_the_file_and_keeps_its_output_out_of_the_log(
    token_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = RecordingShell([None])
    monkeypatch.setattr("whygraph.services.github.client.Shell", lambda: shell)
    GitHubClient.check_auth()
    assert shell.envs[0]["GH_TOKEN"] == FIRST  # type: ignore[index]
    assert shell.kwargs[0]["log_output"] is False


def test_the_shell_trace_can_leave_output_out(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from whygraph.core import Shell

    # An earlier CLI test's configure_logging stops "whygraph" propagating.
    monkeypatch.setattr(logging.getLogger("whygraph"), "propagate", True)
    caplog.set_level(logging.DEBUG, logger="whygraph.core.shell")
    printed = ["sh", "-c", "printf 'ghs_%s' printed_by_a_tool"]
    Shell().run(printed, log_output=False)
    assert "printed_by_a_tool" in caplog.text  # the command line is traced
    assert "ghs_printed_by_a_tool" not in caplog.text
    Shell().run(printed)
    assert "ghs_printed_by_a_tool" in caplog.text  # the default still traces it


def test_pr_ref_fetches_hand_the_file_token_to_git(
    token_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = GitFetchRefsCmd("refs/pull/1/head:refs/whygraph/pull/1").argv()
    config = list(github_git_config())
    assert argv[: 1 + len(config)] == ["git", *config]
    assert all(FIRST not in a for a in argv)

    repo = Repository(tmp_path)
    shell = RecordingShell([None, None], token_file=token_file)
    monkeypatch.setattr(repo, "_shell", shell)
    repo.fetch_refs(["refs/pull/1/head:refs/whygraph/pull/1"])
    repo.fetch_refs(["refs/pull/2/head:refs/whygraph/pull/2"])
    assert [e[TOKEN_ENV_VAR] for e in shell.envs] == [FIRST, SECOND]  # type: ignore[index]
    assert TOKEN_ENV_VAR not in os.environ


def test_apply_github_token_leaves_the_environment_alone_with_a_file(
    token_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Recorded so teardown removes what _apply_github_token writes.
    monkeypatch.setenv("GH_TOKEN", "placeholder")
    monkeypatch.delenv("GH_TOKEN")
    config = SimpleNamespace(scan_forge="auto", scan_token="ghp_from_toml")
    _apply_github_token(config)  # type: ignore[arg-type]
    assert "GH_TOKEN" not in os.environ
    monkeypatch.delenv(TOKEN_FILE_ENV)
    _apply_github_token(config)  # type: ignore[arg-type]
    assert os.environ["GH_TOKEN"] == "ghp_from_toml"
