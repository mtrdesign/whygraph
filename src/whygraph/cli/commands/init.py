"""The ``whygraph init`` subcommand — DB bootstrap and agent wiring."""

from __future__ import annotations

from pathlib import Path

import click

from whygraph import agents, assets

from ..console import fail


@click.command(name="init")
@click.option(
    "--agent",
    "agent_name",
    type=click.Choice(agents.known_agent_names(), case_sensitive=False),
    default=None,
    help="Wire the WhyGraph MCP server into the named LLM agent's config.",
)
@click.option(
    "--print",
    "print_only",
    is_flag=True,
    help="Print the MCP snippet to stdout instead of writing any config file.",
)
@click.option(
    "--list-agents",
    "list_agents",
    is_flag=True,
    help="List supported agents (with config-file paths) and exit.",
)
@click.option(
    "--install-assets/--no-install-assets",
    "install_assets",
    default=True,
    help=(
        "Copy the chosen agent's bundled assets (if any) into the project."
        " Default: enabled. No-op for agents that ship no asset tree."
    ),
)
@click.option(
    "--codegraph/--no-codegraph",
    "install_codegraph",
    default=True,
    help=(
        "Bootstrap CodeGraph via the vendored Docker image so the project"
        " ends up with a populated `.codegraph/codegraph.db`. Default:"
        " enabled. Idempotent — a re-run with the DB already present is"
        " a no-op."
    ),
)
@click.option(
    "--codegraph-image",
    "codegraph_image",
    default=None,
    help=(
        "Override the Docker image used for the CodeGraph bootstrap."
        " Defaults to the pinned ghcr.io/mtrdesign/whygraph-codegraph tag."
    ),
)
@click.option(
    "--skip-preflight",
    "skip_preflight",
    is_flag=True,
    help=(
        "Skip the host-tool diagnostics that normally run at the top of"
        " `whygraph init`. Use only in scripted environments where the"
        " environment is known-good."
    ),
)
@click.option(
    "--force",
    "force",
    is_flag=True,
    help=(
        "When installing assets, overwrite existing files in the agent's"
        " destination directory. Without this flag, existing files are"
        " left alone."
    ),
)
def init_cmd(
    agent_name: str | None,
    print_only: bool,
    list_agents: bool,
    install_assets: bool,
    install_codegraph: bool,
    codegraph_image: str | None,
    skip_preflight: bool,
    force: bool,
) -> None:
    """Initialize the WhyGraph database under ``.whygraph/whygraph.db``.

    Also bootstraps CodeGraph by default — runs the vendored Docker image
    to populate ``.codegraph/codegraph.db`` in the current repo. Pass
    ``--no-codegraph`` to skip that step.

    With ``--agent X``, registers the WhyGraph MCP server with the named
    agent. All supported agents are project-scoped — their MCP config
    file is written / merged in the repo. Pass ``--print`` to skip the
    write and emit the snippet for manual pasting.

    If the chosen agent ships a bundled asset tree (see
    :attr:`whygraph.agents.AgentTarget.has_assets`), the tree is copied
    into the matching destination directory under the repo. Pre-existing
    files are left alone unless ``--force`` is passed; pass
    ``--no-install-assets`` to skip the copy entirely.

    Idempotent — re-running on an already-initialized project just
    confirms both databases are present and at head.
    """
    if list_agents:
        _print_agent_list()
        return

    project_root = Path.cwd()

    if not skip_preflight:
        _run_preflight(project_root, with_codegraph=install_codegraph)

    db_path = _ensure_db_initialized()
    click.echo(f"Initialized WhyGraph database at {db_path}")

    if install_codegraph:
        _ensure_codegraph_bootstrapped(project_root, image=codegraph_image)
    else:
        click.echo(
            "Skipped CodeGraph bootstrap. Re-run without `--no-codegraph`"
            " — or run `codegraph init -i` against the repo by hand —"
            " before using WhyGraph's rationale or evidence tools."
        )

    if agent_name is None:
        click.echo(
            "Tip: run `whygraph init --list-agents` to see supported agents,"
            " then `whygraph init --agent <name>` to wire it up."
        )
        return

    target = agents.resolve_agent(agent_name)
    snippet = agents.render_snippet(target)

    if print_only or not agents.is_write_supported(target):
        _print_snippet(target, project_root, snippet)
    else:
        path = agents.write_snippet(target, project_root)
        click.echo(f"Wrote whygraph MCP entry to {path}")

    if install_assets and target.has_assets:
        result = assets.install_assets(target, project_root, force=force)
        _print_install_summary(target, project_root, result, force=force)


