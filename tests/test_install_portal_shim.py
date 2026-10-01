"""Tests for the portal verbs of the ``whygraph`` shim (``up`` / ``down`` / ...).

The shim is POSIX ``sh``, so the behaviour is tested by running it against a
**fake ``docker``** placed first (and alone) on ``PATH``. The fake keeps a tiny
container store on disk, logs every call, and mimics the real-docker
behaviours the shim depends on: ``--mount`` errors on a missing source,
``docker rm`` refuses a running container without ``-f``, a health status per
container (a file of states, one popped per poll), and ``docker exec`` printing
a fixed payload (or failing) so ``backup`` is testable.

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

from whygraph.cli.commands.install import (
    IMAGE_REPO,
    POSTGRES_IMAGE,
    POSTGRES_MAJOR,
    render_installer,
)

IMAGE = f"{IMAGE_REPO}:1.2.3"
PORTAL = "whygraph-portal"
PG = "whygraph-portal-postgres"
PG_IMAGE = POSTGRES_IMAGE
DUMP_PAYLOAD = "PGDMP-fake-dump"

_TOOLS = (
    "sh",
    "id",
    "mkdir",
    "chmod",
    "grep",
    "touch",
    "rm",
    "mv",
    "cat",
    "od",
    "tr",
    "sleep",
    "date",
    "ls",
    "sort",
    "tail",
)
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
      *State.Health.Status*)
        # One state per line; each poll pops the first until one is left.
        h="$S/c/$name/health"
        [ -f "$h" ] || exit 0
        first=""; read -r first < "$h" || true
        rest=$(tail -n +2 "$h")
        [ -z "$rest" ] || printf '%s\n' "$rest" > "$h"
        printf '%s' "$first" ;;
      *State.Status*) cat "$S/c/$name/status" ;;
      *whygraph.pg_image*) cat "$S/c/$name/pg_image" 2>/dev/null || true ;;
      *whygraph.folders*) cat "$S/c/$name/folders" ;;
      *whygraph.port*) cat "$S/c/$name/port" ;;
      *whygraph.image*) cat "$S/c/$name/image" ;;
      *whygraph.dev_src*) cat "$S/c/$name/dev_src" 2>/dev/null || true ;;
    esac
    ;;
  run)
    name=""; folders=""; port=""; image=""; dev_src=""; pg_image=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --name) name="$2"; shift 2 ;;
        --label)
          case "$2" in
            whygraph.folders=*) folders="${2#whygraph.folders=}" ;;
            whygraph.port=*) port="${2#whygraph.port=}" ;;
            whygraph.image=*) image="${2#whygraph.image=}" ;;
            whygraph.dev_src=*) dev_src="${2#whygraph.dev_src=}" ;;
            whygraph.pg_image=*) pg_image="${2#whygraph.pg_image=}" ;;
          esac
          shift 2 ;;
        --mount)
          src="${2#*source=}"; src="${src%%,target=*}"
          [ -e "$src" ] || { echo "invalid mount config: bind source path does not exist: $src" >&2; exit 125; }
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
    if [ -n "$pg_image" ]; then
      printf '%s' "$pg_image" > "$S/c/$name/pg_image"
      printf '%s\n' "${FAKE_RUN_HEALTH:-healthy}" > "$S/c/$name/health"
    fi
    echo "fakecontainerid"
    ;;
  exec)
    name="$1"
    [ "$(cat "$S/c/$name/status" 2>/dev/null)" = "running" ] || { echo "container $name is not running" >&2; exit 1; }
    if [ -n "${FAKE_EXEC_FAIL:-}" ]; then
      printf 'partial'
      echo "pg_dump: error: connection failed" >&2
      exit 1
    fi
    printf '%s' "${FAKE_EXEC_PAYLOAD:-PGDMP-fake-dump}"
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

    def runs(self) -> list[list[str]]:
        return [c for c in self.calls() if c[0] == "run"]

    def run_call(self, name: str | None = PORTAL) -> list[str]:
        """The one ``docker run`` for container ``name`` (``None``: an unnamed one)."""

        def _name(call: list[str]) -> str | None:
            return call[call.index("--name") + 1] if "--name" in call else None

        runs = [c for c in self.runs() if _name(c) == name]
        assert len(runs) == 1, self.calls()
        return runs[0]

    def run_names(self) -> list[str]:
        return [c[c.index("--name") + 1] for c in self.runs() if "--name" in c]

    def make_container(self, name: str, status: str, **labels: str) -> None:
        d = self.state / "c" / name
        d.mkdir(parents=True)
        (d / "status").write_text(status)
        for key in ("folders", "port", "image"):
            (d / key).write_text(labels.get(key, ""))

    def make_pg_container(
        self,
        status: str = "running",
        health: str = "healthy",
        image: str = PG_IMAGE,
        name: str = PG,
    ) -> None:
        """Pre-create the database container, as an earlier ``up`` would have."""
        self.make_container(name, status)
        d = self.state / "c" / name
        (d / "pg_image").write_text(image)
        (d / "health").write_text(health + "\n")

    def health_polls(self) -> int:
        """How often the database's health was read (``sleep`` is a ksh builtin,
        so the polls are counted on the docker side)."""
        return sum(
            1
            for c in self.calls()
            if c[0] == "inspect" and "{{.State.Health.Status}}" in c
        )


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
    shim.make_pg_container()
    result = shim.run("up")
    assert result.returncode == 0, result.stderr
    assert [c for c in shim.calls() if c[0] == "rm"] == [
        ["rm", "-f", "whygraph-portal"]
    ]
    assert "stop" not in shim.verbs()  # nothing to stop
    assert shim.verbs()[-1] == "run"


def test_up_leaves_a_restarting_container_alone_and_says_so(shim: Shim) -> None:
    shim.make_container("whygraph-portal", "restarting", port="8765", image=IMAGE)
    shim.make_pg_container()
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


# --- the database container --------------------------------------------------


def _non_inspect(shim: Shim) -> list[list[str]]:
    return [c for c in shim.calls() if c[0] != "inspect"]


def _env_value(call: list[str], key: str) -> str:
    values = [a.split("=", 1)[1] for a in call if a.startswith(f"{key}=")]
    assert len(values) == 1, (key, call)
    return values[0]


def test_up_fresh_runs_the_database_first(shim: Shim) -> None:
    result = shim.run("up")
    assert result.returncode == 0, result.stderr
    assert shim.verbs() == ["network", "run", "run"]
    assert shim.run_names() == [PG, PORTAL]

    pg = shim.run_call(PG)
    data = shim.data.resolve()
    assert pg[:2] == ["run", "-d"]
    # Reachable only on the private network: nothing is published.
    assert "-p" not in pg
    assert not [a for a in pg if a.startswith("--publish")]
    assert pg[pg.index("--network") + 1] == "whygraph-portal"
    assert pg[pg.index("--restart") + 1] == "unless-stopped"
    assert pg[pg.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert f"whygraph.pg_image={PG_IMAGE}" in pg
    # The 18 image's VOLUME, never .../data; the password as a read-only file.
    assert f"type=bind,source={data}/postgres,target=/var/lib/postgresql" in pg
    assert (
        f"type=bind,source={data}/postgres.password,"
        "target=/run/secrets/whygraph-db-password,readonly"
    ) in pg
    assert _env_value(pg, "PGDATA") == f"/var/lib/postgresql/{POSTGRES_MAJOR}/docker"
    assert "POSTGRES_USER=whygraph" in pg
    assert "POSTGRES_DB=whygraph" in pg
    assert "POSTGRES_PASSWORD_FILE=/run/secrets/whygraph-db-password" in pg
    assert pg[-1] == PG_IMAGE

    portal = shim.run_call(PORTAL)
    url = _env_value(portal, "WHYGRAPH_DATABASE_URL")
    assert url == f"postgresql+psycopg://whygraph@{PG}:5432/whygraph"
    assert ":" not in url.split("://", 1)[1].split("@", 1)[0]  # no password
    assert "WHYGRAPH_DATABASE_PASSWORD_FILE=/data/postgres.password" in portal


def test_database_argv_has_shm_size_and_a_tcp_health_check(shim: Shim) -> None:
    assert shim.run("up").returncode == 0
    pg = shim.run_call(PG)
    assert "--shm-size=128m" in pg
    # TCP on purpose: the first-run temporary server listens on the socket only.
    health = pg[pg.index("--health-cmd") + 1]
    assert health == "pg_isready -h 127.0.0.1 -U whygraph -d whygraph"
    for flag, value in (
        ("--health-interval", "2s"),
        ("--health-timeout", "3s"),
        ("--health-retries", "30"),
        ("--health-start-period", "5s"),
    ):
        assert pg[pg.index(flag) + 1] == value, flag


def test_network_is_created_before_the_database_run(shim: Shim) -> None:
    assert shim.run("up").returncode == 0
    calls = shim.calls()
    network = calls.index(["network", "create", "whygraph-portal"])
    assert network < calls.index(shim.run_call(PG))


def test_password_is_generated_once_private_and_reused(shim: Shim) -> None:
    assert shim.run("up").returncode == 0
    pw_file = shim.data / "postgres.password"
    password = pw_file.read_text()
    assert re.fullmatch(r"[0-9a-f]{48}", password), password
    assert stat.S_IMODE(pw_file.stat().st_mode) == 0o600
    assert stat.S_IMODE((shim.data / "postgres").stat().st_mode) == 0o700
    assert not (shim.data / "postgres.password.tmp").exists()
    # Never in an argv.
    assert password not in (shim.state / "argv.log").read_text()

    assert shim.run("down").returncode == 0
    assert shim.run("up").returncode == 0
    assert pw_file.read_text() == password


def _pg_version(shim: Shim, major: str) -> None:
    d = shim.data / "postgres" / major / "docker"
    d.mkdir(parents=True)
    (d / "PG_VERSION").write_text(f"{major}\n")


def test_missing_password_with_existing_data_refuses(shim: Shim) -> None:
    _pg_version(shim, POSTGRES_MAJOR)
    result = shim.run("up")
    assert result.returncode == 2
    assert "postgres.password is missing" in result.stderr
    assert "restore" in result.stderr
    assert not (shim.data / "postgres.password").exists()
    assert shim.verbs() == []


@pytest.mark.parametrize(
    ("majors", "ok"),
    [
        (["17"], False),
        ([POSTGRES_MAJOR], True),
        (["17", POSTGRES_MAJOR], True),
    ],
)
def test_major_check(shim: Shim, majors: list[str], ok: bool) -> None:
    for major in majors:
        _pg_version(shim, major)
    (shim.data / "postgres.password").write_text("existing")
    result = shim.run("up")
    if ok:
        assert result.returncode == 0, result.stderr
        assert shim.run_names() == [PG, PORTAL]
        assert (shim.data / "postgres.password").read_text() == "existing"
    else:
        assert result.returncode == 2
        assert "created by Postgres 17" in result.stderr
        assert f"needs {POSTGRES_MAJOR}" in result.stderr
        assert (
            "https://mtrdesign.github.io/whygraph/portal/upgrading/#postgres-major"
            in result.stderr
        )
        assert "run" not in shim.verbs()


def test_pg_image_change_backs_up_then_recreates_both_portal_first(
    shim: Shim,
) -> None:
    assert shim.run("up").returncode == 0
    shim.clear_log()
    new_image = "postgres:18.7-trixie"
    result = shim.run("up", WHYGRAPH_POSTGRES_IMAGE=new_image)
    assert result.returncode == 0, result.stderr
    assert "changed: database" in result.stderr
    assert shim.verbs() == ["exec", "stop", "rm", "network", "stop", "rm", "run", "run"]
    acts = _non_inspect(shim)
    # The dump comes before any stop / rm of either container...
    assert acts[0][:3] == ["exec", PG, "pg_dump"]
    # ...and the portal stops before its database is removed.
    assert acts[1:3] == [["stop", "-t", "30", PORTAL], ["rm", PORTAL]]
    assert acts[4:6] == [["stop", "-t", "30", PG], ["rm", "-f", PG]]
    assert shim.run_names() == [PG, PORTAL]
    assert f"whygraph.pg_image={new_image}" in shim.run_call(PG)
    assert shim.run_call(PG)[-1] == new_image
    dumps = list((shim.data / "backups").glob("portal-*.dump"))
    assert len(dumps) == 1


def test_a_portal_only_change_leaves_the_database_alone(shim: Shim) -> None:
    assert shim.run("up").returncode == 0
    shim.clear_log()
    assert shim.run("up", "--port", "9001").returncode == 0
    assert shim.verbs() == ["stop", "rm", "network", "run"]
    assert not [c for c in _non_inspect(shim) if PG in c]
    assert shim.run_names() == [PORTAL]


def test_failing_pre_recreate_backup_touches_nothing(shim: Shim) -> None:
    assert shim.run("up").returncode == 0
    shim.clear_log()
    result = shim.run(
        "up", WHYGRAPH_POSTGRES_IMAGE="postgres:18.7-trixie", FAKE_EXEC_FAIL="1"
    )
    assert result.returncode == 2
    assert "backup failed" in result.stderr
    assert "WHYGRAPH_SKIP_BACKUP=1" in result.stderr
    assert shim.verbs() == ["exec"]  # no stop / rm / run at all
    for name in (PORTAL, PG):
        assert (shim.state / "c" / name / "status").read_text() == "running"
    assert (shim.state / "c" / PG / "pg_image").read_text() == PG_IMAGE
    assert list((shim.data / "backups").iterdir()) == []


def test_skip_backup_recreates_without_a_dump(shim: Shim) -> None:
    assert shim.run("up").returncode == 0
    shim.clear_log()
    result = shim.run(
        "up",
        WHYGRAPH_POSTGRES_IMAGE="postgres:18.7-trixie",
        WHYGRAPH_SKIP_BACKUP="1",
        FAKE_EXEC_FAIL="1",
    )
    assert result.returncode == 0, result.stderr
    assert "without a backup" in result.stderr
    assert "exec" not in shim.verbs()
    assert shim.run_names() == [PG, PORTAL]


def test_a_stopped_database_is_recreated_without_a_backup(shim: Shim) -> None:
    shim.make_container(PORTAL, "running", port="8765", image=IMAGE)
    shim.make_pg_container(status="exited")
    result = shim.run("up")
    assert result.returncode == 0, result.stderr
    assert "exec" not in shim.verbs()
    assert ["rm", "-f", PG] in shim.calls()
    assert ["stop", "-t", "30", PG] not in shim.calls()  # nothing to stop
    assert shim.run_names() == [PG, PORTAL]


def test_health_wait_polls_until_healthy(shim: Shim) -> None:
    result = shim.run("up", FAKE_RUN_HEALTH="starting\nstarting\nhealthy")
    assert result.returncode == 0, result.stderr
    assert shim.health_polls() == 3
    assert shim.run_names() == [PG, PORTAL]
    # The portal run comes only after the last (healthy) poll.
    calls = shim.calls()
    last_poll = max(i for i, c in enumerate(calls) if "{{.State.Health.Status}}" in c)
    assert last_poll < calls.index(shim.run_call(PORTAL))


def test_health_wait_budget_is_sixty_seconds(shim: Shim) -> None:
    block = _portal_block(shim)
    assert 'pg_budget="${WHYGRAPH_DB_READY_TIMEOUT:-60}"' in block
    assert "sleep 1" in block


@pytest.mark.parametrize("health", ["unhealthy", "starting"])
def test_unready_database_starts_no_portal(shim: Shim, health: str) -> None:
    result = shim.run("up", FAKE_RUN_HEALTH=health, WHYGRAPH_DB_READY_TIMEOUT="2")
    assert result.returncode == 2
    assert "did not become ready" in result.stderr
    assert f"docker logs {PG}" in result.stderr
    assert shim.run_names() == [PG]  # no portal run
    # `unhealthy` fails at once; `starting` waits out the budget.
    assert shim.health_polls() == (1 if health == "unhealthy" else 3)


def test_up_waits_for_an_existing_database_too(shim: Shim) -> None:
    shim.make_pg_container(health="unhealthy")
    result = shim.run("up")
    assert result.returncode == 2
    assert "did not become ready" in result.stderr
    assert shim.runs() == []


def test_down_with_a_missing_database_container(shim: Shim) -> None:
    shim.make_container(PORTAL, "running", port="8765", image=IMAGE)
    result = shim.run("down")
    assert result.returncode == 0, result.stderr
    assert _non_inspect(shim) == [["stop", "-t", "30", PORTAL], ["rm", PORTAL]]


def test_down_with_only_the_database_container(shim: Shim) -> None:
    shim.make_pg_container()
    result = shim.run("down")
    assert result.returncode == 0, result.stderr
    assert _non_inspect(shim) == [["stop", "-t", "30", PG], ["rm", PG]]
    assert "data stays in" in result.stdout


def test_status_shows_both_containers(shim: Shim) -> None:
    assert shim.run("up").returncode == 0
    result = shim.run("status")
    assert result.returncode == 0
    assert "whygraph portal: running" in result.stdout
    assert f"database: running ({PG_IMAGE})" in result.stdout


@pytest.mark.parametrize(
    ("portal", "database", "line"),
    [
        ("running", None, "database: not created"),
        ("running", "exited", f"database: exited ({PG_IMAGE})"),
        (None, "running", f"database: running ({PG_IMAGE})"),
    ],
)
def test_status_is_non_zero_when_either_container_is_down(
    shim: Shim, portal: str | None, database: str | None, line: str
) -> None:
    if portal:
        shim.make_container(PORTAL, portal, port="8765", image=IMAGE)
    if database:
        shim.make_pg_container(status=database)
    result = shim.run("status")
    assert result.returncode == 1
    assert line in result.stdout


def test_portal_name_knob_renames_the_database_container(shim: Shim) -> None:
    name = "whygraph-portal-dev"
    result = shim.run("up", WHYGRAPH_PORTAL_NAME=name)
    assert result.returncode == 0, result.stderr
    assert shim.run_names() == [f"{name}-postgres", name]
    url = _env_value(shim.run_call(name), "WHYGRAPH_DATABASE_URL")
    assert f"@{name}-postgres:5432/" in url
    assert not (shim.state / "c" / PG).exists()
    shim.run("down", WHYGRAPH_PORTAL_NAME=name)
    assert not (shim.state / "c" / f"{name}-postgres").exists()


def test_dev_mode_restarts_neither_container(shim: Shim) -> None:
    root = _checkout(shim)
    assert shim.run("up", WHYGRAPH_DEV_SRC=str(root)).returncode == 0
    for name in (PG, PORTAL):
        call = shim.run_call(name)
        assert call[call.index("--restart") + 1] == "no", name


# --- backup -------------------------------------------------------------------

_DUMP_NAME = re.compile(r"portal-\d{8}T\d{6}Z\.dump")


def test_backup_writes_a_dump_into_a_private_dir(shim: Shim) -> None:
    assert shim.run("up").returncode == 0
    shim.clear_log()
    result = shim.run("backup")
    assert result.returncode == 0, result.stderr
    backups = shim.data / "backups"
    assert stat.S_IMODE(backups.stat().st_mode) == 0o700
    files = sorted(p.name for p in backups.iterdir())
    assert len(files) == 1 and _DUMP_NAME.fullmatch(files[0]), files
    assert (backups / files[0]).read_text() == DUMP_PAYLOAD
    assert _non_inspect(shim) == [
        ["exec", PG, "pg_dump", "-U", "whygraph", "-d", "whygraph", "-Fc"]
    ]
    assert files[0] in result.stdout
    assert "secret.key" in result.stdout


def test_failing_backup_leaves_no_file_and_fails(shim: Shim) -> None:
    assert shim.run("up").returncode == 0
    result = shim.run("backup", FAKE_EXEC_FAIL="1")
    assert result.returncode != 0
    assert "backup failed" in result.stderr
    assert list((shim.data / "backups").iterdir()) == []


def test_backup_retention_keeps_the_newest_ten_and_nothing_else(shim: Shim) -> None:
    assert shim.run("up").returncode == 0
    backups = shim.data / "backups"
    backups.mkdir()
    old = [f"portal-20200101T0000{i:02d}Z.dump" for i in range(11)]
    others = ["notes.txt", "portal-before-upgrade.dump", "other.dump", "portal-x.sql"]
    for name in old + others:
        (backups / name).write_text("old")
    result = shim.run("backup")
    assert result.returncode == 0, result.stderr
    names = {p.name for p in backups.iterdir()}
    dumps = sorted(n for n in names if _DUMP_NAME.fullmatch(n))
    assert len(dumps) == 10
    # The two oldest went; the new dump and the nine newest old ones stay.
    assert dumps[:9] == old[2:]
    assert not set(old[:2]) & names
    assert set(others) <= names


@pytest.mark.parametrize("database", [None, "exited"])
def test_backup_refuses_when_the_database_is_not_running(
    shim: Shim, database: str | None
) -> None:
    if database:
        shim.make_pg_container(status=database)
    result = shim.run("backup")
    assert result.returncode == 2
    assert "not running" in result.stderr
    assert "whygraph up" in result.stderr
    assert "exec" not in shim.verbs()
    assert not (shim.data / "backups").exists()


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
    # Both containers, the portal first; each is stopped before it is removed.
    assert [c for c in shim.calls() if c[0] != "inspect"] == [
        ["stop", "-t", "30", PORTAL],
        ["rm", PORTAL],
        ["stop", "-t", "30", PG],
        ["rm", PG],
    ]
    assert not (shim.state / "c" / "whygraph-portal").exists()
    assert not (shim.state / "c" / PG).exists()


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
    shim.make_pg_container()
    result = shim.run("status")
    assert result.returncode == 1
    assert "whygraph portal: restarting" in result.stdout


def test_status_stopped_and_missing(shim: Shim) -> None:
    result = shim.run("status")
    assert result.returncode == 1
    assert "not created" in result.stdout
    shim.make_container("whygraph-portal", "exited", port="8765", image=IMAGE)
    shim.make_pg_container()
    result = shim.run("status")
    assert result.returncode == 1
    assert "whygraph portal: exited" in result.stdout


def test_logs_follows_the_portal_container(shim: Shim) -> None:
    result = shim.run("logs")
    assert result.returncode == 0
    assert "fake logs" in result.stdout
    assert ["logs", "-f", "whygraph-portal"] in shim.calls()


@pytest.mark.parametrize("verb", ["down", "status", "logs", "backup"])
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
    call = shim.run_call(None)
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
    call = shim.run_call("whygraph-portal-dev")
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
