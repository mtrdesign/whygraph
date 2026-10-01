"""Tests for the portal verbs of the ``whygraph`` shim (``up`` / ``down`` / ...).

The shim is POSIX ``sh``, so the behaviour is tested by running it against a
**fake ``docker``** placed first (and alone) on ``PATH``. The fake keeps a tiny
container store on disk, logs every call, and mimics the two real-docker
behaviours the shim depends on: ``--mount`` errors on a missing source, and
``docker rm`` refuses a running container without ``-f``.

The shim itself comes out of :func:`render_installer` exactly as a user would
get it (installer run into a temp bin dir), so what is tested is what ships.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from whygraph.cli.commands.install import IMAGE_REPO, render_installer

IMAGE = f"{IMAGE_REPO}:1.2.3"

_TOOLS = ("sh", "id", "mkdir", "chmod", "grep", "touch", "rm", "mv", "cat")
"""Externals the portal verbs use; ``PATH`` is only these plus the fake docker."""

_FAKE_DOCKER = r"""#!/bin/sh
S="$FAKE_DOCKER_STATE"
( IFS='|'; echo "$*" >> "$S/argv.log" )
cmd="$1"; shift
case "$cmd" in
  inspect)
    fmt=""; name=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --type) shift 2 ;;
        -f) fmt="$2"; shift 2 ;;
        *) name="$1"; shift ;;
      esac
    done
    [ -d "$S/c/$name" ] || exit 1
    case "$fmt" in
      *State.Status*) cat "$S/c/$name/status" ;;
      *whygraph.folders*) cat "$S/c/$name/folders" ;;
      *whygraph.port*) cat "$S/c/$name/port" ;;
      *whygraph.image*) cat "$S/c/$name/image" ;;
      *whygraph.dev_src*) cat "$S/c/$name/dev_src" 2>/dev/null || true ;;
    esac
    ;;
  run)
    name=""; folders=""; port=""; image=""; dev_src=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --name) name="$2"; shift 2 ;;
        --label)
          case "$2" in
            whygraph.folders=*) folders="${2#whygraph.folders=}" ;;
            whygraph.port=*) port="${2#whygraph.port=}" ;;
            whygraph.image=*) image="${2#whygraph.image=}" ;;
            whygraph.dev_src=*) dev_src="${2#whygraph.dev_src=}" ;;
          esac
          shift 2 ;;
        --mount)
          src="${2#*source=}"; src="${src%%,target=*}"
          [ -d "$src" ] || { echo "invalid mount config: bind source path does not exist: $src" >&2; exit 125; }
          shift 2 ;;
        *) shift ;;
      esac
    done
    [ -n "$name" ] || exit 0
    [ ! -d "$S/c/$name" ] || { echo "name $name already in use" >&2; exit 125; }
    mkdir -p "$S/c/$name"
    printf '%s' "${FAKE_RUN_STATUS:-running}" > "$S/c/$name/status"
    printf '%s' "$folders" > "$S/c/$name/folders"
    printf '%s' "$port" > "$S/c/$name/port"
    printf '%s' "$image" > "$S/c/$name/image"
    printf '%s' "$dev_src" > "$S/c/$name/dev_src"
    echo "fakecontainerid"
    ;;
  stop)
    while [ $# -gt 0 ]; do case "$1" in -t) shift 2 ;; *) name="$1"; shift ;; esac; done
    printf 'exited' > "$S/c/$name/status"
    ;;
  rm)
    force=""; name=""
    while [ $# -gt 0 ]; do case "$1" in -f) force=1; shift ;; *) name="$1"; shift ;; esac; done
    [ -d "$S/c/$name" ] || { [ -n "$force" ] && exit 0; exit 1; }
    if [ -z "$force" ] && [ "$(cat "$S/c/$name/status")" = "running" ]; then
      echo "cannot remove a running container" >&2; exit 1
    fi
    rm -rf "$S/c/$name"
    ;;
  logs) echo "fake logs" ;;
  network) exit 0 ;;
  *) exit 0 ;;
