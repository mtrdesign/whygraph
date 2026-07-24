"""The ``whygraph serve`` subcommand — run the Explorer panel HTTP server.

Deliberately "dumb": it only ever runs a **foreground uvicorn** bound to a socket.
It knows nothing about Docker, port forwarding, or container lifecycle — the
``whygraph`` shim's ``serve`` branch owns all of that (it publishes the port and
manages ``--detach`` / ``--stop`` / ``--logs``). This command, running *inside* the
container, is simply the server.

``--host`` defaults to ``127.0.0.1`` (safe for the native ``uv tool install`` path,
which has no container boundary); the shim passes ``--host 0.0.0.0`` for the
container so Docker's ``-p 127.0.0.1:PORT:PORT`` forward can reach uvicorn.
"""

from __future__ import annotations

import click


@click.command(name="serve")
@click.option("--port", default=8765, show_default=True, help="Port to bind.")
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Bind address (the shim passes 0.0.0.0 for the container).",
)
def serve_cmd(port: int, host: str) -> None:
    """Serve the WhyGraph Explorer panel for this repository."""
    # Lazy-imported so `--help` stays fast and doesn't require the HTTP stack.
    import uvicorn

    from whygraph.core import get_config
    from whygraph.serve.app import create_app

    from ..console import console

    app = create_app(get_config())
    console.print(f"[bold]WhyGraph Explorer[/] → http://localhost:{port}")
    uvicorn.run(app, host=host, port=port, log_config=None)
