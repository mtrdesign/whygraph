"""Static HTML viewer for the WhyGraph + CodeGraph data.

`run_render(out_path)` writes a self-contained HTML file with embedded
data; `run_serve(host, port)` starts a small local HTTP server with a
live `/api/rationale` endpoint backed by `whygraph_rationale_brief`.

Both share the data-assembly + template layers — they only differ in
how the page reaches the rationale on click (read embedded JSON vs.
fetch from the local server).
"""

from __future__ import annotations

from pathlib import Path

import click

from whygraph.render import data as data_module
from whygraph.render import server as server_module
from whygraph.render import template as template_module
from whygraph.scan import db as db_module
from whygraph.scan import git as git_module


def _resolve_paths(
    repo_root: Path | None = None,
) -> tuple[Path, Path, Path]:
    """Return ``(repo_root, codegraph_db, whygraph_db)`` or raise ``click.UsageError``."""
    cwd = repo_root if repo_root is not None else Path.cwd()
    try:
        root = git_module.repo_root(cwd)
    except git_module.GitError as exc:
        raise click.UsageError(f"Not a git repository: {exc}") from exc
    cg_path = root / ".codegraph" / "codegraph.db"
    if not cg_path.exists():
        raise click.UsageError(
            "CodeGraph not initialised. Run `whygraph init` first."
        )
    wg_path = db_module.default_db_path(root)
    if not wg_path.exists():
        raise click.UsageError(
            "WhyGraph DB not found. Run `whygraph scan` first."
        )
    return root, cg_path, wg_path


def run_render(
    out_path: Path | None = None,
    open_browser: bool = False,
    repo_root: Path | None = None,
    depth: int = 1,
) -> int:
    """Build a static HTML viewer and write it to ``out_path``.

    ``depth`` (1–4) caps which nodes get a populated detail block in the
    embedded JSON. Default 1 = modules only. Higher levels trade load
    time for click-through breadth in the same artifact.
    """
    import webbrowser

    root, cg_path, wg_path = _resolve_paths(repo_root)
    if out_path is None:
        out_path = root / ".whygraph" / "whygraph.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    payload = data_module.assemble(
        repo_root=root,
        codegraph_db=cg_path,
        whygraph_db=wg_path,
        runtime="static",
        depth=depth,
    )
    html = template_module.render(payload)
    out_path.write_text(html, encoding="utf-8")
    click.echo(f"Wrote {out_path} ({len(html):,} bytes, depth={depth}).")

    if open_browser:
        webbrowser.open(f"file://{out_path.resolve()}")
    return 0


def run_serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
    repo_root: Path | None = None,
) -> int:
    """Start a local HTTP server with the live viewer."""
    root, cg_path, wg_path = _resolve_paths(repo_root)
    server_module.serve(
        host=host,
        port=port,
        repo_root=root,
        codegraph_db=cg_path,
        whygraph_db=wg_path,
        open_browser=open_browser,
    )
    return 0
