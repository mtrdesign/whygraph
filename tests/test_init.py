from unittest.mock import patch

from whygraph.init import (
    Action,
    MIN_NODE_MAJOR,
    _bootstrap_nvm_then_run,
    _decide_action,
    _parse_node_version,
)


def test_parse_node_version_standard() -> None:
    assert _parse_node_version("v22.4.1\n") == (22, 4, 1)


def test_parse_node_version_no_newline() -> None:
    assert _parse_node_version("v18.0.0") == (18, 0, 0)


def test_parse_node_version_prerelease() -> None:
    assert _parse_node_version("v22.0.0-nightly20240101") == (22, 0, 0)


def test_parse_node_version_malformed() -> None:
    assert _parse_node_version("not a version") is None


def test_parse_node_version_empty() -> None:
    assert _parse_node_version("") is None


def test_decide_action_node_ok() -> None:
    assert _decide_action((MIN_NODE_MAJOR, 0, 0), has_nvm=False) is Action.NODE_OK
    assert _decide_action((MIN_NODE_MAJOR, 0, 0), has_nvm=True) is Action.NODE_OK
    assert _decide_action((99, 0, 0), has_nvm=False) is Action.NODE_OK


def test_decide_action_old_node_with_nvm() -> None:
    assert _decide_action((18, 0, 0), has_nvm=True) is Action.USE_NVM


def test_decide_action_old_node_without_nvm() -> None:
    assert _decide_action((18, 0, 0), has_nvm=False) is Action.BOOTSTRAP_NVM


def test_decide_action_missing_node_with_nvm() -> None:
    assert _decide_action(None, has_nvm=True) is Action.USE_NVM


def test_decide_action_missing_node_without_nvm() -> None:
    assert _decide_action(None, has_nvm=False) is Action.BOOTSTRAP_NVM


def test_bootstrap_declined_returns_1() -> None:
    with patch("click.confirm", return_value=False):
        rc = _bootstrap_nvm_then_run(assume_yes=False)
    assert rc == 1


def test_bootstrap_assume_yes_skips_prompt() -> None:
    class FakeResult:
        returncode = 99

    with (
        patch("click.confirm") as confirm_mock,
        patch("whygraph.init.subprocess.run", return_value=FakeResult()),
    ):
        rc = _bootstrap_nvm_then_run(assume_yes=True)

    assert not confirm_mock.called
    assert rc == 99
