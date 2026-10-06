"""Remote-URL parsing in :mod:`whygraph.services.github.client` (M2e section 4.8).

The URL patterns were generalized to capture the **host**, so a checkout's
``origin`` can be matched against a platform project's clone URL on any host
(:func:`~whygraph.services.github.remote_identity`). The GitHub client itself
must keep filtering ``github.com``, which is what
:meth:`GitHubClient.for_repository` is checked for here.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from whygraph.services.github import GitHubClient, remote_identity


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/acme/demo", ("github.com", "acme", "demo")),
        ("https://github.com/acme/demo.git", ("github.com", "acme", "demo")),
        ("https://github.com/acme/demo.git/", ("github.com", "acme", "demo")),
        ("git@github.com:acme/demo.git", ("github.com", "acme", "demo")),
        ("ssh://git@github.com/acme/demo.git", ("github.com", "acme", "demo")),
        # The host is captured, so another forge matches just as well.
        ("https://git.acme.internal/acme/demo", ("git.acme.internal", "acme", "demo")),
        ("git@git.acme.internal:acme/demo.git", ("git.acme.internal", "acme", "demo")),
        # Host, owner and name are compared case-insensitively.
        ("https://GitHub.com/ACME/Demo.git", ("github.com", "acme", "demo")),
        # The port is not part of the identity: one host's https and ssh
        # remotes name the same repository.
        (
            "https://git.acme.internal:8443/acme/demo",
            ("git.acme.internal", "acme", "demo"),
        ),
        (
            "ssh://git@git.acme.internal:2222/acme/demo",
            ("git.acme.internal", "acme", "demo"),
        ),
        ("  https://github.com/acme/demo  ", ("github.com", "acme", "demo")),
    ],
)
def test_remote_identity_reads_every_supported_url_form(
    url: str, expected: tuple[str, str, str]
) -> None:
    assert remote_identity(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "/home/me/repos/demo",  # a local path
        "file:///home/me/repos/demo",
        "https://user@github.com/acme/demo",  # credentials (strip them first)
        "https://gitlab.com/group/sub/demo",  # a nested group path
        "https://github.com/acme",  # no repository
        "ftp://github.com/acme/demo",
    ],
)
def test_remote_identity_refuses_anything_else(url: str | None) -> None:
    assert remote_identity(url) is None


def test_remote_identity_matches_across_url_forms() -> None:
    """What the link's origin check relies on (plan section 0.1 #12)."""
    assert remote_identity("https://github.com/acme/demo.git") == remote_identity(
        "git@github.com:Acme/Demo"
    )
    assert remote_identity("https://github.com/acme/demo") != remote_identity(
        "https://github.com/acme/other"
    )
    assert remote_identity("https://github.com/acme/demo") != remote_identity(
        "https://git.acme.internal/acme/demo"
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/acme/demo.git", ("acme", "demo")),
        ("git@github.com:acme/demo.git", ("acme", "demo")),
        ("ssh://git@github.com/acme/demo", ("acme", "demo")),
        # Case is preserved: it is the repository's name on GitHub.
        ("https://github.com/Acme/Demo", ("Acme", "Demo")),
        # Another forge, a non-default port and a local path are not GitHub.
        ("https://git.acme.internal/acme/demo", None),
        ("https://github.com:8443/acme/demo", None),
        ("/home/me/repos/demo", None),
        (None, None),
    ],
)
def test_for_repository_still_only_accepts_github_com(
    url: str | None, expected: tuple[str, str] | None
) -> None:
    client = GitHubClient.for_repository(SimpleNamespace(origin_url=url))
    if expected is None:
        assert client is None
    else:
        assert client is not None
        assert (client.owner, client.name) == expected
