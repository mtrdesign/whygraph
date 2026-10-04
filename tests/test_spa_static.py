"""Static guards on the SPA source (M2c plan section 5.5).

The session cookie is scoped to the base host's parent domain, so every org host
receives it. That is only safe while no org host serves user-controlled markup:
a stored script in one org could read or fix another's session. These checks pin
the invariant at the source level - no raw-HTML rendering in the React app.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PLAYGROUND = Path(__file__).resolve().parents[1] / "src" / "playground"
SRC = PLAYGROUND / "src"

# Code forms only: a comment that merely mentions a word does not match.
FORBIDDEN = {
    "dangerouslySetInnerHTML": re.compile(r"dangerouslySetInnerHTML\s*[=:{]"),
    "allowDangerousHtml": re.compile(r"allowDangerousHtml"),
    "rehype-raw import": re.compile(r"""from ["']rehype-raw"""),
}


def _sources() -> list[Path]:
    return [p for p in SRC.rglob("*") if p.suffix in {".ts", ".tsx"} and p.is_file()]


def test_playground_sources_found() -> None:
    """The scan below is vacuous if the path is wrong."""
    assert len(_sources()) > 20


def test_no_raw_html_rendering_in_the_spa() -> None:
    """No file under ``src/playground/src`` renders or allows raw HTML."""
    hits = []
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for label, pattern in FORBIDDEN.items():
            if pattern.search(text):
                hits.append(f"{path.relative_to(SRC)}: {label}")
    assert not hits, hits


def test_rehype_raw_is_not_a_dependency() -> None:
    """``rehype-raw`` would let chat Markdown carry HTML, so it must not be installed."""
    package = json.loads((PLAYGROUND / "package.json").read_text(encoding="utf-8"))
    declared = {
        **package.get("dependencies", {}),
        **package.get("devDependencies", {}),
        **package.get("optionalDependencies", {}),
    }
    assert "rehype-raw" not in declared
