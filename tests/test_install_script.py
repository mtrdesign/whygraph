"""Tests for ``scripts/install.sh`` — the host-side install bootstrapper.

The script is what ``curl -fsSL …/v<tag>/scripts/install.sh | sh`` runs: it
probes Docker, pulls the pinned image, then delegates shim generation to the
image's own ``whygraph install`` (see
:mod:`whygraph.cli.commands.install`). These tests run it offline by putting a
**stub ``docker``** first on ``PATH``, mirroring the isolated-filesystem
approach in ``tests/test_install_cmd.py``.

Two properties matter most:

* The script carries **no shim bodies** — :func:`render_installer` stays the
  single source of truth (:func:`test_carries_no_shim_bodies`).
* **Every** failure path exits non-zero. The command this replaced exited 0
  when it installed nothing, because ``docker run``'s error went to stderr and
  ``sh`` read an empty stdin (:func:`test_empty_generator_output_fails_loudly`).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from whygraph.cli.commands.install import IMAGE_REPO, render_installer

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "install.sh"

_STOP_AFTER_PULL = """
case "$1" in
    pull) exit 1 ;;
    image) exit 1 ;;   # not local either, so the pull failure is fatal
esac
exit 0
"""
"""Stub body that halts the script right after the pull.

Both ``pull`` and ``image inspect`` must fail: a pull failure alone is
recoverable from a local image, so stubbing only ``pull`` would let the run
continue.
"""


def _default_version() -> str:
    """The ``DEFAULT_VERSION`` baked into the script — the front-door pin."""
    match = re.search(r'^DEFAULT_VERSION="([^"]+)"', SCRIPT.read_text(), re.M)
    assert match, "scripts/install.sh must define DEFAULT_VERSION"
    return match.group(1)


_NEEDED_TOOLS = ("sh", "mktemp", "rm", "mkdir", "cat", "chmod")
"""Externals the script (and the installer it generates) shell out to.

These are symlinked into the stub directory so ``PATH`` can be **only** that
directory — see :func:`_build_path_dir`.
"""


def _build_path_dir(tmp_path: Path, docker_body: str | None) -> tuple[Path, Path]:
    """Build a hermetic ``PATH`` directory; return it and the docker argv log.

    Holds symlinks to :data:`_NEEDED_TOOLS` plus — when ``docker_body`` is not
    ``None`` — a stub ``docker`` that appends its argv to the returned log
    before running ``docker_body`` (POSIX ``sh``, with ``$1`` as the docker
    subcommand), so callers can assert on the image reference the script chose.

    This directory is the **entire** ``PATH`` for the run. Prepending a stub to
    the real ``PATH`` is not enough: the no-docker case has to *delete* the
    stub, and then a real ``/usr/bin/docker`` (present on CI runners, absent on
    a macOS Docker Desktop host) gets found instead — the test passes locally
    and hits the live registry on CI.
    """
    path_dir = tmp_path / "path"
    path_dir.mkdir(parents=True, exist_ok=True)
    for tool in _NEEDED_TOOLS:
        found = shutil.which(tool)
        assert found, f"{tool} must exist on the host PATH to run these tests"
        link = path_dir / tool
        if not link.exists():  # a test may call _run twice on one tmp_path
            link.symlink_to(found)

    log = path_dir / "argv.log"
    if docker_body is not None:
        stub = path_dir / "docker"
        stub.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n{docker_body}\n')
        stub.chmod(0o755)
    return path_dir, log


def _run(
    tmp_path: Path,
    *args: str,
    docker_body: str = "exit 0",
    env: dict[str, str] | None = None,
    with_docker: bool = True,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """Run the script hermetically; return (result, docker argv log, bin dir)."""
    path_dir, log = _build_path_dir(tmp_path, docker_body if with_docker else None)

    bin_dir = tmp_path / "bin"
    full_env = {
        "PATH": str(path_dir),
        "HOME": str(tmp_path / "home"),
        "WHYGRAPH_BIN_DIR": str(bin_dir),
        **(env or {}),
    }
    result = subprocess.run(
        ["sh", str(SCRIPT), *args],
        env=full_env,
        capture_output=True,
        text=True,
    )
    return result, log, bin_dir


# --- 1-2: shape ------------------------------------------------------------


def test_script_parses() -> None:
    assert subprocess.run(["sh", "-n", str(SCRIPT)]).returncode == 0


def test_is_truncation_safe() -> None:
    # Every statement lives in a function and `main "$@"` is last, so a
    # truncated download defines functions and never executes anything.
    lines = [
        line
        for line in SCRIPT.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert lines[-1] == 'main "$@"'


def test_carries_no_shim_bodies() -> None:
    # The regression guard for the one invariant this script must not break:
    # shim generation stays in `render_installer`, never duplicated in shell.
    text = SCRIPT.read_text()
    assert 'cat > "$BIN_DIR' not in text
    assert "WHYGRAPH_IMAGE:-" not in text


# --- 3: version resolution -------------------------------------------------


@pytest.mark.parametrize(
    ("args", "env", "expected"),
    [
        ((), {}, None),  # None → DEFAULT_VERSION, resolved in the test body
        (("1.1.0",), {}, "1.1.0"),
        ((), {"WHYGRAPH_VERSION": "2.0.0"}, "2.0.0"),
        (("1.1.0",), {"WHYGRAPH_VERSION": "2.0.0"}, "2.0.0"),  # env wins
    ],
)
def test_version_precedence(
    tmp_path: Path, args: tuple[str, ...], env: dict[str, str], expected: str | None
) -> None:
    # Halt right after the pull so the run stops early but the argv log is
    # written — the pulled ref is what encodes the resolved version.
    result, log, _ = _run(tmp_path, *args, docker_body=_STOP_AFTER_PULL, env=env)
    assert result.returncode != 0  # the deliberate pull failure
    want = expected or _default_version()
    assert f"pull {IMAGE_REPO}:{want}" in log.read_text()


def test_image_repo_override(tmp_path: Path) -> None:
    result, log, _ = _run(
        tmp_path,
        "test",
        docker_body=_STOP_AFTER_PULL,
        env={"WHYGRAPH_IMAGE_REPO": "registry.internal/wg"},
    )
    assert result.returncode != 0
    assert "pull registry.internal/wg:test" in log.read_text()


# --- 4: end to end with the real generator ---------------------------------


def test_installs_both_shims_via_the_real_generator(tmp_path: Path) -> None:
    # The stub's `docker run … whygraph install` emits exactly what the real
    # image would — the output of render_installer — so this exercises the
    # delegation contract rather than a hand-written approximation.
    generated = tmp_path / "generated.sh"
    generated.write_text(render_installer(f"{IMAGE_REPO}:1.2.3"))
    body = f"""
