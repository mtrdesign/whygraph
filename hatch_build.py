"""Hatchling build hook that ships the Explorer SPA bundle in the wheel.

The React playground (``src/playground/``) builds to ``src/whygraph/serve/static/``, which
is gitignored and produced only at build time. This hook makes ``uv tool install``
/ ``pip install`` from a source tree build the bundle automatically, so the wheel
always carries a working SPA.

Behaviour, in order:

1. If ``src/whygraph/serve/static/index.html`` already exists, do nothing — the
   Docker image ``COPY --from``s a pre-built bundle before ``pip install``, so the
   hook must be a **no-op** there (the image build never runs npm).
2. Else, if ``src/playground/`` and ``npm`` are both present, run ``npm ci`` +
   ``npm run build`` to populate ``static/``.
3. Else (no bundle, no npm), warn and continue: the server still runs and its
   ``/`` route reports the UI is not built (see :mod:`whygraph.serve.app`).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


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
                "`whygraph serve` will report the UI is not built at /."
            )
            return

        if shutil.which("npm") is None:
            self.app.display_warning(
                "npm not found — packaging without the Explorer SPA bundle; "
                "install Node and rebuild, or run `make playground`."
            )
            return

        self.app.display_info("building Explorer playground (npm ci && npm run build)…")
        subprocess.run(["npm", "ci"], cwd=playground, check=True)
        subprocess.run(["npm", "run", "build"], cwd=playground, check=True)