esac
"""


@dataclass
class Shim:
    """A rendered shim plus the hermetic environment to run it in."""

    tmp: Path
    shim: Path
    home: Path
    state: Path
    path_dir: Path

    def env(self, **extra: str) -> dict[str, str]:
        return {
            "PATH": str(self.path_dir),
            "HOME": str(self.home),
            "FAKE_DOCKER_STATE": str(self.state),
            "WHYGRAPH_IMAGE": IMAGE,
            **extra,
        }

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(self.shim), *args],
            env=self.env(**env),
            capture_output=True,
            text=True,
            cwd=self.tmp,
        )

    @property
    def data(self) -> Path:
        return self.home / ".local" / "share" / "whygraph"

    @property
    def conf(self) -> Path:
        return self.home / ".config" / "whygraph"

    def calls(self) -> list[list[str]]:
        log = self.state / "argv.log"
        if not log.exists():
            return []
        return [line.split("|") for line in log.read_text().splitlines()]

    def verbs(self) -> list[str]:
        """The docker subcommand of every call, ``inspect`` calls dropped."""
        return [c[0] for c in self.calls() if c[0] != "inspect"]

    def clear_log(self) -> None:
        (self.state / "argv.log").unlink(missing_ok=True)

    def run_call(self) -> list[str]:
        runs = [c for c in self.calls() if c[0] == "run"]
        assert len(runs) == 1, self.calls()
        return runs[0]

    def make_container(self, name: str, status: str, **labels: str) -> None:
        d = self.state / "c" / name
        d.mkdir(parents=True)
        (d / "status").write_text(status)
        for key in ("folders", "port", "image"):
            (d / key).write_text(labels.get(key, ""))


@pytest.fixture
def shim(tmp_path: Path) -> Shim:
    installer = tmp_path / "installer.sh"
    installer.write_text(render_installer(IMAGE))
    bin_dir = tmp_path / "bin"
    subprocess.run(
        ["sh", str(installer)],
        env={**os.environ, "WHYGRAPH_BIN_DIR": str(bin_dir)},
        capture_output=True,
        check=True,
    )
    path_dir = tmp_path / "path"
    path_dir.mkdir()
    for tool in _TOOLS:
        # WHYGRAPH_TEST_SH=/bin/dash re-runs the whole file under another sh.
        found = os.environ.get("WHYGRAPH_TEST_SH") if tool == "sh" else None
        found = found or shutil.which(tool)
        assert found, f"{tool} must exist on the host PATH"
        (path_dir / tool).symlink_to(found)
    docker = path_dir / "docker"
    docker.write_text(_FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    (state / "c").mkdir(parents=True)
    return Shim(tmp_path, bin_dir / "whygraph", home, state, path_dir)


def _dir(shim: Shim, name: str) -> Path:
    d = shim.tmp / name
    d.mkdir(parents=True, exist_ok=True)
    return d.resolve()


def _portal_block(shim: Shim) -> str:
    text = shim.shim.read_text()
    return text.split("# --- portal verbs", 1)[1].split("# --- end portal verbs", 1)[0]


# --- static shape ----------------------------------------------------------


def test_shim_parses_and_keeps_the_ephemeral_path(shim: Shim) -> None:
    assert subprocess.run(["sh", "-n", str(shim.shim)]).returncode == 0
    text = shim.shim.read_text()
    # Every existing invariant of the ephemeral path is still there.
    assert '-v "$PWD:/workspace" -w /workspace' in text
    assert '"$IMAGE" whygraph "$@"' in text
    assert '--user "$(id -u):$(id -g)" -e HOME=/tmp' in text


@pytest.mark.parametrize("shell", ["dash", "bash", "ksh"])
def test_shim_parses_under_other_posix_shells(shim: Shim, shell: str) -> None:
    found = shutil.which(shell)
    if not found:
        pytest.skip(f"{shell} not installed")
    flags = ["--posix", "-n"] if shell == "bash" else ["-n"]
    assert subprocess.run([found, *flags, str(shim.shim)]).returncode == 0


def test_portal_block_shape(shim: Shim) -> None:
    block = _portal_block(shim)
    # The data dir is created (mode 700) in a step that precedes `docker run`.
    assert block.index('mkdir -p "$DATA"') < block.index("docker run -d")
    assert 'chmod 700 "$DATA"' in block
    for needle in (
        "--mount",
        "--init",
        "--network whygraph-portal",
        "--add-host=host.docker.internal:host-gateway",
        'RESTART="unless-stopped"',
        '--restart "$RESTART"',
        '-p "127.0.0.1:$PORT:$PORT"',
        '--user "$(id -u):$(id -g)" -e HOME=/tmp',
        "--host 0.0.0.0",
        "docker stop -t",
    ):
        assert needle in block, needle
    # `--mount`s are built with `set --` (paths with spaces survive).
    assert 'set -- "$@" --mount' in block
    # No credential variable is passed with `-e`.
    assert not re.search(
        r"-e\s+(GH_TOKEN|GITHUB_TOKEN|ANTHROPIC_API_KEY|OPENAI_API_KEY"
        r"|DEEPSEEK_API_KEY|OPENROUTER_API_KEY)",
        block,
    )


# --- up ----------------------------------------------------------------------


def test_up_fresh_starts_the_portal_container(shim: Shim) -> None:
    result = shim.run("up")
    assert result.returncode == 0, result.stderr
    assert "http://127.0.0.1:8765" in result.stdout
    # Data dir exists and is private (the fake refuses a missing --mount source).
    assert shim.data.is_dir()
    assert stat.S_IMODE(shim.data.stat().st_mode) == 0o700

    call = shim.run_call()
    assert call[:2] == ["run", "-d"]
    assert "--init" in call
    assert call[call.index("--name") + 1] == "whygraph-portal"
    assert call[call.index("--restart") + 1] == "unless-stopped"
    assert call[call.index("--network") + 1] == "whygraph-portal"
    assert "--add-host=host.docker.internal:host-gateway" in call
    assert call[call.index("-p") + 1] == "127.0.0.1:8765:8765"
    assert call[call.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert f"type=bind,source={shim.data.resolve()},target=/data" in call
    for env in (
        "WHYGRAPH_DATA=/data",
        "WHYGRAPH_SHARED_FOLDERS=",
        "WHYGRAPH_MODE=local",
        "WHYGRAPH_PORT=8765",
        "HOME=/tmp",
    ):
        assert env in call, env
    assert call[-7:] == [
        IMAGE,
        "whygraph",
        "portal",
        "--host",
        "0.0.0.0",
        "--port",
        "8765",
    ]
    assert ["network", "create", "whygraph-portal"] in shim.calls()


def test_up_passes_no_host_credentials_into_the_container(shim: Shim) -> None:
    creds = {
        "GH_TOKEN": "gh-secret-1",
        "GITHUB_TOKEN": "gh-secret-2",
        "ANTHROPIC_API_KEY": "sk-secret-3",
        "OPENAI_API_KEY": "sk-secret-4",
        "DEEPSEEK_API_KEY": "sk-secret-5",
        "OPENROUTER_API_KEY": "sk-secret-6",
    }
    result = shim.run("up", **creds)
    assert result.returncode == 0, result.stderr
    everything = (shim.state / "argv.log").read_text() + result.stdout + result.stderr
    for name, value in creds.items():
        assert value not in everything, name
    call = shim.run_call()
    assert not [a for a in call if a in creds]


def test_up_mounts_shared_folders_at_the_same_path(shim: Shim) -> None:
    spaced = _dir(shim, "my projects/repo one")
    plain = _dir(shim, "plain")
    result = shim.run("up", "--add-folder", str(spaced), "--add-folder", str(plain))
    assert result.returncode == 0, result.stderr
    call = shim.run_call()
    assert f"type=bind,source={spaced},target={spaced}" in call
    assert f"type=bind,source={plain},target={plain}" in call
    joined = f"{spaced}:{plain}"
    assert f"WHYGRAPH_SHARED_FOLDERS={joined}" in call
    assert f"whygraph.folders={joined}" in call
    assert (shim.conf / "folders").read_text().splitlines() == [str(spaced), str(plain)]


def test_add_folder_normalizes_symlinks_and_dedupes(shim: Shim) -> None:
    real = _dir(shim, "real")
    link = shim.tmp / "link"
    link.symlink_to(real)
    assert (
        shim.run("up", "--add-folder", str(link), "--add-folder", str(real)).returncode
        == 0
    )
    assert (shim.conf / "folders").read_text().splitlines() == [str(real)]
    shim.run("down")
    assert shim.run("up", "--add-folder", str(link)).returncode == 0
    assert (shim.conf / "folders").read_text().splitlines() == [str(real)]


def test_up_unchanged_does_not_recreate(shim: Shim) -> None:
    folder = _dir(shim, "proj")
    assert shim.run("up", "--add-folder", str(folder)).returncode == 0
    shim.clear_log()
    result = shim.run("up")
    assert result.returncode == 0, result.stderr
    assert "already running" in result.stdout
    assert shim.verbs() == []  # only inspect calls: no stop / rm / run


def test_up_add_folder_recreates_with_stop_before_rm(shim: Shim) -> None:
    assert shim.run("up").returncode == 0
    shim.clear_log()
    folder = _dir(shim, "proj")
    result = shim.run("up", "--add-folder", str(folder))
    assert result.returncode == 0, result.stderr
    assert "folders" in result.stderr
    assert shim.verbs() == ["stop", "rm", "network", "run"]
    stop = next(c for c in shim.calls() if c[0] == "stop")
    assert stop == ["stop", "-t", "30", "whygraph-portal"]
    assert f"whygraph.folders={folder}" in shim.run_call()


def test_stop_grace_is_configurable(shim: Shim) -> None:
    shim.run("up")
    shim.clear_log()
    shim.run("down", WHYGRAPH_STOP_GRACE="5")
    assert ["stop", "-t", "5", "whygraph-portal"] in shim.calls()


def test_up_port_recreates_and_is_remembered(shim: Shim) -> None:
    assert shim.run("up").returncode == 0
    shim.clear_log()
    result = shim.run("up", "--port", "9001")
    assert result.returncode == 0, result.stderr
    assert "port" in result.stderr
    assert shim.verbs() == ["stop", "rm", "network", "run"]
    call = shim.run_call()
    assert call[call.index("-p") + 1] == "127.0.0.1:9001:9001"
    assert "WHYGRAPH_PORT=9001" in call
    assert "whygraph.port=9001" in call
    assert (shim.conf / "port").read_text().strip() == "9001"
    assert call[-2:] == ["--port", "9001"]

    # The persisted port is reused: a plain `up` is a no-op.
    shim.clear_log()
    assert shim.run("up").returncode == 0
    assert shim.verbs() == []


def test_up_uses_exported_port_when_none_is_persisted(shim: Shim) -> None:
    assert shim.run("up", WHYGRAPH_PORT="9100").returncode == 0
    assert "127.0.0.1:9100:9100" in shim.run_call()


@pytest.mark.parametrize("bad", ["abc", "0", "70000", "", "80x"])
def test_up_rejects_an_invalid_port(shim: Shim, bad: str) -> None:
    result = shim.run("up", "--port", bad)
    assert result.returncode == 2
    assert "invalid port" in result.stderr
    assert shim.verbs() == []
    assert not (shim.conf / "port").exists()


def test_up_image_change_recreates(shim: Shim) -> None:
    assert shim.run("up").returncode == 0
    shim.clear_log()
    result = shim.run("up", WHYGRAPH_IMAGE=f"{IMAGE_REPO}:9.9.9")
    assert result.returncode == 0, result.stderr
    assert "image" in result.stderr
    assert shim.verbs() == ["stop", "rm", "network", "run"]
    assert f"whygraph.image={IMAGE_REPO}:9.9.9" in shim.run_call()


@pytest.mark.parametrize("status", ["exited", "created", "dead"])
def test_up_removes_a_non_running_container_first(shim: Shim, status: str) -> None:
    shim.make_container("whygraph-portal", status, port="8765", image=IMAGE)
    result = shim.run("up")
    assert result.returncode == 0, result.stderr
    assert [c for c in shim.calls() if c[0] == "rm"] == [
        ["rm", "-f", "whygraph-portal"]
    ]
    assert "stop" not in shim.verbs()  # nothing to stop
    assert shim.verbs()[-1] == "run"


def test_up_leaves_a_restarting_container_alone_and_says_so(shim: Shim) -> None:
    shim.make_container("whygraph-portal", "restarting", port="8765", image=IMAGE)
    result = shim.run("up")
    assert result.returncode == 0
    assert "restarting" in result.stderr
    assert shim.verbs() == []


def test_up_warns_about_a_leftover_serve_container(shim: Shim) -> None:
    shim.make_container("whygraph-serve", "running")
    result = shim.run("up")
    assert result.returncode == 0, result.stderr
    assert "whygraph-serve" in result.stderr
    assert "whygraph serve --stop" in result.stderr
    # The warning is advisory: the portal still starts.
    assert shim.verbs()[-1] == "run"


def test_up_without_a_serve_container_is_quiet(shim: Shim) -> None:
    assert "whygraph-serve" not in shim.run("up").stderr


def test_up_rejects_unknown_args(shim: Shim) -> None:
    result = shim.run("up", "--bogus")
    assert result.returncode == 2
    assert shim.verbs() == []


# --- --add-folder validation -------------------------------------------------


@pytest.mark.parametrize(
    ("dirname", "why"),
    [
        ("has:colon", ":"),
        ("has\nnewline", "newline"),
        ("has,comma", ","),
        ('has"quote', "double quotes"),
    ],
)
def test_add_folder_rejects_separator_characters(
    shim: Shim, dirname: str, why: str
) -> None:
    bad = _dir(shim, dirname)
    result = shim.run("up", "--add-folder", str(bad))
    assert result.returncode == 2
    assert "not allowed" in result.stderr
    assert shim.verbs() == []
    assert (shim.conf / "folders").read_text() == ""


def test_add_folder_rejects_root(shim: Shim) -> None:
    result = shim.run("up", "--add-folder", "/")
    assert result.returncode == 2
    assert "'/'" in result.stderr
    assert shim.verbs() == []


def test_add_folder_rejects_a_missing_directory(shim: Shim) -> None:
    result = shim.run("up", "--add-folder", str(shim.tmp / "nope"))
    assert result.returncode == 2
    assert "not a directory" in result.stderr
    assert shim.verbs() == []


def test_add_folder_rejects_data_dir_overlap(shim: Shim) -> None:
    shim.data.mkdir(parents=True)
    inside = shim.data / "sub"
    inside.mkdir()
    for folder in (shim.data, inside, shim.home / ".local", shim.home):
        result = shim.run("up", "--add-folder", str(folder))
        assert result.returncode == 2, folder
        assert "overlaps the data directory" in result.stderr, folder
    assert shim.verbs() == []
    assert (shim.conf / "folders").read_text() == ""


def test_add_folder_overlap_follows_a_custom_data_dir(shim: Shim) -> None:
    data = _dir(shim, "elsewhere/data")
    result = shim.run("up", "--add-folder", str(data.parent), WHYGRAPH_DATA=str(data))
    assert result.returncode == 2
    assert "overlaps the data directory" in result.stderr
    ok = _dir(shim, "elsewhere2")
    assert (
        shim.run("up", "--add-folder", str(ok), WHYGRAPH_DATA=str(data)).returncode == 0
    )
    assert f"type=bind,source={data},target=/data" in shim.run_call()


def test_one_bad_folder_persists_nothing(shim: Shim) -> None:
    good = _dir(shim, "good")
    result = shim.run("up", "--add-folder", str(good), "--add-folder", "/")
    assert result.returncode == 2
    assert (shim.conf / "folders").read_text() == ""


def test_up_refuses_a_vanished_shared_folder(shim: Shim) -> None:
    gone = _dir(shim, "gone")
    assert shim.run("up", "--add-folder", str(gone)).returncode == 0
    shutil.rmtree(gone)
    shim.clear_log()
    result = shim.run("up")
    assert result.returncode == 2
    assert "no longer exists" in result.stderr
    assert "whygraph folders --remove" in result.stderr
    assert shim.verbs() == []


def test_sharing_home_warns(shim: Shim) -> None:
    result = shim.run("up", "--add-folder", str(shim.home))
    # $HOME contains the data dir here, so it is refused before the warning
    # matters; a folder that merely equals a HOME with a custom data dir warns.
    assert result.returncode == 2
    data = _dir(shim, "elsewhere/data")
    result = shim.run("up", "--add-folder", str(shim.home), WHYGRAPH_DATA=str(data))
    assert result.returncode == 0, result.stderr
    assert "home directory" in result.stderr


# --- folders -----------------------------------------------------------------


def test_folders_lists_and_removes(shim: Shim) -> None:
    a, b = _dir(shim, "a"), _dir(shim, "b")
    shim.run("up", "--add-folder", str(a), "--add-folder", str(b))
    listed = shim.run("folders")
    assert listed.returncode == 0
    assert listed.stdout.splitlines() == [str(a), str(b)]

    shim.clear_log()
    removed = shim.run("folders", "--remove", str(a))
    assert removed.returncode == 0, removed.stderr
    assert (shim.conf / "folders").read_text().splitlines() == [str(b)]
    # The running portal is recreated (stop -t before rm) without the folder.
    assert shim.verbs() == ["stop", "rm", "network", "run"]
    call = shim.run_call()
    assert f"type=bind,source={a},target={a}" not in call
    assert f"whygraph.folders={b}" in call
    assert shim.run("folders").stdout.splitlines() == [str(b)]


def test_folders_remove_works_for_a_deleted_directory(shim: Shim) -> None:
    gone = _dir(shim, "gone")
    shim.run("up", "--add-folder", str(gone))
    shim.run("down")
    shutil.rmtree(gone)
    result = shim.run("folders", "--remove", str(gone))
    assert result.returncode == 0, result.stderr
    assert (shim.conf / "folders").read_text() == ""


def test_folders_remove_without_a_portal_does_not_start_one(shim: Shim) -> None:
    a = _dir(shim, "a")
    shim.run("up", "--add-folder", str(a))
    shim.run("down")
    shim.clear_log()
    assert shim.run("folders", "--remove", str(a)).returncode == 0
    assert shim.verbs() == []


def test_folders_remove_unknown_path_fails(shim: Shim) -> None:
    a = _dir(shim, "a")
    shim.run("up", "--add-folder", str(a))
    shim.clear_log()
    result = shim.run("folders", "--remove", str(shim.tmp / "other"))
    assert result.returncode == 1
    assert "not a shared folder" in result.stderr
    assert (shim.conf / "folders").read_text().splitlines() == [str(a)]
    assert shim.verbs() == []


def test_folders_empty_prints_a_hint_on_stderr(shim: Shim) -> None:
    result = shim.run("folders")
    assert result.returncode == 0
    assert result.stdout == ""
    assert "--add-folder" in result.stderr


# --- down / status / logs ----------------------------------------------------


def test_down_stops_with_grace_then_removes(shim: Shim) -> None:
    shim.run("up")
    shim.clear_log()
    result = shim.run("down")
    assert result.returncode == 0, result.stderr
    assert shim.verbs() == ["stop", "rm"]
    assert ["stop", "-t", "30", "whygraph-portal"] in shim.calls()
    assert not (shim.state / "c" / "whygraph-portal").exists()


def test_down_when_nothing_runs_is_a_quiet_success(shim: Shim) -> None:
    result = shim.run("down")
    assert result.returncode == 0
    assert "not running" in result.stderr
    assert shim.verbs() == []


def test_status_running_shows_url_folders_and_image(shim: Shim) -> None:
    folder = _dir(shim, "proj")
    other = _dir(shim, "other")
    shim.run(
        "up", "--port", "9001", "--add-folder", str(folder), "--add-folder", str(other)
    )
    result = shim.run("status")
    assert result.returncode == 0
    assert "whygraph portal: running" in result.stdout
    assert "url: http://127.0.0.1:9001" in result.stdout
    assert f"image: {IMAGE}" in result.stdout
    assert f"  {folder}" in result.stdout
    assert f"  {other}" in result.stdout


def test_status_running_without_folders(shim: Shim) -> None:
    shim.run("up")
    assert "folders: (none)" in shim.run("status").stdout


def test_status_reports_a_crash_looping_container_as_restarting(shim: Shim) -> None:
    shim.make_container("whygraph-portal", "restarting", port="8765", image=IMAGE)
    result = shim.run("status")
    assert result.returncode == 1
    assert "whygraph portal: restarting" in result.stdout


def test_status_stopped_and_missing(shim: Shim) -> None:
    result = shim.run("status")
    assert result.returncode == 1
    assert "not created" in result.stdout
    shim.make_container("whygraph-portal", "exited", port="8765", image=IMAGE)
    result = shim.run("status")
    assert result.returncode == 1
    assert "whygraph portal: exited" in result.stdout


def test_logs_follows_the_portal_container(shim: Shim) -> None:
    result = shim.run("logs")
    assert result.returncode == 0
    assert "fake logs" in result.stdout
    assert ["logs", "-f", "whygraph-portal"] in shim.calls()


@pytest.mark.parametrize("verb", ["down", "status", "logs"])
def test_verbs_reject_stray_arguments(shim: Shim, verb: str) -> None:
    assert shim.run(verb, "extra").returncode == 2
    assert shim.verbs() == []


# --- env-credentials hint ----------------------------------------------------


def test_env_hint_is_printed_once_with_names_only(shim: Shim) -> None:
    result = shim.run("up", ANTHROPIC_API_KEY="sk-super-secret", GH_TOKEN="ghp_secret")
    assert result.returncode == 0, result.stderr
    assert "ANTHROPIC_API_KEY" in result.stderr
    assert "GH_TOKEN" in result.stderr
    assert "do not reach the portal" in result.stderr
    assert "Settings in the portal" in result.stderr
    assert "sk-super-secret" not in result.stderr + result.stdout
    assert "ghp_secret" not in result.stderr + result.stdout
    assert (shim.conf / "env-hint-shown").exists()

    # A second `up` (here a real recreate, so the whole path runs) stays silent.
    again = shim.run("up", "--port", "9001", ANTHROPIC_API_KEY="sk-super-secret")
    assert again.returncode == 0, again.stderr
    assert "do not reach the portal" not in again.stderr
    # ...and so does an unchanged one.
    same = shim.run("up", ANTHROPIC_API_KEY="sk-super-secret")
    assert "do not reach the portal" not in same.stderr


def test_env_hint_absent_without_credentials(shim: Shim) -> None:
    result = shim.run("up")
    assert "do not reach the portal" not in result.stderr
    assert not (shim.conf / "env-hint-shown").exists()
    # The sentinel is only written when the hint was actually shown.
    later = shim.run("up", "--port", "9001", OPENAI_API_KEY="sk-x")
    assert "OPENAI_API_KEY" in later.stderr


def test_env_hint_also_shows_when_the_portal_is_already_running(shim: Shim) -> None:
    shim.run("up")
    result = shim.run("up", DEEPSEEK_API_KEY="sk-x")
    assert "DEEPSEEK_API_KEY" in result.stderr


# --- the rest of the shim is untouched ---------------------------------------


def test_other_commands_still_run_ephemerally(shim: Shim) -> None:
    result = shim.run("scan", "--help")
    assert result.returncode == 0
    call = shim.run_call()
    assert call[:3] == ["run", "--rm", "-i"]
    assert call[-4:] == [IMAGE, "whygraph", "scan", "--help"]
    assert f"{shim.tmp.resolve()}:/workspace" in call
    # The ephemeral path keeps its credential passthrough.
    assert "GH_TOKEN" in call


def test_serve_branch_still_present(shim: Shim) -> None:
    assert shim.run("serve", "--stop").returncode == 0
    assert ["rm", "-f", "whygraph-serve"] in shim.calls()


# --- the post-merge verify-image workflow step -------------------------------

WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "cd-deploy-whygraph.yml"
)


def test_verify_image_probes_the_portal_state_endpoint() -> None:
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["verify-image"]["steps"]
    probe = [s for s in steps if "portal" in s.get("name", "").lower()]
    assert len(probe) == 1, [s.get("name") for s in steps]
    script = probe[0]["run"]
    assert "whygraph portal --host 0.0.0.0" in script
    assert "/api/portal/state" in script
    assert "X-WhyGraph-Client: 1" in script
    assert "Host: 127.0.0.1" in script
    # Same runtime shape as the shim: host user, loopback publish only.
    assert '--user "$(id -u):$(id -g)"' in script
    assert '"127.0.0.1:${port}:${port}"' in script
    # It cleans up after itself.
    assert "docker rm -f portal-smoke" in script


# --- development knobs (make dev-docker / prod / smoke) ---------------------


def _checkout(shim: Shim) -> Path:
    """A minimal fake WhyGraph checkout for WHYGRAPH_DEV_SRC."""
    root = _dir(shim, "checkout")
    (root / "src" / "whygraph").mkdir(parents=True, exist_ok=True)
    (root / "src" / "whygraph" / "__init__.py").write_text("")
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts" / "dev_portal.py").write_text("")
    return root


def test_default_up_has_no_dev_mode(shim: Shim) -> None:
    assert shim.run("up").returncode == 0
    call = shim.run_call()
    joined = " ".join(call)
    assert "PYTHONPATH" not in joined
    assert "/opt/whygraph-dev" not in joined
    assert "127.0.0.1:5173:5173" not in call
    assert "WHYGRAPH_SCAN_CMD" not in joined
    assert not any(c.startswith("whygraph.dev_src=") for c in call)


def test_portal_name_knob_renames_the_container(shim: Shim) -> None:
    result = shim.run("up", WHYGRAPH_PORTAL_NAME="whygraph-portal-dev")
    assert result.returncode == 0, result.stderr
    call = shim.run_call()
    assert call[call.index("--name") + 1] == "whygraph-portal-dev"
    # The user's own portal is left alone by every verb.
    assert not (shim.state / "c" / "whygraph-portal").exists()
    shim.run("down", WHYGRAPH_PORTAL_NAME="whygraph-portal-dev")
    assert not (shim.state / "c" / "whygraph-portal-dev").exists()


@pytest.mark.parametrize("bad", ["-x", "a b", "a/b", "a;b", ".hidden"])
def test_portal_name_knob_rejects_bad_names(shim: Shim, bad: str) -> None:
    result = shim.run("up", WHYGRAPH_PORTAL_NAME=bad)
    assert result.returncode == 2
    assert "invalid WHYGRAPH_PORTAL_NAME" in result.stderr
    assert "run" not in shim.verbs()


def test_dev_src_runs_the_checkout_through_the_dev_wrapper(shim: Shim) -> None:
    root = _checkout(shim)
    result = shim.run("up", WHYGRAPH_DEV_SRC=str(root))
    assert result.returncode == 0, result.stderr
    call = shim.run_call()
    assert f"type=bind,source={root},target=/opt/whygraph-dev,readonly" in call
    modules = shim.data.resolve() / "dev-node_modules"
    assert modules.is_dir()
    assert (
        f"type=bind,source={modules},target=/opt/whygraph-dev/src/playground/node_modules"
        in call
    )
    assert "127.0.0.1:5173:5173" in call
    assert "PYTHONPATH=/opt/whygraph-dev/src" in call
    # Scan children get the checkout too, without widening the env allowlist.
    assert (
        "WHYGRAPH_SCAN_CMD=env PYTHONPATH=/opt/whygraph-dev/src python -m whygraph scan"
        in call
    )
    assert "WHYGRAPH_DEV_ORIGINS=http://localhost:5173,http://127.0.0.1:5173" in call
    assert f"whygraph.dev_src={root}" in call
    assert call[call.index("--restart") + 1] == "no"
    assert call[-10:] == [
        IMAGE,
        "python",
        "/opt/whygraph-dev/scripts/dev_portal.py",
        "--vite-host",
        "0.0.0.0",
        "--",
        "--host",
        "0.0.0.0",
        "--port",
        "8765",
    ]


def test_toggling_dev_src_recreates_the_container(shim: Shim) -> None:
    root = _checkout(shim)
    assert shim.run("up", WHYGRAPH_DEV_SRC=str(root)).returncode == 0
    shim.clear_log()
    unchanged = shim.run("up", WHYGRAPH_DEV_SRC=str(root))
    assert "already running" in unchanged.stdout
    assert "run" not in shim.verbs()
    back = shim.run("up")
    assert back.returncode == 0, back.stderr
    assert "changed: dev_src" in back.stderr
    assert shim.verbs()[:2] == ["stop", "rm"]


@pytest.mark.parametrize("kind", ["relative", "not-a-checkout", "comma"])
def test_dev_src_is_validated(shim: Shim, kind: str) -> None:
    if kind == "relative":
        value = "checkout"
        _checkout(shim)
    elif kind == "not-a-checkout":
        value = str(_dir(shim, "empty"))
    else:
        root = _checkout(shim)
        bad = shim.tmp / "a,b"
        root.rename(bad)
        value = str(bad)
    result = shim.run("up", WHYGRAPH_DEV_SRC=value)
    assert result.returncode == 2, result.stdout
    assert "WHYGRAPH_DEV_SRC" in result.stderr
    assert "run" not in shim.verbs()
