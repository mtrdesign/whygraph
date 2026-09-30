"""Hatchling build hook that ships the Explorer SPA bundle in the wheel.

The React playground (``src/playground/``) builds to ``src/whygraph/serve/static/``, which
is gitignored and produced only at build time. This hook makes ``uv tool install``
/ ``pip install`` from a source tree build the bundle automatically, so the wheel
always carries a working SPA.

Behaviour, in order:

1. If ``src/whygraph/serve/static/index.html`` already exists, do nothing — the
   Docker image ``COPY --from``s a pre-built bundle before ``pip install``, so the
   hook must be a **no-op** there (the image build never runs npm).
2. Else, if ``src/playground/``, ``npm`` and a new-enough Node (>= 22.12, the floor of
   the Vite / Vitest toolchain) are all present, run ``npm ci`` + ``npm run build`` to
   populate ``static/``.
3. Else (no bundle, no npm, or an older Node), warn and continue: the server still
   runs and its ``/`` route reports the UI is not built (see
   :mod:`whygraph.serve.app`). An old Node must only *warn*, never fail, because
   ``uv sync --dev`` in the ``lint`` and ``test`` CI jobs runs this hook on runners
   whose default Node predates the floor.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

# The playground toolchain (Vite 8, Vitest 5) needs Node >= 22.12; keep in step with
# `engines` in src/playground/package.json and `make node-check`.
MIN_NODE = (22, 12)


def _node_version() -> tuple[int, int] | None:
    """Return Node's ``(major, minor)``, or ``None`` if it cannot be determined."""
    try:
        out = subprocess.run(
            ["node", "-v"], capture_output=True, text=True, check=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.match(r"v?(\d+)\.(\d+)", out.strip())
    return (int(match[1]), int(match[2])) if match else None


class PlaygroundBuildHook(BuildHookInterface):
    """Build the playground bundle into the package tree before packaging."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        root = Path(self.root)
        static = root / "src" / "whygraph" / "serve" / "static"
        playground = root / "src" / "playground"

        if (static / "index.html").is_file():
            # Already built (Docker COPY --from, or a prior `make playground`).
            return

        if not (playground / "package.json").is_file():
            self.app.display_warning(
                "src/playground/ not found — packaging without the Explorer SPA bundle; "
                "the portal will report the UI is not built at /."
            )
            return

        if shutil.which("npm") is None:
            self.app.display_warning(
                "npm not found — packaging without the Explorer SPA bundle; "
                "install Node and rebuild, or run `make playground`."
            )
            return

        node = _node_version()
        if node is None or node < MIN_NODE:
            have = "unknown" if node is None else ".".join(map(str, node))
            self.app.display_warning(
                f"Node >= {'.'.join(map(str, MIN_NODE))} is required to build the Explorer SPA "
                f"(have {have}) - packaging without the bundle; the portal will report "
                "the UI is not built at /. Upgrade Node (e.g. `nvm use 22`) and rebuild, "
                "or run `make playground`."
            )
            return

        self.app.display_info("building Explorer playground (npm ci && npm run build)…")
        subprocess.run(["npm", "ci"], cwd=playground, check=True)
        subprocess.run(["npm", "run", "build"], cwd=playground, check=True)
