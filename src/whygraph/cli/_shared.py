"""Helpers shared across more than one ``whygraph`` subcommand."""

from __future__ import annotations


def _configure_logging_best_effort() -> None:
    """Configure logging if the core dependency chain is healthy.

    Failures here are tolerated so the CLI can still expose pure-CLI
    surfaces (``--help``, ``--list-agents``) while parts of the package
    are in flux.
    """
    try:
        from whygraph.core import configure_logging, get_config

        cfg = get_config()
        configure_logging(cfg.log_level, file_config=cfg.logging)
    except Exception:  # noqa: BLE001 — best-effort, intentional
        pass
