"""The wheel build hook builds the playground only when Node is new enough.

``hatchling`` is the build backend, not a runtime dependency, so it is stubbed here; the
hook module itself is loaded from the repo root by path.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def hatch_build(monkeypatch):
    """Import ``hatch_build.py`` with a stub ``BuildHookInterface``."""

    class BuildHookInterface:
        def __init__(self, root: str):
            self.root = root
            self.app = types.SimpleNamespace(
                warnings=[],
                infos=[],
                display_warning=lambda m: self.app.warnings.append(m),
                display_info=lambda m: self.app.infos.append(m),
            )

    stub = types.ModuleType("hatchling.builders.hooks.plugin.interface")
    stub.BuildHookInterface = BuildHookInterface
    for name in (
        "hatchling",
        "hatchling.builders",
        "hatchling.builders.hooks",
        "hatchling.builders.hooks.plugin",
    ):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "hatchling.builders.hooks.plugin.interface", stub)

    spec = importlib.util.spec_from_file_location(
        "_hatch_build_under_test", ROOT / "hatch_build.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_run(node_output: str | None, calls: list[list[str]]):
    def run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["node", "-v"]:
            if node_output is None:
                raise FileNotFoundError("node")
            return subprocess.CompletedProcess(cmd, 0, stdout=node_output, stderr="")
        return subprocess.CompletedProcess(cmd, 0)

    return run


def _project(tmp_path: Path) -> Path:
    (tmp_path / "src" / "playground").mkdir(parents=True)
    (tmp_path / "src" / "playground" / "package.json").write_text("{}")
    return tmp_path


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("v22.12.0\n", (22, 12)),
        ("v24.15.0", (24, 15)),
        ("v16.20.2", (16, 20)),
        ("garbage", None),
    ],
)
def test_node_version_parses(hatch_build, monkeypatch, output, expected):
    monkeypatch.setattr(hatch_build.subprocess, "run", _fake_run(output, []))
    assert hatch_build._node_version() == expected


def test_node_version_none_when_node_missing(hatch_build, monkeypatch):
    monkeypatch.setattr(hatch_build.subprocess, "run", _fake_run(None, []))
    assert hatch_build._node_version() is None


@pytest.mark.parametrize("output", ["v20.19.0", "v22.11.0", "v16.20.2", None])
def test_old_or_missing_node_warns_and_skips(
    hatch_build, monkeypatch, tmp_path, output
):
    calls: list[list[str]] = []
    monkeypatch.setattr(hatch_build.shutil, "which", lambda name: "/usr/bin/npm")
    monkeypatch.setattr(hatch_build.subprocess, "run", _fake_run(output, calls))
    hook = hatch_build.PlaygroundBuildHook(str(_project(tmp_path)))

    hook.initialize("0.0.0", {})  # must not raise: uv sync --dev runs this in CI

    assert any("Node >= 22.12" in w for w in hook.app.warnings)
    assert ["npm", "ci"] not in calls
    assert ["npm", "run", "build"] not in calls


@pytest.mark.parametrize("output", ["v22.12.0", "v22.22.2", "v24.15.0"])
def test_new_enough_node_builds(hatch_build, monkeypatch, tmp_path, output):
    calls: list[list[str]] = []
    monkeypatch.setattr(hatch_build.shutil, "which", lambda name: "/usr/bin/npm")
    monkeypatch.setattr(hatch_build.subprocess, "run", _fake_run(output, calls))
    hook = hatch_build.PlaygroundBuildHook(str(_project(tmp_path)))

    hook.initialize("0.0.0", {})

    assert hook.app.warnings == []
    assert ["npm", "ci"] in calls
    assert ["npm", "run", "build"] in calls


def test_prebuilt_bundle_is_a_noop(hatch_build, monkeypatch, tmp_path):
    static = tmp_path / "src" / "whygraph" / "serve" / "static"
    static.mkdir(parents=True)
    (static / "index.html").write_text("<html></html>")
    calls: list[list[str]] = []
    monkeypatch.setattr(hatch_build.subprocess, "run", _fake_run("v16.0.0", calls))
    hook = hatch_build.PlaygroundBuildHook(str(_project(tmp_path)))

    hook.initialize("0.0.0", {})

    assert calls == []
    assert hook.app.warnings == []
