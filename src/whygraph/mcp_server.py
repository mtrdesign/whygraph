from __future__ import annotations

import sqlite3
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from whygraph.backend import GraphBackend, SqliteCodegraphBackend, SymbolNode
from whygraph.config import Config, load_config
from whygraph.db import open_whygraph_db
from whygraph.evidence import (
    CollectionResult,
    EvidenceService,
    EvidenceStore,
    GitEvidenceCollector,
    GitHubEvidenceCollector,
)

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


def _build_evidence_service(
    config: Config, conn: sqlite3.Connection
) -> EvidenceService:
    return EvidenceService(
        EvidenceStore(conn),
        GitEvidenceCollector(config.repo_root),
        GitHubEvidenceCollector(config.repo_root),
        config.repo_root,
        config.evidence_ttl_seconds,
    )


def _evidence_payload(node: SymbolNode, collection: CollectionResult) -> dict[str, Any]:
    return {
        "qualified_name": node.qualified_name,
        "node_id": node.id,
        "location": f"{node.file_path}:{node.start_line}-{node.end_line}",
        "source": collection.source,
        "bundle_hash": collection.bundle_hash,
        "head_at_collection": collection.head_at_collection,
        "collected_at": collection.collected_at,
        "evidence": [
            {
                "source": e.source,
                "ref": e.ref,
                "collected_at": e.collected_at,
                "payload": e.payload,
            }
            for e in collection.evidence
        ],
    }


def _evidence_summary(payload: dict[str, Any]) -> str:
    summary = (
        payload.get("summary")
        or payload.get("subject")
        or payload.get("title")
        or ""
    )
    return summary


def format_evidence_markdown(
    node: SymbolNode, collection: CollectionResult
) -> str:
    head = collection.head_at_collection
    lines = [
        f"# Evidence: `{node.qualified_name}`",
        "",
        f"- **Location**: {node.file_path}:{node.start_line}-{node.end_line}",
        f"- **Source**: {collection.source} (bundle {collection.bundle_hash[:12]})",
        f"- **HEAD at collection**: {head[:12] if head else '(none)'}",
        f"- **Items**: {len(collection.evidence)}",
        "",
    ]
    if not collection.evidence:
        lines.append("_(no evidence)_")
    else:
        for e in collection.evidence:
            ref = f"`{e.ref[:12]}`" if e.ref else "`-`"
            payload = e.payload if isinstance(e.payload, dict) else {}
            lines.append(f"- **{e.source}** {ref} — {_evidence_summary(payload)}")
    return "\n".join(lines)


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
) -> dict[str, Any] | str:
    config = load_config()
    backend = _open_backend(config)
    conn = open_whygraph_db(config.whygraph_db_path)
    try:
        node = _resolve_symbol(backend, target)
        if node is None:
            raise ValueError(f"Symbol not found in CodeGraph: {target}")
        service = _build_evidence_service(config, conn)
        collection = service.for_node(node, force=refresh)
        if response_format == "json":
            return _evidence_payload(node, collection)
        return format_evidence_markdown(node, collection)
    finally:
        conn.close()
        backend.close()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