case "$1" in
    run) cat "{generated}" ;;
    image) echo "WHYGRAPH_VERSION=9.9.9" ;;
esac
exit 0
"""
    result, _, bin_dir = _run(tmp_path, "1.2.3", docker_body=body)
    assert result.returncode == 0, result.stderr

    # The resolved version comes from the image env, not the requested tag.
    assert "Installing WhyGraph 9.9.9" in result.stderr

    for name in ("whygraph", "whygraph-mcp"):
        shim = bin_dir / name
        assert shim.exists(), name
        assert os.access(shim, os.X_OK), name
        assert subprocess.run(["sh", "-n", str(shim)]).returncode == 0, name
        assert f"{IMAGE_REPO}:1.2.3" in shim.read_text()


def test_warns_about_the_git_hook_path_when_bin_dir_is_reachable(
    tmp_path: Path,
) -> None:
    # The host-only diagnostic: git hooks exit quietly when `whygraph` isn't on
    # the PATH of whatever ran git, which GUI clients routinely aren't. It fires
    # only when bin_dir IS on the interactive PATH — otherwise the generated
    # installer's own "add it to your PATH" warning is the bigger problem.
    generated = tmp_path / "generated.sh"
    generated.write_text(render_installer(f"{IMAGE_REPO}:1.2.3"))
    body = f'[ "$1" = run ] && cat "{generated}"; exit 0'
    bin_dir = tmp_path / "bin"

    on_path, _, _ = _run(
        tmp_path,
        "1.2.3",
        docker_body=body,
        env={"PATH": f"{tmp_path / 'path'}:{bin_dir}"},
    )
    assert on_path.returncode == 0, on_path.stderr
    assert "git hooks launched by GUI clients" in on_path.stderr
    assert "ln -sf" in on_path.stderr

    off_path, _, _ = _run(tmp_path, "1.2.3", docker_body=body)
    assert off_path.returncode == 0, off_path.stderr
    assert "git hooks launched by GUI clients" not in off_path.stderr


def test_falls_back_to_the_requested_version_when_unresolvable(
    tmp_path: Path,
) -> None:
    # `docker image inspect` finding nothing is cosmetic — it must never fail
    # the install, and the requested version is echoed instead.
    generated = tmp_path / "generated.sh"
    generated.write_text(render_installer(f"{IMAGE_REPO}:1.2.3"))
    body = f'[ "$1" = run ] && cat "{generated}"; exit 0'
    result, _, bin_dir = _run(tmp_path, "1.2.3", docker_body=body)
    assert result.returncode == 0, result.stderr
    assert "Installing WhyGraph 1.2.3" in result.stderr
    assert (bin_dir / "whygraph").exists()


# --- 5-8: failure paths all exit non-zero ----------------------------------


def test_empty_generator_output_fails_loudly(tmp_path: Path) -> None:
    # The defect this whole change exists to fix: the old front door piped an
    # empty stdout into `sh`, which exits 0 — a silent no-op that looked like
    # a successful install.
    result, _, bin_dir = _run(tmp_path, docker_body="exit 0")
    assert result.returncode != 0
    assert "empty installer" in result.stderr
    assert "uv tool install whygraph==" in result.stderr  # the escape hatch
    assert not bin_dir.exists()


def test_failed_generator_run_fails_loudly(tmp_path: Path) -> None:
    result, _, _ = _run(tmp_path, docker_body='[ "$1" = run ] && exit 125; exit 0')
    assert result.returncode != 0
    assert "could not emit the installer" in result.stderr


def test_missing_docker_fails_loudly(tmp_path: Path) -> None:
    result, _, _ = _run(tmp_path, with_docker=False)
    assert result.returncode != 0
    assert "docker not found on PATH" in result.stderr


def test_unreachable_daemon_fails_loudly(tmp_path: Path) -> None:
    result, _, _ = _run(tmp_path, docker_body='[ "$1" = info ] && exit 1; exit 0')
    assert result.returncode != 0
    assert "daemon is not reachable" in result.stderr


def test_bad_tag_fails_loudly(tmp_path: Path) -> None:
    # Unpullable *and* not present locally — the only combination that is fatal.
    result, _, _ = _run(tmp_path, "9.9.9", docker_body=_STOP_AFTER_PULL)
    assert result.returncode != 0
    assert "could not pull" in result.stderr
    assert "releases" in result.stderr


def test_unpullable_but_local_image_still_installs(tmp_path: Path) -> None:
    # A locally built or `docker load`ed image has no registry to pull from, so
    # a pull failure alone must not be fatal — air-gapped hosts and this repo's
    # own integration checks depend on it.
    generated = tmp_path / "generated.sh"
    generated.write_text(render_installer("wg:test"))
    body = f"""
