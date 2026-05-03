from __future__ import annotations

import atexit
import sqlite3
from dataclasses import dataclass
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
from whygraph.rationale import (
    RationaleRecord,
    RationaleService,
    RationaleStore,
    make_llm_client,
)

mcp = FastMCP("whygraph")

ResponseFormat = Literal["markdown", "json"]


class NoCodeGraphError(ValueError):
    """Raised when no .codegraph/codegraph.db is reachable from the cwd."""


@dataclass
class _Deps:
    backend: GraphBackend
    conn: sqlite3.Connection
    evidence_service: EvidenceService
    rationale_service: RationaleService
    model: str

    def close(self) -> None:
        try:
            self.backend.close()
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass


# Global installs reuse a single MCP server entry across many projects, but
# only some of those projects have a CodeGraph DB. Failing fast at startup
# turns every `claude mcp list` into a confusing "Failed to connect", so we
# defer dep construction until the first tool call and return a clean error
# then.
_DEPS: _Deps | None = None


def _resolve_deps(config: Config) -> _Deps:
    if config.codegraph_db_path is None:
        raise NoCodeGraphError(
            f"No CodeGraph DB found at or above {config.repo_root} "
            "(looked for .codegraph/codegraph.db). Index this project with "
            "CodeGraph first, or set CODEGRAPH_DB to an absolute path."
        )
    backend = SqliteCodegraphBackend(config.codegraph_db_path)
    conn = open_whygraph_db(config.whygraph_db_path)
    evidence_service = EvidenceService(
        EvidenceStore(conn),
        GitEvidenceCollector(config.repo_root),
        GitHubEvidenceCollector(config.repo_root),
        config.repo_root,
        config.evidence_ttl_seconds,
    )
    rationale_service = RationaleService(
        RationaleStore(conn),
        make_llm_client(config),
        model=config.model,
    )
    return _Deps(
        backend=backend,
        conn=conn,
        evidence_service=evidence_service,
        rationale_service=rationale_service,
        model=config.model,
    )


def _get_deps() -> _Deps:
    global _DEPS
    if _DEPS is None:
        _DEPS = _resolve_deps(load_config())
    return _DEPS


def _reset_deps() -> None:
    """Test helper: close and clear cached deps."""
    global _DEPS
    if _DEPS is not None:
        _DEPS.close()
        _DEPS = None


def _atexit_close() -> None:
    if _DEPS is not None:
        _DEPS.close()


atexit.register(_atexit_close)


def _resolve_symbol(backend: GraphBackend, target: str) -> SymbolNode | None:
    return backend.get_node_by_id(target) or backend.get_node(target)


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
    return (
        payload.get("summary")
        or payload.get("subject")
        or payload.get("title")
        or ""
    )


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


def _rationale_payload(
    node: SymbolNode,
    collection: CollectionResult,
    record: RationaleRecord,
    source: str,
) -> dict[str, Any]:
    return {
        "qualified_name": node.qualified_name,
        "kind": node.kind,
        "location": f"{node.file_path}:{node.start_line}-{node.end_line}",
        "source": source,
        "evidence_source": collection.source,
        "model": record.model,
        "prompt_version": record.prompt_version,
        "bundle_hash": record.bundle_hash,
        "cache_key": record.cache_key,
        "generated_at": record.generated_at,
        "purpose": record.purpose,
        "why": record.why,
        "constraints": record.constraints,
        "tradeoffs": record.tradeoffs,
        "risks": record.risks,
    }


def _bullets(items: list[str]) -> list[str]:
    return ["_(none)_"] if not items else [f"- {item}" for item in items]


def format_rationale_markdown(
    node: SymbolNode,
    collection: CollectionResult,
    record: RationaleRecord,
    source: str,
) -> str:
    lines = [
        f"# Rationale: `{node.qualified_name}`",
        "",
        f"- **Kind**: {node.kind}",
        f"- **Location**: {node.file_path}:{node.start_line}-{node.end_line}",
        f"- **Model**: {record.model} (prompt {record.prompt_version})",
        (
            f"- **Rationale**: {source} · **Evidence**: {collection.source} "
            f"(bundle {record.bundle_hash[:12]})"
        ),
        "",
        "## Purpose",
        record.purpose or "_(none)_",
        "",
        "## Why",
        record.why or "_(none)_",
        "",
        "## Constraints",
    ]
    lines.extend(_bullets(record.constraints))
    lines.append("")
    lines.append("## Tradeoffs")
    lines.extend(_bullets(record.tradeoffs))
    lines.append("")
    lines.append("## Risks")
    lines.extend(_bullets(record.risks))
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
    deps = _get_deps()
    node = _resolve_symbol(deps.backend, target)
    if node is None:
        raise ValueError(f"Symbol not found in CodeGraph: {target}")
    collection = deps.evidence_service.for_node(node, force=refresh)
    if response_format == "json":
        return _evidence_payload(node, collection)
    return format_evidence_markdown(node, collection)


@mcp.tool(
    name="whygraph_rationale_pre_edit_brief",
    description=(
        "Return the rationale for a code symbol BEFORE editing it: purpose, "
        "why it exists, constraints to preserve, tradeoffs, and risks of "
        "modification. Lazily collects evidence (git blame + commits, plus "
        "GitHub PRs/issues if available) on first request; subsequent "
        "requests reuse the cache when (bundle_hash, prompt_version, model) "
        "matches.\n\n"
        "Args:\n"
        "  target: CodeGraph node ID or qualified_name.\n"
        "  force: If true, bypass the rationale cache and regenerate. "
        "Default false.\n"
        "  refresh_evidence: If true, recollect evidence even if cached "
        "(implies bypassing the rationale cache). Default false.\n"
        "  response_format: 'markdown' or 'json'. Default 'markdown'."
    ),
)
def rationale_pre_edit_brief(
    target: str,
    force: bool = False,
    refresh_evidence: bool = False,
    response_format: ResponseFormat = "markdown",
) -> dict[str, Any] | str:
    deps = _get_deps()
    node = _resolve_symbol(deps.backend, target)
    if node is None:
        raise ValueError(f"Symbol not found in CodeGraph: {target}")
    collection = deps.evidence_service.for_node(node, force=refresh_evidence)
    if not collection.evidence:
        raise ValueError(
            f"No evidence for {node.qualified_name}: file has no git "
            f"history ({node.file_path})."
        )
    record, source = deps.rationale_service.get_or_generate(
        node,
        collection.evidence,
        collection.bundle_hash,
        force=force or refresh_evidence,
    )
    if response_format == "json":
        return _rationale_payload(node, collection, record, source)
    return format_rationale_markdown(node, collection, record, source)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
