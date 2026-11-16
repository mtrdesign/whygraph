"""Read one Claude Code session and print what the run did (a dev tool).

It reads the main transcript and the ``subagents/`` transcripts with the same
loader the eval checks use, and prints counts, times and pointers: how long each
agent ran and where the time went, the tokens, the stops, the ``plan-tool.sh``
calls, the review results per lens, failed tool calls, and a fixed list of Bash
commands a person may want to look at.

Rules: it prints no prompt text and no file content, and it calls no network. A
flag is a pointer for a person, never a verdict. Its output stays out of tracked
files. Every token total is a lower bound: harness-side summary requests are not
in the transcripts.

Usage
-----
From the repository root::

    uv run python scripts/agent_evals/audit.py <session id | path to .jsonl> [--json]
    uv run python scripts/agent_evals/audit.py --compare <record A> <record B>
    uv run python scripts/agent_evals/audit.py --self-check
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from checks import Transcript, _transcript_tokens, load_transcripts  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "audit"
REVIEW_KINDS = ("applied", "refuted", "asked", "blocked", "deferred")
LENSES = ("logic", "ui", "journey")
NO_CHANGE_SUBS = ("verify", "end")
STEP_SUBS = (
    "verify",
    "begin",
    "end",
    "pass",
    "attempt",
    "baseline",
    "amends",
    "amend-check",
    "constraints",
)
FLAG_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("network client", re.compile(r"\b(curl|wget|nc|ncat|ssh|scp|httpie)\b")),
    ("env file read", re.compile(r"(^|[\s/=\"'])\.env(\.[\w-]+)?(\s|$|[\"';|&])")),
    (
        "write under .git or agent dir",
        re.compile(
            r"(>|\btee\b|\bcp\b|\bmv\b|\brm\b|\bsed\s+-i|\btouch\b|\bmkdir\b)"
            r"[^|;&]*(\.git/|\.claude/|\.cursor/|\.codex/|\.github/)"
        ),
    ),
    ("git push", re.compile(r"\bgit\b[^;&|]*\bpush\b")),
    ("login or test-first path", re.compile(r"ui-auth\.json|WHYGRAPH_UI_|testfirst/")),
]
_FINDING_RE = re.compile(r"^-\s*F(\d+)\s*\|\s*(blocker|should)\s*\|\s*(.*)$")
_REVIEW_HEAD_RE = re.compile(r"^REVIEW:\s*(logic|ui|journey|dry-run)\s*(none)?\s*$")
_SIDECAR_BLOCK_RE = re.compile(r"^=== ([\w-]+) ===\s*$", re.M)
_FIXER_RE = re.compile(
    r"^-\s*F(\d+)\s*\((\w+)\):\s*(applied|refuted|asked|blocked|deferred)\b", re.M
)
_CHECK_RE = re.compile(r"^([A-Za-z][\w ]{0,30}):")
_EXIT_RE = re.compile(r"^Exit code (\d+)")
_TREE_RE = re.compile(r"^tree: (\S+)", re.M)
_PLAN_TOOL_RE = re.compile(
    r"plan-tool\.sh\s+([\w-]+)(?:\s+(\S+))?(?:\s+(\d+)(?=\s|$|[;&|\"']))?"
)


def _when(stamp: Any) -> datetime | None:
    """Parse an ISO timestamp, or return None."""
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None


def _blocks(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the content blocks of a row, a string content as one text block."""
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]


def _result_text(block: dict[str, Any]) -> str:
    """Return the text of a ``tool_result`` block."""
    result = block.get("content")
    if isinstance(result, list):
        return "".join(str(b.get("text", "")) for b in result if isinstance(b, dict))
    return str(result or "")


def _results(transcript: Transcript) -> dict[str, tuple[str, bool, str]]:
    """Map a tool_use id to its result text, error flag and timestamp."""
    found = {}
    for record in transcript.records:
        if record.get("type") != "user":
            continue
        for block in _blocks(record):
            if block.get("type") == "tool_result":
                found[str(block.get("tool_use_id"))] = (
                    _result_text(block),
                    bool(block.get("is_error")),
                    str(record.get("timestamp", "")),
                )
    return found


