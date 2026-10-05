"""The WhyGraph GitHub App client (M2d-2 plan sections 4.3, 4.11, 5.1).

The configuration checks (all five variables or none, an RSA key, a long
enough webhook secret), the app JWT, the repo-scoped installation tokens and
their cache, the access-lost mapping, the host guard, the user-authorization
calls against the fake, :class:`UserTokens`, and git over the fake's dumb
HTTP with a scoped installation token.
"""

# ruff: noqa: F811 -- pytest fixtures imported from test_portal_app

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from github_fake import FakeGitHub, _s256
from test_portal_app import (  # noqa: F401 -- fixtures
    GITHUB_APP_CLIENT_ID,
    GITHUB_APP_CLIENT_SECRET,
    GITHUB_APP_SLUG,
    GITHUB_WEBHOOK_SECRET,
    PROD_BASE,
    GitServer,
    env,
    github_app_key,
    github_git_server,
    prod_portal,
    production_env,
)
from whygraph.portal.app import PortalStartupError
from whygraph.services.git import (
    GitError,
    InvalidRepoUrlError,
    Repository,
    git_env,
    parse_github_url,
)
from whygraph.services.git.credentials import GITHUB_URL_ENV
from whygraph.portal.github_app import (
    APP_ENV_VARS,
    INSTALLATION_PERMISSIONS,
    GitHubAccessLost,
    GitHubApp,
    GitHubAppConfig,
    GitHubNotFound,
    GitHubTokenRejected,
    UserTokens,
    load_github_app_config,
)
from whygraph.portal.github_auth import (
    GitHubAuthFailed,
    GitHubHostError,
    GitHubUnavailable,
    pkce_challenge,
)

WEB = "http://127.0.0.1:9"
API = WEB + "/api/v3"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _pem(key) -> str:  # noqa: ANN001
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def app_env(tmp_path: Path, github_app_key: rsa.RSAPrivateKey) -> dict[str, str]:
    """A complete, valid set of the five variables (files in ``tmp_path``)."""
    secret = tmp_path / "app-secret"
    secret.write_text(GITHUB_APP_CLIENT_SECRET + "\n")
    key = tmp_path / "app-key.pem"
    key.write_text(_pem(github_app_key))
    hook = tmp_path / "webhook-secret"
    hook.write_text(GITHUB_WEBHOOK_SECRET + "\n")
    return {
        "WHYGRAPH_GITHUB_APP_SLUG": GITHUB_APP_SLUG,
        "WHYGRAPH_GITHUB_APP_CLIENT_ID": GITHUB_APP_CLIENT_ID,
        "WHYGRAPH_GITHUB_APP_CLIENT_SECRET_FILE": str(secret),
        "WHYGRAPH_GITHUB_APP_PRIVATE_KEY_FILE": str(key),
        "WHYGRAPH_GITHUB_APP_WEBHOOK_SECRET_FILE": str(hook),
    }


def test_the_config_is_required_and_names_every_variable() -> None:
    for environ in ({}, dict.fromkeys(APP_ENV_VARS, "  ")):
        with pytest.raises(
            ValueError, match="production mode needs the GitHub App"
        ) as info:
            load_github_app_config(environ)
        assert str(info.value).endswith("missing: " + ", ".join(APP_ENV_VARS))


def test_a_complete_config_loads_and_hides_its_secrets(
    app_env: dict[str, str], github_app_key: rsa.RSAPrivateKey
) -> None:
    config = load_github_app_config(app_env)
    assert config is not None
    assert (config.slug, config.client_id) == (GITHUB_APP_SLUG, GITHUB_APP_CLIENT_ID)
    assert config.client_secret == GITHUB_APP_CLIENT_SECRET
    assert config.webhook_secret == GITHUB_WEBHOOK_SECRET
    assert config.private_key.public_key().public_numbers() == (
        github_app_key.public_key().public_numbers()
    )
    assert (config.web_url, config.api_url) == (
        "https://github.com",
        "https://api.github.com",
    )
    shown = repr(config)
    assert GITHUB_APP_CLIENT_SECRET not in shown
    assert GITHUB_WEBHOOK_SECRET not in shown
    assert "PRIVATE" not in shown and "private_key" not in shown


