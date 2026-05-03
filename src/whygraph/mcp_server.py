from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from whygraph.backend import GraphBackend, SqliteCodegraphBackend, SymbolNode
from whygraph.config import Config, load_config

mcp = FastMCP("whygraph")

ResponseFormat = Literal["markdown", "json"]


def _resolve_symbol(backend: GraphBackend, target: str) -> SymbolNode | None:
    return backend.get_node_by_id(target) or backend.get_node(target)


def _open_backend(config: Config) -> GraphBackend:
    if config.codegraph_db_path is None:
        raise ValueError(
            f"No CodeGraph DB found at or above {config.repo_root} "
            "(looked for .codegraph/codegraph.db). Index this project with "
            "CodeGraph first, or set CODEGRAPH_DB to an absolute path."
        )
    return SqliteCodegraphBackend(config.codegraph_db_path)


@mcp.tool(
    name="whygraph_evidence_for",
    description=(
        "Return evidence rows for a code symbol — git commits, blame, and "
        "(when available) PRs/issues. Cached per project; recollects when the "
        "file's HEAD sha advances or after the TTL. Never calls Claude.\n\n"
        "Args:\n"
        "  target: CodeGraph node ID or qualified_name.\n"
        "  refresh: If true, recollect even if cached. Default false.\n"
        "  response_format: 'markdown' or 'json'. Default 'markdown'."
    ),
)
def evidence_for(
    target: str,
    refresh: bool = False,
    response_format: ResponseFormat = "markdown",
) -> dict[str, Any]:
    config = load_config()
    backend = _open_backend(config)
    try:
        node = _resolve_symbol(backend, target)
        if node is None:
            raise ValueError(f"Symbol not found in CodeGraph: {target}")
        return {
            "qualified_name": node.qualified_name,
            "node_id": node.id,
            "location": f"{node.file_path}:{node.start_line}-{node.end_line}",
            "evidence": [],
            "source": "stub",
        }
    finally:
        backend.close()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
