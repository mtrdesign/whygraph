"""Template rendering — assemble the self-contained HTML page.

The template uses simple ``{{TOKEN}}`` placeholders rather than Jinja
to avoid pulling a dep. Every JS/CSS asset is inlined so the output is
a single drop-and-double-click file.
"""

from __future__ import annotations

import json
from pathlib import Path

_ASSETS = Path(__file__).parent / "assets"


def _read(name: str) -> str:
    return (_ASSETS / name).read_text(encoding="utf-8")


def render(payload: dict) -> str:
    """Substitute the payload + asset bodies into the template."""
    html = _read("template.html")
    cytoscape = _read("cytoscape.min.js")
    app_js = _read("app.js")
    style_css = _read("style.css")
    data_json = json.dumps(payload, ensure_ascii=False)

    # Replace tokens. Order matters — DATA last so we don't accidentally
    # inject braces that match earlier tokens.
    return (
        html.replace("{{STYLE_CSS}}", style_css)
        .replace("{{CYTOSCAPE_JS}}", cytoscape)
        .replace("{{APP_JS}}", app_js)
        .replace("{{DATA}}", data_json)
    )