def test_config_shares_the_github_urls(app_env: dict[str, str]) -> None:
    config = load_github_app_config(
        {
            **app_env,
            "WHYGRAPH_GITHUB_URL": "https://ghe.example.com/",
            "WHYGRAPH_GITHUB_API_URL": "https://ghe.example.com/api/v3",
        }
    )
    assert config is not None
    assert config.web_url == "https://ghe.example.com"
    assert config.api_url == "https://ghe.example.com/api/v3"
    with pytest.raises(ValueError, match="WHYGRAPH_GITHUB_URL"):
        load_github_app_config({**app_env, "WHYGRAPH_GITHUB_URL": "http://gh.example"})


@pytest.mark.parametrize("dropped", APP_ENV_VARS)
def test_a_partial_config_names_what_is_missing(
    app_env: dict[str, str], dropped: str
) -> None:
    partial = {k: v for k, v in app_env.items() if k != dropped}
    with pytest.raises(ValueError, match=f"missing: {dropped}$"):
        load_github_app_config(partial)


def test_a_non_rsa_key_is_refused(app_env: dict[str, str], tmp_path: Path) -> None:
    ec_key = tmp_path / "ec.pem"
    ec_key.write_text(_pem(ec.generate_private_key(ec.SECP256R1())))
    with pytest.raises(ValueError, match="must be an RSA private key"):
        load_github_app_config(
            {**app_env, "WHYGRAPH_GITHUB_APP_PRIVATE_KEY_FILE": str(ec_key)}
        )
    junk = tmp_path / "junk.pem"
    junk.write_text("not a key")
    with pytest.raises(ValueError, match="not an unencrypted PEM private key"):
        load_github_app_config(
            {**app_env, "WHYGRAPH_GITHUB_APP_PRIVATE_KEY_FILE": str(junk)}
        )


def test_a_short_webhook_secret_is_refused(
    app_env: dict[str, str], tmp_path: Path
) -> None:
    short = tmp_path / "short"
    short.write_text("x" * 31 + "\n")
    with pytest.raises(ValueError, match="at least 32 characters") as info:
        load_github_app_config(
            {**app_env, "WHYGRAPH_GITHUB_APP_WEBHOOK_SECRET_FILE": str(short)}
        )
    assert "x" * 31 not in str(info.value)
    short.write_text("y" * 32)
    assert load_github_app_config(
        {**app_env, "WHYGRAPH_GITHUB_APP_WEBHOOK_SECRET_FILE": str(short)}
    )