case "$1" in
    pull) exit 1 ;;
    run) cat "{generated}" ;;
esac
exit 0
"""
    result, _, bin_dir = _run(
        tmp_path, "test", docker_body=body, env={"WHYGRAPH_IMAGE_REPO": "wg"}
    )
    assert result.returncode == 0, result.stderr
    assert (bin_dir / "whygraph").exists()


# --- 9: the pin is internally consistent ----------------------------------


def test_readme_install_url_matches_the_default_version() -> None:
    # The front-door URL carries the version, so DEFAULT_VERSION and the tag
    # in the advertised URL must agree. The release workflow enforces both
    # against the release tag; this catches the internal mismatch on every PR.
    readme = (REPO_ROOT / "README.md").read_text()
    tags = set(re.findall(r"whygraph/v([^/]+)/scripts/install\.sh", readme))
    assert tags, "README.md must advertise a tag-pinned install URL"
    assert tags == {_default_version()}, (
        f"README.md install URL tag(s) {sorted(tags)} != "
        f"DEFAULT_VERSION {_default_version()}"
    )


def test_package_version_matches_the_default_version() -> None:
    # `whygraph version` reports pyproject's version via importlib.metadata, so
    # a stale one makes the installed tool misreport itself — it sat at 0.1.0
    # through the v1.0.0 release because nothing checked it. Now something does.
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        packaged = tomllib.load(fh)["project"]["version"]
    assert packaged == _default_version(), (
        f"pyproject.toml version {packaged} != "
        f"DEFAULT_VERSION {_default_version()} in scripts/install.sh"
    )


# Pinned forms the docs advertise. Each capture group is a version that must
# equal DEFAULT_VERSION — a reader copies these verbatim, so a stale one hands
# them the wrong release.
_DOC_PIN_PATTERNS = (
    r"whygraph/v([^/\s]+)/scripts/install\.sh",  # the curl front door
    rf"{re.escape(IMAGE_REPO)}:(\d[^\s`\\|]*)",  # docker run … <repo>:<ver>
    r"whygraph\.git@v([^\s\"`]+)",  # uv tool install from a tag
)

# Pages that are *only* about installing, where every semver literal in the
# prose refers to the release being advertised. Scanning bare versions here
# catches sentences like "`v1.1.1` installs 1.1.1", which carry no URL.
_INSTALL_ONLY_PAGES = (
    "docs/getting-started/installation.md",
    "docs/deploy/docker.md",
)


def _docs_pages() -> list[Path]:
    return sorted((REPO_ROOT / "docs").rglob("*.md"))


def test_docs_pinned_urls_match_the_default_version() -> None:
    # The docs site carries the same install commands as the README, and until
    # now nothing checked them — nine literals that would silently point at the
    # previous release the moment a version was cut.
    stale: list[str] = []
    for page in _docs_pages():
        text = page.read_text()
        for pattern in _DOC_PIN_PATTERNS:
            for found in re.findall(pattern, text):
                if found != _default_version():
                    rel = page.relative_to(REPO_ROOT)
                    stale.append(f"{rel}: {found}")
    assert not stale, (
        f"docs pin version(s) != DEFAULT_VERSION {_default_version()}: {stale}"
    )


def test_install_pages_have_no_stale_bare_versions() -> None:
    # Prose on the install pages names the version outside any URL. `sh -s
    # latest` is unaffected — this only looks at semver-shaped literals.
    stale: list[str] = []
    for relative in _INSTALL_ONLY_PAGES:
        page = REPO_ROOT / relative
        assert page.exists(), f"{relative} is gated for versions but does not exist"
        for found in re.findall(r"\b\d+\.\d+\.\d+\b", page.read_text()):
            if found != _default_version():
                stale.append(f"{relative}: {found}")
    assert not stale, (
        f"install-page version(s) != DEFAULT_VERSION {_default_version()}: {stale}"
    )
