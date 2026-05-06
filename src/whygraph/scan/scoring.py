"""TF-IDF text-quality scoring for `whygraph scan`."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.progress import Progress, TaskID
from sklearn.feature_extraction.text import TfidfVectorizer

from whygraph.scan import db as db_module

_SCORED_TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "commits": ("sha", ("subject", "body")),
    "pull_requests": ("number", ("title", "body")),
    "issues": ("number", ("title", "body")),
}


@dataclass(frozen=True)
class FieldRef:
    table: str
    pk: str | int
    field: str
    text: str


def collect_corpus(db: db_module.Database) -> list[FieldRef]:
    """One FieldRef per (entity, field) for the six scored fields.

    Skips fields where text is None or strips to empty — those rows still get
    a final score of 0 because `write_scores` resets the columns first.
    """
    refs: list[FieldRef] = []
    cur = db._conn.cursor()
    for table, (pk_col, fields) in _SCORED_TABLES.items():
        cols = ", ".join((pk_col, *fields))
        cur.execute(f"SELECT {cols} FROM {table}")
        for row in cur.fetchall():
            pk = row[0]
            for field, value in zip(fields, row[1:], strict=True):
                if value and value.strip():
                    refs.append(FieldRef(table=table, pk=pk, field=field, text=value))
    return refs


def score_documents(
    refs: list[FieldRef],
) -> dict[tuple[str, str | int, str], float]:
    """Fit TF-IDF on the whole corpus, return mean-nonzero-weight score per ref."""
    if not refs:
        return {}
    vectorizer = TfidfVectorizer(
        stop_words="english",
        lowercase=True,
        token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z]+\b",
        norm=None,
    )
    try:
        matrix = vectorizer.fit_transform([r.text for r in refs])
    except ValueError:
        return {(r.table, r.pk, r.field): 0.0 for r in refs}

    out: dict[tuple[str, str | int, str], float] = {}
    for i, ref in enumerate(refs):
        row = matrix[i]
        data = row.data
        nnz = int(data.shape[0])
        out[(ref.table, ref.pk, ref.field)] = (
            float(data.sum()) / nnz if nnz else 0.0
        )
    return out


def write_scores(
    db: db_module.Database,
    scores: dict[tuple[str, str | int, str], float],
) -> int:
    """Reset every score column to 0, then UPDATE rows with computed scores."""
    cur = db._conn.cursor()
    for table, (_, fields) in _SCORED_TABLES.items():
        resets = ", ".join(f"{f}_tfidf_score = 0" for f in fields)
        cur.execute(f"UPDATE {table} SET {resets}")

    n_updated = 0
    for (table, pk, field), score in scores.items():
        if table not in _SCORED_TABLES:
            continue
        pk_col, fields = _SCORED_TABLES[table]
        if field not in fields:
            continue
        cur.execute(
            f"UPDATE {table} SET {field}_tfidf_score = ? WHERE {pk_col} = ?",
            (score, pk),
        )
        n_updated += cur.rowcount
    db._conn.commit()
    return n_updated


def percentile_threshold(
    db: db_module.Database,
    table: str,
    field: str,
    *,
    fraction: float = 0.2,
) -> float:
    """Score at the fractional cutoff inside `table.field`.

    `fraction=0.2` returns the score below which the lowest-scoring 20% of rows
    sit. A row passes the gate iff its score is strictly greater than the
    returned threshold.

    Empty tables return 0 (no rows to filter, threshold trivially admits any
    future row).
    """
    if table not in _SCORED_TABLES:
        raise ValueError(f"unknown table {table!r}")
    _, fields = _SCORED_TABLES[table]
    if field not in fields:
        raise ValueError(f"unknown field {field!r} on table {table!r}")
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("fraction must be in [0, 1]")

    column = f"{field}_tfidf_score"
    cur = db._conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    total = int(cur.fetchone()[0])
    if total == 0:
        return 0.0
    offset = int(fraction * (total - 1))
    cur.execute(
        f"SELECT {column} FROM {table} ORDER BY {column} LIMIT 1 OFFSET ?",
        (offset,),
    )
    row = cur.fetchone()
    return float(row[0]) if row else 0.0


@dataclass(frozen=True)
class ValueGate:
    """Per-session threshold cache for the six scored fields.

    Build once when you start iterating over many rows so the threshold
    queries don't re-fire per-row:

        gate = ValueGate.percentile(db, fraction=0.2)
        for sha, subject, score in db.iter_commit_subjects():
            if gate.is_above("commits", "subject", score):
                ...

    Construct via the `percentile` classmethod; the dataclass `__init__` is
    not part of the public API.
    """

    thresholds: dict[tuple[str, str], float]

    @classmethod
    def percentile(
        cls,
        db: db_module.Database,
        *,
        fraction: float = 0.2,
    ) -> ValueGate:
        thresholds: dict[tuple[str, str], float] = {}
        for table, (_, fields) in _SCORED_TABLES.items():
            for field in fields:
                thresholds[(table, field)] = percentile_threshold(
                    db, table, field, fraction=fraction
                )
        return cls(thresholds=thresholds)

    def threshold_for(self, table: str, field: str) -> float:
        return self.thresholds[(table, field)]

    def is_above(self, table: str, field: str, score: float) -> bool:
        return score > self.thresholds[(table, field)]


def run_scoring_phase(
    db_path: Path,
    progress: Progress,
    task_id: TaskID,
) -> str:
    """Open DB, collect corpus, fit/score, write back. Return summary string."""
    with db_module.Database(db_path) as db:
        progress.update(task_id, description="score (collecting)")
        refs = collect_corpus(db)
        total = max(len(refs), 1)
        progress.update(task_id, total=total, completed=0, description="score (fitting)")
        scores = score_documents(refs)
        progress.update(task_id, completed=total, description="score (writing)")
        write_scores(db, scores)
        progress.update(task_id, description="score")

    by_table: dict[str, set[str | int]] = {t: set() for t in _SCORED_TABLES}
    for r in refs:
        by_table[r.table].add(r.pk)
    return (
        f"{len(refs)} fields across "
        f"{len(by_table['commits'])} commits, "
        f"{len(by_table['pull_requests'])} PRs, "
        f"{len(by_table['issues'])} issues"
    )