@pytest.mark.parametrize(
    "var, value, match",
    [
        ("WHYGRAPH_GITHUB_APP_SLUG", "Not A Slug", "URL name"),
        ("WHYGRAPH_GITHUB_APP_CLIENT_SECRET_FILE", "/nonexistent", "cannot read"),
        ("WHYGRAPH_GITHUB_APP_PRIVATE_KEY_FILE", "/nonexistent", "cannot read"),
    ],
)
def test_bad_values_are_refused(
    app_env: dict[str, str], var: str, value: str, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        load_github_app_config({**app_env, var: value})


def test_an_empty_secret_file_is_refused(
    app_env: dict[str, str], tmp_path: Path
) -> None:
    empty = tmp_path / "empty"
    empty.write_text("\n \n")
    with pytest.raises(ValueError, match="is empty"):
        load_github_app_config(
            {**app_env, "WHYGRAPH_GITHUB_APP_CLIENT_SECRET_FILE": str(empty)}
        )


def test_production_refuses_to_start_without_the_app(
    production_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in APP_ENV_VARS:
        monkeypatch.delenv(name)
    with pytest.raises(PortalStartupError, match="needs the GitHub App") as info:
        with prod_portal():
            pass
    for name in APP_ENV_VARS:
        assert name in str(info.value)


def test_production_builds_the_app_when_configured(
    production_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    app_env: dict[str, str],
) -> None:
    for name, value in app_env.items():
        monkeypatch.setenv(name, value)
    with prod_portal() as client:
        app = client.app.state.portal.github_app
        assert isinstance(app, GitHubApp)
        assert app.config.client_id == GITHUB_APP_CLIENT_ID


def test_production_refuses_a_partial_app_config(
    production_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    app_env: dict[str, str],
) -> None:
    for name in APP_ENV_VARS:
        monkeypatch.delenv(name)
    monkeypatch.setenv("WHYGRAPH_GITHUB_APP_SLUG", app_env["WHYGRAPH_GITHUB_APP_SLUG"])
    with pytest.raises(PortalStartupError, match="needs the GitHub App") as info:
        with prod_portal():
            pass
    assert "WHYGRAPH_GITHUB_APP_SLUG" not in str(info.value).split("missing:")[1]


# ---------------------------------------------------------------------------
# The client against the fake (MockTransport)
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _config(key: rsa.RSAPrivateKey, web: str = WEB, api: str = API) -> GitHubAppConfig:
    return GitHubAppConfig(
        slug=GITHUB_APP_SLUG,
        client_id=GITHUB_APP_CLIENT_ID,
        client_secret=GITHUB_APP_CLIENT_SECRET,
        private_key=key,
        webhook_secret=GITHUB_WEBHOOK_SECRET,
        web_url=web,
        api_url=api,
    )


@pytest.fixture
def world(github_app_key: rsa.RSAPrivateKey) -> SimpleNamespace:
    """A fake with one installation and three repos, and a client over it."""
    clock = Clock()
    fake = FakeGitHub(
        "oauth-id",
        "oauth-secret",
        f"{PROD_BASE}/auth/github",
        now=clock,
        app_client_id=GITHUB_APP_CLIENT_ID,
        app_client_secret=GITHUB_APP_CLIENT_SECRET,
        app_callback=f"{PROD_BASE}/auth/github-app",
        app_public_key=github_app_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
        app_slug=GITHUB_APP_SLUG,
        webhook_secret=GITHUB_WEBHOOK_SECRET,
    )
    fake.add_installation(7, "acme", account_type="Organization")
    fake.add_repo(2**31 + 1, "acme/api", 7, readers={"ben": {"pull": True}})
    fake.add_repo(2**31 + 2, "acme/web", 7, default_branch="trunk")
    fake.add_repo(2**31 + 3, "acme/docs", 7, readers={"cy": {"pull": True}})
    fake.add_repo(99, "octocat/hello", None, public=True)
    app = GitHubApp(
        _config(github_app_key),
        transport=httpx.MockTransport(fake.handle),
        clock=clock,
    )
    return SimpleNamespace(fake=fake, app=app, clock=clock)


def _b64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_app_jwt_claims_and_signature(
    world: SimpleNamespace, github_app_key: rsa.RSAPrivateKey
) -> None:
    head, body, sig = world.app.app_jwt().split(".")
    assert json.loads(_b64(head)) == {"alg": "RS256", "typ": "JWT"}
    now = int(world.clock.now)
    assert json.loads(_b64(body)) == {
        "iat": now - 60,
        "exp": now + 540,
        "iss": GITHUB_APP_CLIENT_ID,
    }
    github_app_key.public_key().verify(
        _b64(sig), f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )


def test_installation_token_is_scoped_and_cached(world: SimpleNamespace) -> None:
    token = world.app.installation_token(7, 2**31 + 1)
    assert token.token.startswith("ghs_")
    assert token.token not in repr(token)
    assert token.expires_at == pytest.approx(world.clock.now + 3600, abs=1)
    (call,) = world.fake.calls("POST", "/api/v3/app/installations/7/access_tokens")
    assert json.loads(call.content) == {
        "repository_ids": [2**31 + 1],
        "permissions": dict(INSTALLATION_PERMISSIONS),
    }
    issued = world.fake.installation_tokens[token.token]
    assert issued.repo_ids == (2**31 + 1,)
    assert issued.permissions == dict(INSTALLATION_PERMISSIONS)

    # Cached per (installation, repo) ...
    assert world.app.installation_token(7, 2**31 + 1) == token
    world.clock.now += 3600 - 301
    assert world.app.installation_token(7, 2**31 + 1) == token
    assert len(world.fake.calls("POST", "/api/v3/app/installations/")) == 1
    # ... another repo is its own token ...
    other = world.app.installation_token(7, 2**31 + 2)
    assert other.token != token.token
    # ... and within five minutes of expiry a fresh one is minted.
    world.clock.now += 2
    fresh = world.app.installation_token(7, 2**31 + 1)
    assert fresh.token not in (token.token, other.token)
    assert len(world.fake.calls("POST", "/api/v3/app/installations/")) == 3


def test_installation_token_access_lost(world: SimpleNamespace) -> None:
    # 422: a repository outside the installation.
    with pytest.raises(GitHubAccessLost) as lost:
        world.app.installation_token(7, 99)
    assert lost.value.status == 422
    # 404: no such installation (uninstalled).
    with pytest.raises(GitHubAccessLost) as lost:
        world.app.installation_token(8, 2**31 + 1)
    assert lost.value.status == 404
    # 403: suspended.
    world.fake.installations[7].suspended = True
    with pytest.raises(GitHubAccessLost) as lost:
        world.app.installation_token(7, 2**31 + 1)
    assert lost.value.status == 403


def test_a_cached_token_dies_with_access(world: SimpleNamespace) -> None:
    world.app.installation_token(7, 2**31 + 1)
    world.fake.uninstall(7)
    world.clock.now += 3600
    with pytest.raises(GitHubAccessLost):
        world.app.installation_token(7, 2**31 + 1)


@pytest.mark.parametrize(
    "status, exc", [(500, GitHubUnavailable), (401, GitHubUnavailable)]
)
def test_installation_token_other_failures_are_unavailable(
    world: SimpleNamespace, status: int, exc: type[Exception]
) -> None:
    world.fake.force("app_token", status=status)
    with pytest.raises(exc):
        world.app.installation_token(7, 2**31 + 1)
    world.fake.force("app_token", exc=httpx.ConnectError("down"))
    with pytest.raises(GitHubUnavailable):
        world.app.installation_token(7, 2**31 + 1)


def test_a_wrong_key_is_refused_by_github(world: SimpleNamespace) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    app = GitHubApp(_config(other), transport=httpx.MockTransport(world.fake.handle))
    with pytest.raises(GitHubUnavailable, match="401"):
        app.installation_token(7, 2**31 + 1)


def test_the_host_guard(world: SimpleNamespace) -> None:
    with pytest.raises(GitHubHostError):
        world.app._request("GET", "https://api.github.com/user")
    with pytest.raises(GitHubHostError):
        world.app._request("GET", "http://127.0.0.1:10/api/v3/user")
    assert world.fake.requests == []


def test_repository_installation(world: SimpleNamespace) -> None:
    assert world.app.repository_installation("acme/api") == 7
    assert world.app.repository_installation("ACME/API") == 7
    assert world.app.repository_installation("octocat/hello") is None
    assert world.app.repository_installation("nobody/nothing") is None
    with pytest.raises(ValueError):
        world.app.repository_installation("../../user")


def test_installation_repository(world: SimpleNamespace) -> None:
    token = world.app.installation_token(7, 2**31 + 2).token
    repo = world.app.installation_repository(token, 2**31 + 2)
    assert (repo.full_name, repo.default_branch) == ("acme/web", "trunk")
    with pytest.raises(GitHubAccessLost):
        world.app.installation_repository(token, 2**31 + 1)


# -- user authorization -------------------------------------------------------


def _follow(world: SimpleNamespace, url: str) -> dict[str, str]:
    """GET ``url`` against the fake; return the redirect's query."""
    response = world.fake.handle(httpx.Request("GET", url))
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(f"{PROD_BASE}/auth/github-app?")
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


def test_authorize_with_pkce(world: SimpleNamespace) -> None:
    verifier = "v" * 64
    url = world.app.authorize_url("st", pkce_challenge(verifier))
    query = parse_qs(urlsplit(url).query)
    assert url.startswith(f"{WEB}/login/oauth/authorize?")
    assert query == {
        "client_id": [GITHUB_APP_CLIENT_ID],
        "state": ["st"],
        "code_challenge": [_s256(verifier)],
        "code_challenge_method": ["S256"],
    }
    back = _follow(world, url + "&login=ben")
    assert back["state"] == "st" and back["iss"].endswith("/login/oauth")
    # A wrong verifier is refused; the code is single use.
    with pytest.raises(GitHubAuthFailed):
        world.app.exchange(back["code"], "w" * 64)
    back = _follow(world, url + "&login=ben")
    auth = world.app.exchange(back["code"], verifier)
    assert auth.token.startswith("ghu_") and auth.token not in repr(auth)
    assert auth.expires_at == pytest.approx(world.clock.now + 8 * 3600)
    user = world.app.user(auth.token)
    assert (user.login, user.id, user.two_factor) == ("ben", 1001, False)
    with pytest.raises(GitHubAuthFailed):
        world.app.exchange(back["code"], verifier)


def test_install_redirect_exchanges_without_pkce(world: SimpleNamespace) -> None:
    url = world.app.install_url("st2")
    assert url == f"{WEB}/apps/{GITHUB_APP_SLUG}/installations/new?state=st2"
    back = _follow(world, url + "&login=dee")
    assert back["setup_action"] == "install" and back["state"] == "st2"
    assert "iss" not in back
    installation = int(back["installation_id"])
    assert world.fake.installations[installation].account == "dee"
    auth = world.app.exchange(back["code"], None)
    assert world.app.user(auth.token).login == "dee"
    assert [i.id for i in world.app.installations(auth.token)] == [installation]
    # Configuring an existing installation is an update.
    back = _follow(world, url + f"&login=dee&installation_id={installation}")
    assert back["setup_action"] == "update"
    # An org member asking the owners comes back without an installation.
    back = _follow(world, url + "&login=dee&request=1")
    assert back["setup_action"] == "request" and "installation_id" not in back


def _user_token(world: SimpleNamespace, login: str) -> str:
    return world.app.exchange(world.fake.issue_code(login, app=True), None).token


def test_installations_and_repositories_as_a_user(world: SimpleNamespace) -> None:
    ben = _user_token(world, "ben")
    (inst,) = world.app.installations(ben)
    assert (inst.id, inst.account_login, inst.account_type) == (
        7,
        "acme",
        "Organization",
    )
    page = world.app.installation_repos(ben, 7, 1)
    assert [r.full_name for r in page.repos] == ["acme/api"]
    assert page.total_count == 1 and page.repos[0].can_pull
    (call,) = world.fake.calls("GET", "/api/v3/user/installations/7/repositories")
    assert call.url.params["per_page"] == "100" and call.url.params["page"] == "1"

    # Read access: a reader 200, a private non-reader 404, a public repo 200.
    assert world.app.repository(ben, 2**31 + 1).can_pull
    with pytest.raises(GitHubNotFound):
        world.app.repository(ben, 2**31 + 3)
    public = world.app.repository(ben, 99)
    assert public.can_pull and not public.private

    # Someone with no access to the installation sees none of it.
    nofa = _user_token(world, "nofa")
    assert world.app.installations(nofa) == []
    with pytest.raises(GitHubNotFound):
        world.app.installation_repos(nofa, 7, 1)


def test_user_token_rejection_and_revoke(world: SimpleNamespace) -> None:
    token = _user_token(world, "ben")
    assert world.app.revoke(token) is True
    with pytest.raises(GitHubTokenRejected):
        world.app.installations(token)
    with pytest.raises(GitHubTokenRejected):
        world.app.repository(token, 2**31 + 1)
    with pytest.raises(GitHubAuthFailed):
        world.app.user(token)
    # Expiry counts as rejection too.
    token = _user_token(world, "ben")
    world.clock.now += 8 * 3600
    with pytest.raises(GitHubTokenRejected):
        world.app.installations(token)
    # A revoke of an unknown token is a quiet False.
    assert world.app.revoke("ghu_unknown") is False


def test_tokens_never_reach_the_log(
    world: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    minted = world.app.installation_token(7, 2**31 + 1).token
    user = _user_token(world, "ben")
    world.fake.force("installation_repos", status=500)
    with pytest.raises(GitHubUnavailable):
        world.app.installation_repos(user, 7, 1)
    with pytest.raises(GitHubAccessLost):
        world.app.installation_token(7, 99)
    world.app.revoke(user)
    text = caplog.text + "".join(str(r.args) for r in caplog.records)
    assert minted not in text and user not in text
    assert world.app.app_jwt().split(".")[2] not in text


# ---------------------------------------------------------------------------
# UserTokens
# ---------------------------------------------------------------------------


def test_user_tokens_semantics() -> None:
    clock = Clock(1000.0)
    tokens = UserTokens(clock=clock)
    tokens.put(1, token="ghu_a", user_id=10, github_id=1001, expires=1000.0 + 3600)
    entry = tokens.get(1, 10)
    assert entry is not None and entry.token == "ghu_a" and entry.github_id == 1001
    assert "ghu_a" not in repr(entry)
    # Another user, or another session, never gets it.
    assert tokens.get(1, 11) is None
    assert tokens.get(2, 10) is None
    # Replaced by a later put.
    tokens.put(1, token="ghu_b", user_id=10, github_id=1001, expires=1000.0 + 3600)
    assert tokens.get(1, 10).token == "ghu_b"
    # Unusable within the margin of expiry, then swept.
    tokens.put(2, token="ghu_c", user_id=12, github_id=1002, expires=1000.0 + 30)
    assert tokens.get(2, 12) is None
    assert len(tokens) == 2
    clock.now += 31
    assert tokens.sweep() == 1
    assert len(tokens) == 1
    # drop hands the entry back (for a best-effort revoke) and forgets it.
    dropped = tokens.drop(1)
    assert dropped is not None and dropped.token == "ghu_b"
    assert tokens.drop(1) is None
    assert tokens.get(1, 10) is None


# ---------------------------------------------------------------------------
# Git over the fake's dumb HTTP
# ---------------------------------------------------------------------------

_HELPER = '!f() { echo username=x-access-token; echo "password=$WG_TEST_TOKEN"; }; f'


def _git(*args: str, token: str | None, cwd: Path | None = None):  # noqa: ANN202
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.lower().endswith("_proxy") and not k.startswith("GIT_")
    }
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        NO_PROXY="*",
        WG_TEST_TOKEN=token or "",
    )
    return subprocess.run(
        [
            "git",
            "-c",
            "protocol.http.allow=always",
            "-c",
            "credential.helper=",
            "-c",
            f"credential.helper={_HELPER}",
            *args,
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def git_world(
    github_git_server: GitServer,
    github_app_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    """Two repos of one installation on the real git server, and a real client."""
    for var in list(os.environ):
        if var.lower().endswith("_proxy"):
            monkeypatch.delenv(var)
    server = github_git_server
    server.fake.add_installation(7, "acme", account_type="Organization")
    api = server.add_repo(501, "acme/api", 7, files={"README.md": "api\n"})
    server.add_repo(502, "acme/web", 7, files={"README.md": "web\n"})
    app = GitHubApp(_config(github_app_key, server.url, server.url + "/api/v3"))
    return SimpleNamespace(server=server, app=app, api=api)


def test_git_clone_and_ls_remote_with_a_scoped_token(
    git_world: SimpleNamespace, tmp_path: Path
) -> None:
    server, app = git_world.server, git_world.app
    token = app.installation_token(7, 501).token  # minted over real HTTP
    head = _git(
        "--git-dir", str(git_world.api.path), "rev-parse", "HEAD", token=None
    ).stdout.strip()

    listed = _git("ls-remote", server.clone_url("acme/api"), token=token)
    assert listed.returncode == 0, listed.stderr
    assert f"{head}\trefs/heads/main" in listed.stdout

    clone = tmp_path / "clone"
    cloned = _git("clone", "-q", server.clone_url("acme/api"), str(clone), token=token)
    assert cloned.returncode == 0, cloned.stderr
    assert (clone / "README.md").read_text() == "api\n"
    assert token not in cloned.stderr

    # A new commit shows up on the next fetch.
    sha = server.commit("acme/api", message="second")
    fetched = _git("fetch", "-q", "origin", token=token, cwd=clone)
    assert fetched.returncode == 0, fetched.stderr
    assert _git("rev-parse", "origin/main", token=None, cwd=clone).stdout.strip() == sha


def test_git_refuses_another_repo_an_expired_token_and_no_token(
    git_world: SimpleNamespace, tmp_path: Path
) -> None:
    server, app = git_world.server, git_world.app
    token = app.installation_token(7, 501).token
    # Scoping holds: another repository of the same installation is refused.
    other = _git("ls-remote", server.clone_url("acme/web"), token=token)
    assert other.returncode != 0
    assert "Authentication failed" in other.stderr or "not found" in other.stderr
    assert token not in other.stderr
    # No token at all.
    assert _git("ls-remote", server.clone_url("acme/api"), token=None).returncode != 0
    # An expired token.
    server.fake.installation_tokens[token].expires = server.fake.now() - 1
    expired = _git("ls-remote", server.clone_url("acme/api"), token=token)
    assert expired.returncode != 0
    # An unscoped (installation-wide) token reads both - what scoping prevents.
    wide = server.fake.issue_installation_token(7)
    assert _git("ls-remote", server.clone_url("acme/web"), token=wide).returncode == 0


def test_the_portal_git_commands_reach_the_configured_loopback_host(
    git_world: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``WHYGRAPH_GITHUB_URL`` drives git's host: http, port and helper (section 0.2 #7)."""
    server, app = git_world.server, git_world.app
    monkeypatch.setenv(GITHUB_URL_ENV, server.url)
    token = app.installation_token(7, 501).token
    url = server.clone_url("acme/api")
    assert parse_github_url(url) == ("acme", "api")

    dest = tmp_path / "clone"
    Repository.clone(url, dest, env=git_env(token))
    assert (dest / "README.md").read_text() == "api\n"
    assert token not in (dest / ".git" / "config").read_text()
    sha = server.commit("acme/api", message="second")
    Repository(dest).fetch_default(env=git_env(token))
    assert _git("rev-parse", "origin/main", token=None, cwd=dest).stdout.strip() == sha

    # The helper answers that host only: without a token the server refuses.
    with pytest.raises(GitError, match="clone of"):
        Repository.clone(url, tmp_path / "anonymous", env=git_env(None))
    # github.com is not the configured host any more, so its URL is refused.
    with pytest.raises(InvalidRepoUrlError):
        Repository.clone("https://github.com/acme/api", tmp_path / "x")


def test_webhook_delivery_is_signed(github_git_server: GitServer) -> None:
    import hashlib
    import hmac

    headers, body = github_git_server.fake.webhook_delivery(
        "push", {"ref": "refs/heads/main"}
    )
    expected = hmac.new(GITHUB_WEBHOOK_SECRET.encode(), body, hashlib.sha256)
    assert headers["X-Hub-Signature-256"] == "sha256=" + expected.hexdigest()
    assert headers["X-GitHub-Event"] == "push"
    assert json.loads(body) == {"ref": "refs/heads/main"}
    other, _ = github_git_server.fake.webhook_delivery(
        "push", {"ref": "refs/heads/main"}, secret="wrong"
    )
    assert other["X-Hub-Signature-256"] != headers["X-Hub-Signature-256"]
    assert other["X-GitHub-Delivery"] != headers["X-GitHub-Delivery"]