def _run_preflight(project_root: Path, *, with_codegraph: bool) -> None:
    """Echo the diagnostics block; ``fail`` with a clean error on missing tools.

    Imported here (not at module top) so ``--list-agents`` and ``--help``
    don't pay the import cost.
    """
    from whygraph.cli.preflight import PreflightError, run_preflight

    try:
        run_preflight(project_root, with_codegraph=with_codegraph)
    except PreflightError as exc:
        fail(str(exc))


def _ensure_codegraph_bootstrapped(
    project_root: Path, *, image: str | None
) -> None:
    """Idempotently materialise ``.codegraph/codegraph.db`` via Docker.

    Echoes a one-line status for both the "already initialized" and
    "bootstrapping now" cases, and ``fail``s with a clean message if the
    container run blows up. Lazy import keeps lightweight CLI surfaces
    fast (mirrors :func:`_ensure_db_initialized`).
    """
    from whygraph.services.codegraph import (
        CODEGRAPH_DB_RELPATH,
        CodeGraphBootstrapError,
        DEFAULT_CODEGRAPH_IMAGE,
        ensure_codegraph_db,
    )

    db_path = project_root / CODEGRAPH_DB_RELPATH
    if db_path.exists():
        click.echo(f"CodeGraph already initialized at {db_path}")
        return

    img = image or DEFAULT_CODEGRAPH_IMAGE
    click.echo(f"Bootstrapping CodeGraph via Docker (image: {img})...")
    try:
        result_path = ensure_codegraph_db(project_root, image=image)
    except CodeGraphBootstrapError as exc:
        fail(f"CodeGraph bootstrap failed: {exc}")
    click.echo(f"Initialized CodeGraph database at {result_path}")


def _ensure_db_initialized() -> Path:
    """Bootstrap the WhyGraph DB, lazy-importing the heavy chain.

    Imported here (not at module top) so that lightweight CLI surfaces
    like ``--list-agents`` and ``--help`` don't fail when the DB layer
    or its dependencies are mid-rewrite.
    """
    from whygraph.db import ensure_initialized

    return ensure_initialized()


def _print_agent_list() -> None:
    click.echo("Supported agents:")
    for name in sorted(agents.AGENTS):
        target = agents.AGENTS[name]
        aliases = f" (aliases: {', '.join(target.aliases)})" if target.aliases else ""
        path = agents.config_path_for(target, Path.cwd())
        scope = "project" if target.scope == "project" else "user"
        click.echo(f"  {target.name}{aliases}")
        click.echo(f"    scope: {scope}  format: {target.format}")
        click.echo(f"    path:  {path}")
        click.echo(f"    note:  {target.description}")


def _print_snippet(
    target: agents.AgentTarget, project_root: Path, snippet: str
) -> None:
    path = agents.config_path_for(target, project_root)
    click.echo(f"Paste the following into {path}:")
    click.echo("")
    click.echo(snippet.rstrip("\n"))


def _print_install_summary(
    target: agents.AgentTarget,
    project_root: Path,
    result: assets.InstallResult,
    *,
    force: bool,
) -> None:
    """Echo a one-paragraph summary of the asset install."""
    assert target.assets_dest is not None  # guaranteed by target.has_assets
    dest = project_root.joinpath(*target.assets_dest)
    click.echo(f"Installed assets for {target.name} under {dest}/:")
    click.echo(f"  written:     {len(result.written):>3} files")
    suffix = "" if force else " (pass --force to overwrite)"
    click.echo(f"  skipped:     {len(result.skipped):>3} files{suffix}")
    click.echo(f"  overwritten: {len(result.overwritten):>3} files")
