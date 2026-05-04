from __future__ import annotations

import json
import sqlite3

from whygraph.evidence.types import (
    BundleMeta,
    EvidenceRecord,
    EvidenceRow,
    compute_bundle_hash,
)


class EvidenceStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def replace(
        self,
        node_id: str,
        qualified_name: str,
        rows: list[EvidenceRow],
        head_at_collection: str | None,
        *,
        now: int,
    ) -> str:
        bundle_hash = compute_bundle_hash(rows)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "DELETE FROM evidence WHERE node_id = ?", (node_id,)
            )
            for row in rows:
                self._conn.execute(
                    "INSERT INTO evidence "
                    "(node_id, qualified_name, source, ref, payload, collected_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        node_id,
                        qualified_name,
                        row.source,
                        row.ref,
                        json.dumps(row.payload),
                        now,
                    ),
                )
            self._conn.execute(
                "INSERT INTO evidence_bundles "
                "(node_id, bundle_hash, built_at, head_at_collection) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(node_id) DO UPDATE SET "
                "bundle_hash = excluded.bundle_hash, "
                "built_at = excluded.built_at, "
                "head_at_collection = excluded.head_at_collection",
                (node_id, bundle_hash, now, head_at_collection),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return bundle_hash

    def bundle_meta_for(self, node_id: str) -> BundleMeta | None:
        row = self._conn.execute(
            "SELECT bundle_hash, built_at, head_at_collection "
            "FROM evidence_bundles WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        if row is None:
            return None
        return BundleMeta(
            bundle_hash=row["bundle_hash"],
            built_at=int(row["built_at"]),
            head_at_collection=row["head_at_collection"],
        )

    def for_node(self, node_id: str) -> list[EvidenceRecord]:
        rows = self._conn.execute(
            "SELECT id, node_id, qualified_name, source, ref, payload, collected_at "
            "FROM evidence WHERE node_id = ? ORDER BY id",
            (node_id,),
        ).fetchall()
        records = []
        for r in rows:
            try:
                payload = json.loads(r["payload"])
            except (ValueError, TypeError):
                payload = r["payload"]
            records.append(
                EvidenceRecord(
                    id=int(r["id"]),
                    node_id=r["node_id"],
                    qualified_name=r["qualified_name"],
                    source=r["source"],
                    ref=r["ref"],
                    payload=payload,
                    collected_at=int(r["collected_at"]),
                )
            )
        return records