def _is_compaction(record: dict[str, Any]) -> bool:
    """Whether a row marks a compaction."""
    return record.get("subtype") == "compact_boundary" or bool(
        record.get("isCompactSummary")
    )


def _compactions(transcript: Transcript) -> int:
    """Count compaction events: boundary rows, else summary rows."""
    boundaries = sum(
        1 for r in transcript.records if r.get("subtype") == "compact_boundary"
    )
    return boundaries or sum(1 for r in transcript.records if _is_compaction(r))


def _is_human(record: dict[str, Any]) -> bool:
    """Whether a user row is a turn typed by the person."""
    if record.get("type") != "user" or record.get("isMeta") or _is_compaction(record):
        return False
    blocks = _blocks(record)
    return bool(blocks) and all(b.get("type") == "text" for b in blocks)


def _agent_key(transcript: Transcript) -> str:
    """Return the agent label, the plan reviewer split by its ``LENS:`` line."""
    if transcript.agent_type == "whygraph-plan-reviewer":
        found = re.search(r"^LENS:\s*(\w+)", transcript.first_prompt(), re.M)
        return f"plan-reviewer ({found.group(1) if found else 'unknown'})"
    return transcript.agent_type


def _split_tokens(transcript: Transcript) -> dict[str, int]:
    """Return input, output, cache-read and cache-creation tokens, once per message."""
    seen: set[str] = set()
    out = dict.fromkeys(("input", "output", "cache_read", "cache_creation"), 0)
    for record in transcript.assistant_records():
        message = record.get("message") or {}
        key = str(message.get("id") or id(record))
        if key in seen:
            continue
        seen.add(key)
        usage = message.get("usage") or {}
        out["input"] += int(usage.get("input_tokens") or 0)
        out["output"] += int(usage.get("output_tokens") or 0)
        out["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
        out["cache_creation"] += int(usage.get("cache_creation_input_tokens") or 0)
    return out


def _time_split(transcript: Transcript) -> dict[str, float]:
    """Split a transcript's seconds into model, tool and person time."""
    questions = {
        str(b.get("id"))
        for r in transcript.assistant_records()
        for b in _blocks(r)
        if b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion"
    }
    split = {"model": 0.0, "tool": 0.0, "person": 0.0}
    previous = None
    for record in transcript.records:
        now = _when(record.get("timestamp"))
        if now is None:
            continue
        if previous is not None:
            gap = max((now - previous).total_seconds(), 0.0)
            kind = record.get("type")
            if kind == "assistant":
                split["model"] += gap
            elif kind == "user" and not _is_compaction(record):
                ids = {
                    str(b.get("tool_use_id"))
                    for b in _blocks(record)
                    if b.get("type") == "tool_result"
                }
                if ids & questions or _is_human(record):
                    split["person"] += gap
                elif ids:
                    split["tool"] += gap
        previous = now
    return split


def _agents(transcripts: list[Transcript]) -> dict[str, dict[str, Any]]:
    """Build the per-agent block."""
    agents: dict[str, dict[str, Any]] = {}
    for t in transcripts:
        entry = agents.setdefault(
            _agent_key(t),
            {
                "runs": 0,
                "minutes": {"model": 0.0, "tool": 0.0, "person": 0.0},
                "tokens": dict.fromkeys(
                    ("input", "output", "cache_read", "cache_creation", "total"), 0
                ),
                "models": [],
                "compactions": 0,
            },
        )
        entry["runs"] += 1
        for k, seconds in _time_split(t).items():
            entry["minutes"][k] += seconds / 60
        for k, count in _split_tokens(t).items():
            entry["tokens"][k] += count
        entry["tokens"]["total"] += sum(_transcript_tokens(t).values())
        entry["models"] = sorted(set(entry["models"]) | t.models())
        entry["compactions"] += _compactions(t)
    for entry in agents.values():
        entry["minutes"] = {k: round(v, 2) for k, v in entry["minutes"].items()}
    return agents


def _stops(main: Transcript, transcripts: list[Transcript]) -> dict[str, Any]:
    """Build the Stops block."""
    turns = sum(1 for r in main.records if _is_human(r))
    questions = sum(
        1 for t in transcripts for u in t.tool_uses() if u.name == "AskUserQuestion"
    )
    causes = []
    for t in transcripts:
        for record in t.assistant_records():
            for block in _blocks(record):
                text = str(block.get("text", "")) if block.get("type") == "text" else ""
                if "## Run stopped" not in text:
                    continue
                found = re.search(r"\*\*Cause:\*\*\s*(.*)", text)
                causes.append((found.group(1) if found else "")[:120])
    return {
        "user_turns": max(turns - 1, 0),
        "questions": questions,
        "run_stopped": causes,
    }


def _plan_tool(transcripts: list[Transcript]) -> dict[str, Any]:
    """Build the Plan tool block."""
    calls: list[dict[str, Any]] = []
    seen: Counter[tuple[str, str, str, str]] = Counter()
    reruns: Counter[str] = Counter()
    for t in transcripts:
        results = _results(t)
        for record in t.assistant_records():
            for block in _blocks(record):
                command = str((block.get("input") or {}).get("command", ""))
                if block.get("type") != "tool_use" or "plan-tool.sh" not in command:
                    continue
                found = _PLAN_TOOL_RE.search(command)
                if not found:
                    continue
                sub = found.group(1)
                step = (found.group(3) or "") if sub in STEP_SUBS else ""
                text, failed, _ = results.get(str(block.get("id")), ("", False, ""))
                exit_match = _EXIT_RE.match(text)
                exit_code = int(exit_match.group(1)) if exit_match else int(failed)
                calls.append({"sub": sub, "step": step, "exit": exit_code})
                if sub in NO_CHANGE_SUBS and step:
                    tree = _TREE_RE.search(text)
                    digest = hashlib.sha256(text.encode()).hexdigest()
                    key = (sub, step, tree.group(1) if tree else "", digest)
                    if seen[key]:
                        reruns[step] += 1
                    seen[key] += 1
    by_sub = Counter(c["sub"] for c in calls)
    failed = sum(1 for c in calls if c["exit"])
    return {
        "calls": calls,
        "by_sub": dict(sorted(by_sub.items())),
        "failed": failed,
        "no_change_reruns": dict(sorted(reruns.items(), key=lambda i: int(i[0]))),
    }


def _review(transcripts: list[Transcript]) -> dict[str, Any]:
    """Build the Review block: counts per lens and per journey check."""
    lenses: dict[str, dict[str, Any]] = {}

    def lens_entry(name: str) -> dict[str, Any]:
        return lenses.setdefault(
            name,
            {
                "replies": 0,
                "findings": 0,
                "blocker": 0,
                "should": 0,
                **dict.fromkeys(REVIEW_KINDS, 0),
                "checks": {},
            },
        )

    # The review sidecar the planner writes holds every reply, one
    # "=== <lens> ===" block each; a background reviewer's own transcript may
    # end without its reply. Without a sidecar, read the reviewer transcripts.
    sidecars: dict[str, str] = {}
    for t in transcripts:
        for use in t.tool_uses():
            path = str(use.input.get("file_path", ""))
            if use.name == "Write" and path.endswith(".review.md"):
                sidecars[path] = str(use.input.get("content", ""))
    replies: list[list[str]] = []
    for text in sidecars.values():
        parts = _SIDECAR_BLOCK_RE.split(text)
        replies += [body.strip().splitlines() for body in parts[2::2]]
    if not sidecars:
        replies = [
            t.final_text().splitlines()
            for t in transcripts
            if t.agent_type == "whygraph-plan-reviewer"
        ]
    for lines in replies:
        head = next((m for m in map(_REVIEW_HEAD_RE.match, lines) if m), None)
        if head is None:
            continue
        entry = lens_entry(head.group(1))
        entry["replies"] += 1
        for line in lines:
            finding = _FINDING_RE.match(line.strip())
            if not finding:
                continue
            entry["findings"] += 1
            entry[finding.group(2)] += 1
            if head.group(1) == "journey":
                parts = [p.strip() for p in finding.group(3).split(" | ")]
                check = _CHECK_RE.match(parts[0])
                name = check.group(1) if check else "other"
                entry["checks"][name] = entry["checks"].get(name, 0) + 1
    fixed: dict[tuple[str, str], str] = {}
    for t in transcripts:
        for use in t.tool_uses():
            text = str(use.input.get("content") or use.input.get("new_string") or "")
            if "## Plan review" not in text:
                continue
            section = text.split("## Plan review", 1)[1]
            for found in _FIXER_RE.finditer(section):
                fixed[(found.group(2), found.group(1))] = found.group(3)
    for (lens, _), kind in fixed.items():
        lens_entry(lens)[kind] += 1
    return dict(sorted(lenses.items()))


def _friction(transcripts: list[Transcript]) -> dict[str, Any]:
    """Build the Friction block."""
    failed: Counter[str] = Counter()
    seen: set[str] = set()
    repeats = 0
    for t in transcripts:
        names = {
            str(b.get("id")): str(b.get("name"))
            for r in t.assistant_records()
            for b in _blocks(r)
            if b.get("type") == "tool_use"
        }
        for use_id, (_, is_error, _) in _results(t).items():
            if is_error:
                failed[names.get(use_id, "unknown")] += 1
        for record in t.assistant_records():
            for block in _blocks(record):
                text = str(block.get("text", "")) if block.get("type") == "text" else ""
                if len(text) < 200:
                    continue
                digest = hashlib.sha256(text.encode()).hexdigest()
                repeats += digest in seen
                seen.add(digest)
    return {"failed_calls": dict(sorted(failed.items())), "repeated_outputs": repeats}


def _flags(transcripts: list[Transcript]) -> list[dict[str, str]]:
    """Build the Flags block from Bash commands."""
    flags = []
    for t in transcripts:
        for use in t.tool_uses():
            if use.name != "Bash":
                continue
            command = str(use.input.get("command", ""))
            for kind, pattern in FLAG_PATTERNS:
                if pattern.search(command):
                    flags.append(
                        {
                            "agent": _agent_key(t),
                            "kind": kind,
                            "command": " ".join(command.split())[:120],
                        }
                    )
    return flags


def build_record(transcripts: list[Transcript], session: str) -> dict[str, Any]:
    """Build the audit record of one session.

    Parameters
    ----------
    transcripts : list of Transcript
        The main transcript first, then the subagents.
    session : str
        The session id.

    Returns
    -------
    dict
        A JSON-serializable record: counts, times and pointers, no text.
    """
    main = transcripts[0]
    stamps = sorted(
        s for t in transcripts for r in t.records if (s := _when(r.get("timestamp")))
    )
    version = next((str(r["version"]) for r in main.records if r.get("version")), "")
    plan_tool = _plan_tool(transcripts)
    steps = {c["step"] for c in plan_tool["calls"] if c["step"]}
    return {
        "session": session,
        "steps": len(steps),
        "run": {
            "start": stamps[0].isoformat() if stamps else "",
            "end": stamps[-1].isoformat() if stamps else "",
            "wall_minutes": round((stamps[-1] - stamps[0]).total_seconds() / 60, 2)
            if stamps
            else 0.0,
            "version": version,
        },
        "agents": _agents(transcripts),
        "stops": _stops(main, transcripts),
        "plan_tool": plan_tool,
        "review": _review(transcripts),
        "friction": _friction(transcripts),
        "flags": _flags(transcripts),
    }


def render(record: dict[str, Any]) -> str:
    """Render a record as the text report."""
    out = ["== Run =="]
    run = record["run"]
    out += [
        f"session: {record['session']}",
        f"start: {run['start']}",
        f"end: {run['end']}",
        f"wall minutes: {run['wall_minutes']}",
        f"claude code version: {run['version'] or 'unknown'}",
        f"plan steps seen: {record['steps']}",
        "",
        "== Agents ==",
    ]
    for name, a in record["agents"].items():
        m, tk = a["minutes"], a["tokens"]
        out.append(
            f"{name}: runs {a['runs']}, minutes model {m['model']} "
            f"tool {m['tool']} person {m['person']}, "
            f"models {', '.join(a['models']) or 'none'}, compactions {a['compactions']}"
        )
        out.append(
            f"  tokens: input {tk['input']}, output {tk['output']}, "
            f"cache-read {tk['cache_read']}, cache-creation {tk['cache_creation']}, "
            f"total {tk['total']}"
        )
    total = sum(a["tokens"]["total"] for a in record["agents"].values())
    out += [f"transcript tokens: lower bound, total {total}", "", "== Stops =="]
    s = record["stops"]
    out += [
        f"user turns after the first: {s['user_turns']}",
        f"question dialogs: {s['questions']}",
        f"run stopped blocks: {len(s['run_stopped'])}",
    ]
    out += [f"  cause: {c}" for c in s["run_stopped"]]
    p = record["plan_tool"]
    out += ["", "== Plan tool ==", f"calls: {len(p['calls'])}, failed: {p['failed']}"]
    out += [
        f"  {c['sub']} {c['step']}".rstrip() + f" exit {c['exit']}" for c in p["calls"]
    ]
    for step, k in p["no_change_reruns"].items():
        out.append(f"no-change re-runs: step {step} {k}")
    out += ["", "== Review =="]
    for lens, r in record["review"].items():
        out.append(
            f"{lens}: replies {r['replies']}, findings {r['findings']} "
            f"(blocker {r['blocker']}, should {r['should']}); "
            + ", ".join(f"{k} {r[k]}" for k in REVIEW_KINDS)
        )
        out += [f"  check {c}: {n}" for c, n in sorted(r["checks"].items())]
    out += ["", "== Friction =="]
    f = record["friction"]
    out += [f"failed {tool}: {n}" for tool, n in f["failed_calls"].items()]
    out.append(f"repeated outputs: {f['repeated_outputs']}")
    out += ["", "== Flags =="]
    out += [f"[{x['kind']}] {x['agent']}: {x['command']}" for x in record["flags"]]
    if not record["flags"]:
        out.append("none")
    return "\n".join(out) + "\n"


def _flatten(record: dict[str, Any]) -> dict[str, list[tuple[str, float]]]:
    """Flatten a record to numbers per block, for ``--compare``."""
    blocks: dict[str, list[tuple[str, float]]] = {}
    blocks["Run"] = [("wall minutes", record["run"]["wall_minutes"])]
    rows: list[tuple[str, float]] = []
    for name, a in record["agents"].items():
        rows.append((f"{name} runs", a["runs"]))
        rows += [(f"{name} {k} minutes", v) for k, v in a["minutes"].items()]
        rows += [(f"{name} {k} tokens", v) for k, v in a["tokens"].items()]
        rows.append((f"{name} compactions", a["compactions"]))
    blocks["Agents"] = rows
    s = record["stops"]
    blocks["Stops"] = [
        ("user turns", s["user_turns"]),
        ("question dialogs", s["questions"]),
        ("run stopped", len(s["run_stopped"])),
    ]
    p = record["plan_tool"]
    blocks["Plan tool"] = (
        [("calls", len(p["calls"])), ("failed", p["failed"])]
        + [(f"{sub} calls", n) for sub, n in p["by_sub"].items()]
        + [("no-change re-runs", sum(p["no_change_reruns"].values()))]
    )
    rows = []
    for lens, r in record["review"].items():
        rows += [(f"{lens} findings", r["findings"])]
        rows += [(f"{lens} {k}", r[k]) for k in REVIEW_KINDS]
        rows += [(f"{lens} check {c}", n) for c, n in sorted(r["checks"].items())]
    blocks["Review"] = rows
    f = record["friction"]
    blocks["Friction"] = [
        (f"failed {tool}", n) for tool, n in f["failed_calls"].items()
    ] + [("repeated outputs", f["repeated_outputs"])]
    kinds = Counter(x["kind"] for x in record["flags"])
    blocks["Flags"] = sorted(kinds.items())
    return blocks


def _num(value: float) -> str:
    """Format a count or minutes value."""
    return (
        f"{value:g}"
        if isinstance(value, int)
        else f"{value:.2f}".rstrip("0").rstrip(".")
    )


def compare(a: dict[str, Any], b: dict[str, Any]) -> str:
    """Render two records side by side, each count also per plan step.

    Parameters
    ----------
    a, b : dict
        Records written by ``audit.py --json``.

    Returns
    -------
    str
        One block per block of the report, with no judgement.
    """
    out = [
        f"A: {a['session']} ({a['steps']} steps)",
        f"B: {b['session']} ({b['steps']} steps)",
    ]
    flat_a, flat_b = _flatten(a), _flatten(b)

    def per_step(value: float, steps: int) -> str:
        return _num(round(value / steps, 2)) if steps else "-"

    for block in flat_a:
        labels = [label for label, _ in flat_a[block]]
        labels += [x for x, _ in flat_b[block] if x not in labels]
        out += [
            "",
            f"== {block} ==",
            f"{'':44} {'A':>9} {'A/step':>8} {'B':>9} {'B/step':>8}",
        ]
        da, db = dict(flat_a[block]), dict(flat_b[block])
        for label in labels:
            x, y = da.get(label, 0), db.get(label, 0)
            out.append(
                f"{label:44} {_num(x):>9} {per_step(x, a['steps']):>8} "
                f"{_num(y):>9} {per_step(y, b['steps']):>8}"
            )
    out += ["", "transcript tokens: lower bound"]
    return "\n".join(out) + "\n"


def load_record(target: str) -> dict[str, Any]:
    """Audit a session by id or by the path of its main ``.jsonl``."""
    path = Path(target)
    if path.suffix == ".jsonl" and path.exists():
        if path.resolve().parent.parent.name != "projects":
            raise SystemExit(
                "audit.py: a session file must sit at <config>/projects/<dir>/<id>.jsonl"
            )
        os.environ["CLAUDE_CONFIG_DIR"] = str(path.resolve().parent.parent.parent)
        target = path.stem
    transcripts = load_transcripts(target)
    if not transcripts:
        raise SystemExit(f"audit.py: no transcripts for session {target!r}")
    return build_record(transcripts, target)


def self_check() -> int:
    """Compare the fixture output with ``expected.txt``; return an exit code."""
    os.environ["CLAUDE_CONFIG_DIR"] = str(FIXTURE)
    record = build_record(load_transcripts("session"), "session")
    before = json.loads((FIXTURE / "before.json").read_text(encoding="utf-8"))
    pairs = [
        ("expected.txt", render(record)),
        ("expected-compare.txt", compare(before, record)),
    ]
    failed = False
    for name, actual in pairs:
        expected = (FIXTURE / name).read_text(encoding="utf-8")
        if actual != expected:
            failed = True
            sys.stdout.writelines(
                difflib.unified_diff(
                    expected.splitlines(True),
                    actual.splitlines(True),
                    f"{name} (expected)",
                    f"{name} (actual)",
                )
            )
    print("audit self-check: " + ("FAILED" if failed else "ok"))
    return int(failed)


def main(argv: list[str] | None = None) -> int:
    """Run the command line."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("session", nargs="?", help="session id or path to a .jsonl")
    parser.add_argument("--json", action="store_true", help="print a JSON record")
    parser.add_argument("--compare", nargs=2, metavar=("A", "B"), help="two records")
    parser.add_argument("--self-check", action="store_true", help="check the fixture")
    args = parser.parse_args(argv)
    if args.self_check:
        return self_check()
    if args.compare:
        records = [
            json.loads(Path(p).read_text(encoding="utf-8")) for p in args.compare
        ]
        sys.stdout.write(compare(*records))
        return 0
    if not args.session:
        parser.error("give a session id or path, --compare A B, or --self-check")
    record = load_record(args.session)
    if args.json:
        print(json.dumps(record, indent=2))
    else:
        sys.stdout.write(render(record))
    return 0


if __name__ == "__main__":
    sys.exit(main())
