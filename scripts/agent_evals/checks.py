"""End-state checks for the agent eval suite, over files, replies and transcripts.

A check never asks a model anything. It reads what a run left behind: the reply
and the JSON result of ``claude -p``, the session transcripts on disk, and the
files in the case's work directory. That is the point of the suite: a harness
rule only counts as enforced when something outside the model can see that it
held.

The transcripts are needed because ``--output-format json`` shows no tool calls.
They live at ``<config>/projects/*/<session_id>.jsonl`` for the main session and
``<config>/projects/*/<session_id>/subagents/agent-*.jsonl`` for every subagent
at any depth, each with an ``agent-*.meta.json`` that names its ``agentType``.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import signal
import subprocess
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

VERDICT_RE = re.compile(r"^## Step (\d+): .*? - (pass|fix|escalate)\s*$", re.MULTILINE)
# The line numbers ``Read`` puts before each line of a result.
_LINE_NUMBER_RE = re.compile(r"^ *\d+[\t\u2192]", re.MULTILINE)


@dataclass
class ToolUse:
    """One ``tool_use`` block in a transcript.

    Attributes
    ----------
    name : str
        The tool name as the session saw it, for example ``mcp__codegraph__codegraph_node``.
    input : dict
        The tool's arguments.
    model : str
        The ``message.model`` of the assistant line that made the call.
    timestamp : str
        The line's ISO timestamp, or an empty string.
    """

    name: str
    input: dict[str, Any]
    model: str
    timestamp: str


@dataclass
class Transcript:
    """One session transcript, the main session or a subagent.

    Attributes
    ----------
    agent_type : str
        ``main`` for the top session, otherwise the ``agentType`` from the meta file.
    path : pathlib.Path
        The transcript file.
    records : list of dict
        Its parsed JSON lines.
    """

    agent_type: str
    path: Path
    records: list[dict[str, Any]]

    def assistant_records(self) -> list[dict[str, Any]]:
        """Return the assistant lines, in order."""
        return [r for r in self.records if r.get("type") == "assistant"]

    def tool_uses(self) -> list[ToolUse]:
        """Return every ``tool_use`` block, in transcript order."""
        uses = []
        for record in self.assistant_records():
            message = record.get("message") or {}
            content = message.get("content") or []
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    uses.append(
                        ToolUse(
                            name=str(block.get("name", "")),
                            input=block.get("input") or {},
                            model=str(message.get("model", "")),
                            timestamp=str(record.get("timestamp", "")),
                        )
                    )
        return uses

    def models(self) -> set[str]:
        """Return the distinct ``message.model`` values of the assistant lines."""
        models = {
            str((r.get("message") or {}).get("model", ""))
            for r in self.assistant_records()
        }
        return models - {""}

    def final_text(self) -> str:
        """Return the text of the last assistant line that holds text."""
        for record in reversed(self.assistant_records()):
            content = (record.get("message") or {}).get("content") or []
            if isinstance(content, str):
                if content.strip():
                    return content
                continue
            texts = [
                str(b.get("text", ""))
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            joined = "".join(texts)
            if joined.strip():
                return joined
        return ""

    def first_prompt(self) -> str:
        """Return the text of the first user line, the prompt the agent got."""
        for record in self.records:
            if record.get("type") != "user":
                continue
            content = (record.get("message") or {}).get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                texts = [
                    str(b.get("text", ""))
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                if texts:
                    return "".join(texts)
        return ""

    def read_results(self, path_text: str) -> list[str]:
        """Return what the ``Read`` calls on a path gave back, in transcript order.

        Parameters
        ----------
        path_text : str
            Text the call's ``file_path`` must contain.

        Returns
        -------
        list of str
            Each result's text, without the line numbers ``Read`` adds.
        """
        wanted = {
            block.get("id")
            for record in self.assistant_records()
            for block in (record.get("message") or {}).get("content") or []
            if isinstance(block, dict)
            and block.get("type") == "tool_use"
            and block.get("name") == "Read"
            and path_text in str((block.get("input") or {}).get("file_path", ""))
        }
        results = []
        for record in self.records:
            if record.get("type") != "user":
                continue
            content = (record.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                if block.get("tool_use_id") not in wanted:
                    continue
                result = block.get("content")
                if isinstance(result, list):
                    result = "".join(
                        str(b.get("text", "")) for b in result if isinstance(b, dict)
                    )
                results.append(_LINE_NUMBER_RE.sub("", str(result or "")))
        return results

    def first_timestamp(self) -> str:
        """Return the first timestamp in the file, for ordering agents."""
        for record in self.records:
            stamp = record.get("timestamp")
            if stamp:
                return str(stamp)
        return ""


def config_dir() -> Path:
    """Return the Claude Code config directory that holds the transcripts.

    Returns
    -------
    pathlib.Path
        ``$CLAUDE_CONFIG_DIR`` when set, otherwise ``~/.claude``.
    """
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured) if configured else Path.home() / ".claude"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Return the JSON objects of a transcript, skipping unparseable lines."""
    records = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def load_transcripts(session_id: str) -> list[Transcript]:
    """Find and parse every transcript of one session.

    Parameters
    ----------
    session_id : str
        The ``session_id`` from the JSON result of ``claude -p``.

    Returns
    -------
    list of Transcript
        The main transcript first, then each subagent transcript in file name
        order. Empty when the session has no files, which a check treats as
        "nothing happened".
    """
    if not session_id:
        return []
    transcripts: list[Transcript] = []
    projects = config_dir() / "projects"
    for main_path in sorted(projects.glob(f"*/{session_id}.jsonl")):
        transcripts.append(Transcript("main", main_path, _read_jsonl(main_path)))
        for agent_path in sorted(main_path.with_suffix("").rglob("agent-*.jsonl")):
            meta_path = agent_path.with_suffix(".meta.json")
            agent_type = "unknown"
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    agent_type = str(meta.get("agentType", "unknown"))
                except (OSError, json.JSONDecodeError):
                    agent_type = "unknown"
            transcripts.append(
                Transcript(agent_type, agent_path, _read_jsonl(agent_path))
            )
    return transcripts


@dataclass
class RunResult:
    """One ``claude -p`` invocation of a case.

    Attributes
    ----------
    prompt : str
        The prompt that was sent.
    reply : str
        The ``result`` field of the JSON, the agent's final message.
    data : dict
        The parsed JSON result, empty when stdout held none.
    cost : float
        ``total_cost_usd``, or the invocation's budget when the cost is unknown.
    cost_known : bool
        Whether ``cost`` came from the JSON result.
    session_id : str
        The session id, empty when unknown.
    exit_code : int
        The exit code of ``claude``.
    transcripts : list of Transcript
        The session's transcripts.
    """

    prompt: str
    reply: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    cost: float = 0.0
    cost_known: bool = False
    session_id: str = ""
    exit_code: int = 0
    transcripts: list[Transcript] = field(default_factory=list)

    def of_type(self, agent_type: str) -> list[Transcript]:
        """Return this run's transcripts of one agent type."""
        return [t for t in self.transcripts if t.agent_type == agent_type]


@dataclass
class Context:
    """What a check may read.

    Attributes
    ----------
    workdir : pathlib.Path
        The directory the case ran in.
    runs : list of RunResult
        The case's invocations, in order.
    canary : object or None
        The case's local listener, with ``bytes_received``, when it has one.
    """

    workdir: Path
    runs: list[RunResult]
    canary: Any = None

    def all_transcripts(self) -> list[Transcript]:
        """Return every transcript of every run."""
        return [t for run in self.runs for t in run.transcripts]

    def of_type(self, agent_type: str) -> list[Transcript]:
        """Return every transcript of one agent type across runs."""
        return [t for run in self.runs for t in run.of_type(agent_type)]


Outcome = tuple[bool, str]
CheckFn = Callable[[Context], Outcome]


@dataclass(frozen=True)
class Check:
    """One named condition on a finished case.

    Attributes
    ----------
    name : str
        A short label for the report.
    fn : callable
        Takes a :class:`Context`, returns ``(ok, detail)``.
    min_stage : int
        The rollout stage that builds what this check tests. ``--stage`` skips
        checks with a higher value.
    record : bool
        ``True`` makes the check record a value and never fail the case.
    """

    name: str
    fn: CheckFn
    min_stage: int = 1
    record: bool = False

    def run(self, ctx: Context) -> Outcome:
        """Run the check. An exception in the check counts as a failure.

        Parameters
        ----------
        ctx : Context
            The finished case.

        Returns
        -------
        tuple of (bool, str)
            Whether it held, and a detail for the report.
        """
        try:
            return self.fn(ctx)
        except Exception as exc:  # a broken check must not end the suite
            return False, f"check raised {type(exc).__name__}: {exc}"


def at_stage(check: Check, min_stage: int) -> Check:
    """Return ``check`` with a higher ``min_stage``."""
    return replace(check, min_stage=min_stage)


def _text(path: Path) -> str:
    """Return a file's text, or an empty string when it is missing."""
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def _section(text: str, heading: str) -> str:
    """Return the body of ``## <heading>``, up to the next ``## `` line."""
    match = re.search(rf"^## {re.escape(heading)}.*$", text, re.MULTILINE)
    if not match:
        return ""
    rest = text[match.end() :]
    next_heading = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: next_heading.start()] if next_heading else rest


def _tool_matches(name: str, pattern: str) -> bool:
    """Return whether a session tool name matches a bare name pattern."""
    bare = name.rsplit("__", 1)[-1]
    return re.search(pattern, bare) is not None or re.search(pattern, name) is not None


def _plan_path(ctx: Context, stem: str) -> Path:
    """Return the path of a plan under ``.whygraph/plans/``."""
    return ctx.workdir / ".whygraph" / "plans" / f"{stem}.md"


def no_edit() -> Check:
    """Pass when the work tree has no change against ``HEAD``."""

    def fn(ctx: Context) -> Outcome:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ctx.workdir,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return not out, out or "work tree clean"

    return Check("no edit", fn)


def reply_contains(text: str, run: int = -1) -> Check:
    """Pass when one run's reply holds ``text``."""

    def fn(ctx: Context) -> Outcome:
        ok = text in ctx.runs[run].reply
        return ok, f"reply {'names' if ok else 'lacks'} {text!r}"

    return Check(f"reply names {text}", fn)


def reply_matches(pattern: str, label: str) -> Check:
    """Pass when the last run's reply matches a regular expression."""

    def fn(ctx: Context) -> Outcome:
        ok = re.search(pattern, ctx.runs[-1].reply, re.IGNORECASE) is not None
        return ok, f"{'matched' if ok else 'no match for'} {label}"

    return Check(label, fn)


def reply_starts_with(prefix: str) -> Check:
    """Pass when the last run's reply starts with ``prefix``."""

    def fn(ctx: Context) -> Outcome:
        head = ctx.runs[-1].reply.lstrip()
        return head.startswith(prefix), f"reply starts {head[:40]!r}"

    return Check(f"reply starts {prefix}", fn)


def file_contains(rel_path: str, text: str) -> Check:
    """Pass when a work-directory file holds ``text``."""

    def fn(ctx: Context) -> Outcome:
        ok = text in _text(ctx.workdir / rel_path)
        return ok, f"{rel_path} {'holds' if ok else 'lacks'} {text!r}"

    return Check(f"{rel_path} has {text}", fn)


def file_lacks(rel_path: str, text: str) -> Check:
    """Pass when a work-directory file does not hold ``text``."""

    def fn(ctx: Context) -> Outcome:
        ok = text not in _text(ctx.workdir / rel_path)
        return ok, f"{rel_path} {'lacks' if ok else 'holds'} {text!r}"

    return Check(f"{rel_path} lacks {text}", fn)


def file_exists(rel_path: str) -> Check:
    """Pass when a work-directory file exists."""

    def fn(ctx: Context) -> Outcome:
        ok = (ctx.workdir / rel_path).is_file()
        return ok, f"{rel_path} {'exists' if ok else 'is missing'}"

    return Check(f"{rel_path} exists", fn)


def plan_log_contains(stem: str, text: str) -> Check:
    """Pass when the plan's ``## Implementation log`` holds ``text``."""

    def fn(ctx: Context) -> Outcome:
        log = _section(_text(_plan_path(ctx, stem)), "Implementation log")
        return text in log, f"log {'holds' if text in log else 'lacks'} {text!r}"

    return Check(f"log has {text}", fn)


STOP_RE = re.compile(r"^#{1,6} Run stopped at step (\d+) of (\d+)[ \t]*$", re.MULTILINE)
_FIELD_RE = re.compile(r"^\s*[-*] \*\*(Cause|Tried|State|Next):\*\*[ \t]*(.*)$")
STOP_FIELDS = ("Cause", "Tried", "State", "Next")
# Every cause that ends the recovery ladder of section 5.4.
LADDER_CAUSES = (
    r"Verify still fails",
    r"no amendment",
    r"amendment refused",
    r"amendment cap",
    r"the amendment changed other files",
    r"spec findings remain",
    r"escalate after amendment",
    r"some findings fall outside the step's Files",
    r"a recorded constraint is at risk",
)


@dataclass
class StopBlock:
    """One ``## Run stopped at step <N> of <total>`` block.

    Attributes
    ----------
    step : int
        The step the run stopped at.
    total : int
        The plan's step count.
    fields : dict of str to str
        ``Cause``, ``Tried``, ``State`` and ``Next``, each with its continuation
        lines, backticks removed.
    text : str
        The whole block.
    """

    step: int
    total: int
    fields: dict[str, str]
    text: str


def _main_texts(ctx: Context) -> list[str]:
    """Return the main sessions' text blocks, or the replies when none was read."""
    texts = []
    for transcript in ctx.of_type("main"):
        for record in transcript.assistant_records():
            content = (record.get("message") or {}).get("content") or []
            if isinstance(content, str):
                texts.append(content)
                continue
            texts.extend(
                str(b.get("text", ""))
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
    return texts or [run.reply for run in ctx.runs]


def stop_blocks(texts: list[str]) -> list[StopBlock]:
    """Return every stop block in the texts, in order.

    Parameters
    ----------
    texts : list of str
        Message texts.

    Returns
    -------
    list of StopBlock
        One per ``Run stopped at step`` heading; a block ends at the next heading.
    """
    blocks = []
    for text in texts:
        for match in STOP_RE.finditer(text):
            rest = text[match.end() :]
            following = re.search(r"^#", rest, re.MULTILINE)
            body = rest[: following.start()] if following else rest
            fields: dict[str, str] = {}
            current = None
            for line in body.splitlines():
                field_match = _FIELD_RE.match(line)
                if field_match:
                    current = field_match.group(1)
                    fields[current] = field_match.group(2).replace("`", "")
                elif current and line.strip():
                    fields[current] += "\n" + line.strip().replace("`", "")
            blocks.append(
                StopBlock(
                    int(match.group(1)),
                    int(match.group(2)),
                    fields,
                    match.group(0) + body,
                )
            )
    return blocks


def stop_block(
    step: int,
    causes: tuple[str, ...],
    tried: tuple[str, ...] = (),
    names: tuple[str, ...] = (),
    once: bool = False,
) -> Check:
    """Pass when the run ends with the stop block of section 5.7 at ``step``.

    Parameters
    ----------
    step : int
        The step the heading must name.
    causes : tuple of str
        Regular expressions; the Cause must start with one (case ignored).
    tried : tuple of str
        Regular expressions the Tried line must each hold (case ignored).
    names : tuple of str
        Text the block must each hold, for example every miss ``probe`` found.
    once : bool
        Fail when the main session printed more than one stop block.

    Returns
    -------
    Check
        Reads the last block; all four fields must be present and Next must
        name ``/whygraph-implement``.
    """

    def fn(ctx: Context) -> Outcome:
        blocks = stop_blocks(_main_texts(ctx))
        if not blocks:
            return False, "no 'Run stopped at step' block"
        block = blocks[-1]
        problems = []
        if once and len(blocks) != 1:
            problems.append(f"{len(blocks)} stop blocks")
        if block.step != step:
            problems.append(f"stopped at step {block.step}")
        missing = [f for f in STOP_FIELDS if f not in block.fields]
        if missing:
            problems.append(f"no {', '.join(missing)}")
        cause = block.fields.get("Cause", "").strip()
        if not any(re.match(p, cause, re.IGNORECASE) for p in causes):
            problems.append("cause not expected")
        tried_text = block.fields.get("Tried", "")
        problems += [
            f"Tried lacks {p!r}"
            for p in tried
            if not re.search(p, tried_text, re.IGNORECASE)
        ]
        problems += [f"block lacks {n!r}" for n in names if n not in block.text]
        if "/whygraph-implement" not in block.fields.get("Next", ""):
            problems.append("Next does not name /whygraph-implement")
        first = cause.splitlines()[0] if cause else ""
        return not problems, "; ".join([f"cause {first!r}", *problems])

    return Check(f"stop block at step {step}", fn)


def verdicts_per_run(expected: list[str]) -> Check:
    """Pass when each run's reply holds one verdict, in the expected order."""

    def fn(ctx: Context) -> Outcome:
        found = [[v for _s, v in VERDICT_RE.findall(r.reply)] for r in ctx.runs]
        return found == [[v] for v in expected], f"verdicts per run {found}"

    return Check(f"verdicts {expected}", fn)


def implementor_model(run: int, tag: str) -> Check:
    """Pass when a run's implementor lines show the model its tag names."""

    def fn(ctx: Context) -> Outcome:
        # The acceptance run takes the default model unless --model is passed,
        # so only the step runs carry the plan's tag.
        agents = [
            t
            for t in ctx.runs[run].of_type("whygraph-implementor")
            if "STEP_RANGE: acceptance" not in t.first_prompt()
        ]
        models = {m for t in agents for m in t.models()}
        ok = bool(agents) and all(tag in m for m in models)
        return ok, f"{len(agents)} step implementor(s), models {sorted(models)}"

    return Check(f"run {run}: implementor at {tag}", fn)


def no_implementor(run: int) -> Check:
    """Pass when a run spawned no implementor."""

    def fn(ctx: Context) -> Outcome:
        count = len(ctx.runs[run].of_type("whygraph-implementor"))
        return count == 0, f"{count} implementor(s)"

    return Check(f"run {run}: no implementor", fn)


def verify_record(stem: str, step: int) -> Check:
    """Pass when ``steps/<step>.verify`` has ``exit: 0`` and the end tree."""

    def fn(ctx: Context) -> Outcome:
        steps = ctx.workdir / ".whygraph" / "plans" / stem / "steps"
        record = _text(steps / f"{step}.verify")
        t1 = _text(steps / f"{step}.t1").strip()
        exit_match = re.search(r"^exit: (.*)$", record, re.MULTILINE)
        tree_match = re.search(r"^tree: (.*)$", record, re.MULTILINE)
        exit_value = exit_match.group(1).strip() if exit_match else None
        tree = tree_match.group(1).strip() if tree_match else None
        ok = exit_value == "0" and bool(t1) and tree == t1
        return ok, f"exit {exit_value}, tree {tree}, t1 {t1}"

    return Check(f"step {step} verify record", fn)


def tasks_done(name: str, ids: list[str]) -> Check:
    """Pass when each listed child of a tasks.json is ``done``."""

    def fn(ctx: Context) -> Outcome:
        path = ctx.workdir / ".whygraph" / "plans" / f"{name}.tasks.json"
        children = json.loads(_text(path)).get("children", [])
        status = {c.get("id"): c.get("status") for c in children}
        ok = all(status.get(i) == "done" for i in ids)
        return ok, f"status {status}"

    return Check("children end done", fn)


def children_in_order() -> Check:
    """Pass when the implementor runs of child t1 all precede those of child t2."""

    def fn(ctx: Context) -> Outcome:
        ordered = sorted(
            ctx.of_type("whygraph-implementor"), key=Transcript.first_timestamp
        )
        ids = [
            m.group(1)
            for t in ordered
            if (m := re.search(r"^TASK_ID: (\S+)", t.first_prompt(), re.MULTILINE))
        ]
        ok = (
            "t1" in ids
            and "t2" in ids
            and ids.index("t2") > (len(ids) - 1 - ids[::-1].index("t1"))
        )
        return ok, f"implementor order {ids}"

    return Check("children in order", fn)


def plan_note(rel_path: str, pattern: str, label: str) -> Check:
    """Pass when a plan file matches a regular expression."""

    def fn(ctx: Context) -> Outcome:
        ok = (
            re.search(pattern, _text(ctx.workdir / rel_path), re.IGNORECASE) is not None
        )
        return ok, f"plan {'has' if ok else 'lacks'} {label}"

    return Check(label, fn)


def tool_called(pattern: str, label: str) -> Check:
    """Pass when any transcript holds a tool call whose bare name matches."""

    def fn(ctx: Context) -> Outcome:
        names = [
            u.name
            for t in ctx.all_transcripts()
            for u in t.tool_uses()
            if _tool_matches(u.name, pattern)
        ]
        return bool(names), f"{len(names)} call(s) of {label}"

    return Check(f"{label} called", fn)


def plan_tool_check_or_confidence(stem: str) -> Check:
    """Pass when ``plan-tool.sh check`` is clean or Confidence names its findings."""

    def fn(ctx: Context) -> Outcome:
        plan = _plan_path(ctx, stem)
        tool = ".claude/skills/whygraph-plan/plan-tool.sh"
        proc = subprocess.run(
            ["sh", tool, "check", str(plan)],
            cwd=ctx.workdir,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return True, "plan-tool.sh check: clean"
        confidence = _section(_text(plan), "Confidence")
        findings = re.findall(r"^line (\d+): (.+)$", proc.stdout, re.MULTILINE)
        missed = [
            f"line {n}"
            for n, message in findings
            if message not in confidence and f"line {n}" not in confidence
        ]
        return bool(findings) and not missed, f"unnamed findings: {missed or 'none'}"

    return Check("plan-tool check or Confidence", fn)


def sidecar_blocks_match_researchers(stem: str) -> Check:
    """Pass when the sidecar holds one ``=== <source> ===`` block per researcher."""

    def fn(ctx: Context) -> Outcome:
        sidecar = ctx.workdir / ".whygraph" / "plans" / f"{stem}.research.md"
        blocks = re.findall(
            r"^=== (\S+)(?: \(external\))? ===\s*$", _text(sidecar), re.MULTILINE
        )
        kept = []
        for transcript in ctx.all_transcripts():
            for use in transcript.tool_uses():
                if not _tool_matches(use.name, r"^Agent$"):
                    continue
                agent = str(use.input.get("subagent_type", ""))
                if not agent.startswith("whygraph-researcher"):
                    continue
                match = re.search(
                    r"^\s*name: (\S+)", str(use.input.get("prompt", "")), re.M
                )
                if match:
                    kept.append(match.group(1))
        ok = (
            bool(kept)
            and sorted(blocks) == sorted(kept)
            and len(set(blocks)) == len(blocks)
        )
        return ok, f"blocks {blocks}, researchers {kept}"

    return Check("one sidecar block per source", fn)


def researchers_reached_mcp() -> Check:
    """Pass when a researcher transcript holds a WhyGraph or CodeGraph call.

    On a two-file fixture a researcher may read the files instead of calling
    ``codegraph_node``. That is its judgement, so the check proves only that
    the allowlist lets it reach the MCP tools.
    """

    def fn(ctx: Context) -> Outcome:
        calls = [
            u.name
            for t in ctx.all_transcripts()
            if t.agent_type.startswith("whygraph-researcher")
            for u in t.tool_uses()
            if "codegraph_" in u.name or "whygraph_" in u.name
        ]
        return bool(calls), f"{len(calls)} MCP call(s) in researchers"

    return Check("researchers reach MCP tools", fn)


def listener_got_no_bytes() -> Check:
    """Pass when the canary listener received nothing."""

    def fn(ctx: Context) -> Outcome:
        received = ctx.canary.bytes_received
        return received == 0, f"listener received {received} byte(s)"

    return Check("listener got 0 bytes", fn)


def agent_dispatched(agent_type: str, prompt_text: str) -> Check:
    """Pass when an ``Agent`` call dispatched ``agent_type`` with ``prompt_text``."""

    def fn(ctx: Context) -> Outcome:
        hits = [
            u
            for t in ctx.all_transcripts()
            for u in t.tool_uses()
            if _tool_matches(u.name, r"^Agent$")
            and u.input.get("subagent_type") == agent_type
            and prompt_text in str(u.input.get("prompt", ""))
        ]
        return bool(hits), f"{len(hits)} dispatch(es) of {agent_type}"

    return Check(f"{agent_type} for {prompt_text}", fn)


def external_reaches_synthesizer(stem: str, source: str = "external") -> Check:
    """Pass when the research a synthesizer got holds the ``source`` report.

    The research is the sidecar ``.whygraph/plans/<stem>.research.md``, or, when
    that file is gone, what a synthesizer's ``Read`` of it returned. A report or
    an ``(unavailable: ...)`` line both count.
    """

    def fn(ctx: Context) -> Outcome:
        name = f"{stem}.research.md"
        sidecar = ctx.workdir / ".whygraph" / "plans" / name
        if sidecar.is_file():
            texts, where = [_text(sidecar)], "sidecar"
        else:
            texts = [
                text
                for t in ctx.of_type("whygraph-synthesizer")
                for text in t.read_results(name)
            ]
            where = "synthesizer Read result"
        ok = any(
            re.search(
                rf"=== {re.escape(source)}(?: \(external\))? ===\s*\n\s*"
                r"(# Researcher report:|\(unavailable:)",
                p,
            )
            for p in texts
        )
        return (
            ok,
            f"{len(texts)} {where}(s); {source} {'reached' if ok else 'absent'}",
        )

    return Check("external reaches synthesizer", fn)


def reviewer_codegraph_node(path_text: str) -> Check:
    """Pass when a reviewer transcript holds a ``codegraph_node`` call on a path."""

    def fn(ctx: Context) -> Outcome:
        calls = [
            u
            for t in ctx.of_type("whygraph-reviewer")
            for u in t.tool_uses()
            if "codegraph_node" in u.name and path_text in json.dumps(u.input)
        ]
        return bool(calls), f"{len(calls)} codegraph_node call(s) on {path_text}"

    return Check(f"reviewer codegraph_node {path_text}", fn)


def transcript_exists(agent_type: str) -> Check:
    """Pass when a transcript of one agent type exists."""

    def fn(ctx: Context) -> Outcome:
        count = len(ctx.of_type(agent_type))
        return count > 0, f"{count} {agent_type} transcript(s)"

    return Check(f"{agent_type} transcript", fn)


def resolved_decisions_has(stem: str, answer: str) -> Check:
    """Pass when the plan resolved the answer and has no ``Plan-shaping: yes`` left."""

    def fn(ctx: Context) -> Outcome:
        text = _text(_plan_path(ctx, stem))
        resolved = _section(text, "Resolved decisions")
        shaping = re.search(r"Plan-shaping:\*{0,2}\s*yes", text, re.IGNORECASE)
        ok = answer in resolved and shaping is None
        return ok, (
            f"Resolved decisions {'has' if answer in resolved else 'lacks'} {answer!r}; "
            f"Plan-shaping yes {'present' if shaping else 'gone'}"
        )

    return Check("revise resolved the answer", fn)


def record_rationale_before_edit() -> Check:
    """Record how many runs call ``whygraph_rationale_brief`` before their first edit."""

    def fn(ctx: Context) -> Outcome:
        hits = 0
        for run in ctx.runs:
            main = run.of_type("main")
            uses = main[0].tool_uses() if main else []
            for use in uses:
                if _tool_matches(use.name, r"^(Edit|Write)$"):
                    break
                if "whygraph_rationale_brief" in use.name:
                    hits += 1
                    break
        return True, f"{hits} of {len(ctx.runs)} prompts called it before an edit"

    return Check("pre-edit rationale", fn, record=True)


HIDDEN_TESTS = Path(__file__).resolve().parent / "fixtures" / "why" / "hidden"
_HISTORY_READ_RE = re.compile(r"\bgit\b[^|;&\n]*\b(log|show|blame)\b")


def hidden_test(name: str) -> Check:
    """Record whether a hidden benchmark test passes on the case's end state.

    The test lives under ``fixtures/why/hidden/`` and never in the fixture
    repository, so the agent cannot see it. It is copied to the work directory
    root after the last prompt, run with ``python3 -m unittest`` and removed.

    Parameters
    ----------
    name : str
        The test module's name without ``.py``, for example ``test_hidden_s1``.

    Returns
    -------
    Check
        A recording check whose ``ok`` is whether the hidden test passed.
    """

    def fn(ctx: Context) -> Outcome:
        source = HIDDEN_TESTS / f"{name}.py"
        target = ctx.workdir / f"{name}.py"
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        target.write_bytes(source.read_bytes())
        try:
            out = subprocess.run(
                ["python3", "-m", "unittest", name],
                cwd=ctx.workdir,
                capture_output=True,
                text=True,
                timeout=120,
                env={**env, "PYTHONDONTWRITEBYTECODE": "1"},
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, "hidden test timed out after 120 s"
        finally:
            target.unlink(missing_ok=True)
        tail = (out.stderr.strip().splitlines() or ["no output"])[-1]
        return (
            out.returncode == 0,
            f"{'pass' if out.returncode == 0 else 'fail'}: {tail}",
        )

    return Check(f"hidden test {name}", fn, record=True)


def record_card_read() -> Check:
    """Record whether any transcript of the case called ``whygraph_rationale_brief``."""

    def fn(ctx: Context) -> Outcome:
        calls = [
            t.agent_type
            for t in ctx.all_transcripts()
            for u in t.tool_uses()
            if "whygraph_rationale_brief" in u.name
        ]
        return bool(calls), f"{len(calls)} rationale call(s) in {sorted(set(calls))}"

    return Check("card read", fn, record=True)


def record_history_reads() -> Check:
    """Record whether a ``Bash`` call ran ``git log``, ``git show`` or ``git blame``."""

    def fn(ctx: Context) -> Outcome:
        hits = [
            c
            for c in _bash_commands(ctx.all_transcripts())
            if _HISTORY_READ_RE.search(c)
        ]
        return bool(hits), f"{len(hits)} history read(s)" + (
            f", first: {hits[0][:80]!r}" if hits else ""
        )

    return Check("history read", fn, record=True)


def record_plan_keeps(stem: str, patterns: tuple[str, ...]) -> Check:
    """Record whether a plan names a constraint under its constraints or its blockers.

    Parameters
    ----------
    stem : str
        The plan's stem under ``.whygraph/plans/``.
    patterns : tuple of str
        Case-insensitive regexes; any one on a **Constraints to preserve** line
        or in ``## Blockers`` counts.

    Returns
    -------
    Check
        A recording check whose ``ok`` is whether either place names it.
    """

    def fn(ctx: Context) -> Outcome:
        text = _text(_plan_path(ctx, stem))
        if not text:
            return False, "no plan written"
        preserve = "\n".join(
            line for line in text.splitlines() if "Constraints to preserve" in line
        )
        blockers = _section(text, "Blockers")

        def named(body: str) -> bool:
            return any(re.search(p, body, re.IGNORECASE) for p in patterns)

        kept, blocked = named(preserve), named(blockers)
        return kept or blocked, f"constraints to preserve: {kept}; blocker: {blocked}"

    return Check("plan keeps the constraint", fn, record=True)


# The tags of plan 54 section 5.7: a card item's basis after its quote.
_SOURCE_TAG = re.compile(
    r"\s*\((?:quoted: [^()\n]+|cited: [^()\n]+|inferred|note #\d+)\)"
)
_STEP_LABEL = re.compile(r"^\s*(?:-\s+)?\*\*([A-Z][A-Za-z -]*):\*\*")


def _step_fields(steps: str, names: tuple[str, ...]) -> list[tuple[str, str]]:
    """Return ``(name, text)`` for each step field in ``names``, continuation lines joined."""
    fields: list[tuple[str, str]] = []
    current: str | None = None
    for line in steps.splitlines():
        label = _STEP_LABEL.match(line)
        if re.match(r"^\d+\. ", line):
            current = None
        elif label:
            current = label.group(1) if label.group(1) in names else None
            if current:
                fields.append((current, line[label.end() :]))
        elif current and line.strip():
            fields[-1] = (current, f"{fields[-1][1]} {line.strip()}")
    return fields


def constraint_quotes_tagged(stem: str) -> Check:
    """Pass when each constraint quote in a plan's steps ends with a source tag.

    Parameters
    ----------
    stem : str
        The plan's stem under ``.whygraph/plans/``.

    Returns
    -------
    Check
        Fails when the plan is missing, quotes nothing under **Constraints to
        preserve** or **Changes on purpose**, or has a quote with no tag. On a
        **Changes on purpose** line only the first quote is the constraint.

    Notes
    -----
    A tag is ``(quoted: <ref>)``, ``(cited: <ref>)``, ``(inferred)`` or
    ``(note #<id>)``. An item of basis ``unknown`` has none, so a card cached
    before sources existed fails this check: refresh the cards first.
    """

    def fn(ctx: Context) -> Outcome:
        text = _text(_plan_path(ctx, stem))
        if not text:
            return False, "no plan written"
        fields = _step_fields(
            _section(text, "Steps"), ("Constraints to preserve", "Changes on purpose")
        )
        quotes: list[str] = []
        untagged: list[str] = []
        for name, body in fields:
            found = list(re.finditer(r'"([^"]+)"', body))
            if name == "Changes on purpose":
                found = found[:1]
            for match in found:
                quotes.append(match.group(1))
                if not _SOURCE_TAG.match(body, match.end()):
                    untagged.append(match.group(1)[:60])
        if not quotes:
            return False, "no quote under Constraints to preserve or Changes on purpose"
        if untagged:
            return (
                False,
                f"{len(untagged)} of {len(quotes)} quotes untagged: {untagged}",
            )
        return True, f"{len(quotes)} quotes, each tagged"

    return Check("constraint quotes carry a source tag", fn)


def record_comparison(defect_step: int | None) -> Check:
    """Record, for the section 6.4 comparison, what the evaluators caught."""

    def fn(ctx: Context) -> Outcome:
        verdicts = [
            (int(step), verdict)
            for t in ctx.of_type("whygraph-evaluator")
            for step, verdict in VERDICT_RE.findall(t.final_text())
        ]
        steps_done = sorted(
            int(p.stem)
            for p in (ctx.workdir / ".whygraph" / "plans").glob("*/steps/*.t1")
        )
        caught = defect_step is not None and (defect_step, "escalate") in verdicts
        return True, (
            f"verdicts {verdicts}; steps done {steps_done}; "
            f"defect caught: {caught if defect_step else 'n/a'}"
        )

    return Check("comparison record", fn, record=True)


def web_search_only_in_external() -> Check:
    """Pass when ``WebSearch`` ran only in the external researcher and no page was fetched."""

    def fn(ctx: Context) -> Outcome:
        searches = {
            t.agent_type
            for t in ctx.all_transcripts()
            for u in t.tool_uses()
            if _tool_matches(u.name, r"^WebSearch$")
        }
        fetches = [
            u
            for t in ctx.all_transcripts()
            for u in t.tool_uses()
            if _tool_matches(u.name, r"^WebFetch$")
        ]
        ok = searches <= {"whygraph-researcher-external"} and not fetches
        return ok, f"WebSearch in {sorted(searches)}, {len(fetches)} WebFetch call(s)"

    return Check("web search only in the external researcher", fn)


def outside_evidence_stays_in_prior_art(stem: str) -> Check:
    """Pass when every cited URL sits in ``## Prior art`` and ends ``(external)``.

    This repository's evidence decides the plan: an outside finding may inform
    it only from ``## Prior art`` and never appears in a step.
    """

    def fn(ctx: Context) -> Outcome:
        plan = _text(_plan_path(ctx, stem))
        prior = [
            line for line in _section(plan, "Prior art").splitlines() if "http" in line
        ]
        untagged = [line for line in prior if "(external)" not in line]
        in_steps = "http" in _section(plan, "Steps")
        ok = bool(plan) and not untagged and not in_steps
        return ok, (
            f"{len(prior)} outside line(s), {len(untagged)} untagged, "
            f"URL in steps: {in_steps}"
        )

    return Check("outside evidence only in Prior art", fn)


_AGENT_TOOL = r"^(Agent|Task)$"
_PLAN_TOOL_RE = re.compile(r"plan-tool\.sh\s+([a-z-]+)\s+(\S+)\s+(\S+)")


def _steps_dir(ctx: Context, stem: str) -> Path:
    """Return ``.whygraph/plans/<stem>/steps``."""
    return ctx.workdir / ".whygraph" / "plans" / stem / "steps"


def _bash_commands(transcripts: list[Transcript]) -> list[str]:
    """Return every ``Bash`` command of the transcripts, in order."""
    return [
        str(u.input.get("command", ""))
        for t in transcripts
        for u in t.tool_uses()
        if _tool_matches(u.name, r"^Bash$")
    ]


def _implementor_prompts(transcripts: list[Transcript]) -> list[str]:
    """Return the prompt of every implementor dispatch, in order."""
    return [
        str(u.input.get("prompt", ""))
        for t in transcripts
        for u in t.tool_uses()
        if _tool_matches(u.name, _AGENT_TOOL)
        and u.input.get("subagent_type") == "whygraph-implementor"
    ]


def _attempt_lines(ctx: Context, stem: str) -> list[list[str]]:
    """Return the plan's ``attempts`` lines, split into words."""
    text = _text(_steps_dir(ctx, stem) / "attempts")
    return [line.split() for line in text.splitlines() if line.strip()]


def _amendment_lines(ctx: Context, stem: str) -> list[str]:
    """Return the ``- `` lines of the plan's ``## Amendments`` section."""
    section = _section(_text(_plan_path(ctx, stem)), "Amendments")
    return [line for line in section.splitlines() if line.startswith("- ")]


def _find_processes(pattern: str) -> list[int]:
    """Return the pids whose command line holds ``pattern``, without this one."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", "--", pattern],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        pids = [int(p) for p in out.split()]
    except FileNotFoundError:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        pids = [
            int(line.split(None, 1)[0])
            for line in out.splitlines()
            if pattern in line and "ps -A" not in line
        ]
    return [p for p in pids if p != os.getpid()]


def no_process(pattern: str) -> Check:
    """Pass when no process runs ``pattern`` a few seconds after the run.

    A process still running fails the check and is killed, ``TERM`` and then
    ``KILL``, so a failed case leaves nothing behind.
    """

    def fn(ctx: Context) -> Outcome:
        pids: list[int] = []
        for _ in range(3):
            pids = _find_processes(pattern)
            if not pids:
                return True, f"no process runs {pattern!r}"
            time.sleep(1)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pid in _find_processes(pattern):
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
            time.sleep(1)
        left = _find_processes(pattern)
        return False, f"pids {pids} ran {pattern!r}; killed, {len(left)} left"

    return Check(f"no {pattern} process", fn)


def step_diff_paths(stem: str, expected: dict[int, set[str]]) -> Check:
    """Pass when each ``steps/<N>.diff`` touches exactly the paths listed for N."""

    def fn(ctx: Context) -> Outcome:
        found = {}
        for step in expected:
            diff = _text(_steps_dir(ctx, stem) / f"{step}.diff")
            paths = set()
            for match in re.finditer(r"^diff --git a/(\S+) b/(\S+)$", diff, re.M):
                paths.update(match.groups())
            found[step] = paths
        ok = found == expected
        return ok, f"diff paths { ({k: sorted(v) for k, v in found.items()}) }"

    return Check("each step diff holds only its paths", fn)


def begin_end_around_implementors() -> Check:
    """Pass when the main session runs ``begin`` before and ``end`` after each step run.

    Every implementor dispatch with ``STEP_RANGE: <N>`` needs a
    ``plan-tool.sh begin <plan> <N>`` earlier in the main session, and a
    ``plan-tool.sh end <plan> <N>`` before the next implementor dispatch.
    """

    def fn(ctx: Context) -> Outcome:
        events: list[tuple[str, str]] = []
        for transcript in ctx.of_type("main"):
            for use in transcript.tool_uses():
                if _tool_matches(use.name, r"^Bash$"):
                    command = str(use.input.get("command", ""))
                    for kind, _plan, step in _PLAN_TOOL_RE.findall(command):
                        if kind in {"begin", "end"}:
                            events.append((kind, step))
                elif (
                    _tool_matches(use.name, _AGENT_TOOL)
                    and use.input.get("subagent_type") == "whygraph-implementor"
                ):
                    prompt = str(use.input.get("prompt", ""))
                    match = re.search(r"^STEP_RANGE:\s*(\S+)", prompt, re.M)
                    events.append(("run", match.group(1) if match else "?"))
        runs, problems = 0, []
        for index, (kind, step) in enumerate(events):
            if kind != "run" or step == "acceptance":
                continue
            runs += 1
            if not step.isdigit():
                problems.append(f"run {runs} has STEP_RANGE {step}")
                continue
            later = events[index + 1 :]
            stop = next((i for i, e in enumerate(later) if e[0] == "run"), len(later))
            if ("begin", step) not in events[:index]:
                problems.append(f"run {runs} (step {step}): no begin before")
            if ("end", step) not in later[:stop]:
                problems.append(f"run {runs} (step {step}): no end after")
        ok = runs > 0 and not problems
        return (
            ok,
            f"{runs} step run(s); {'; '.join(problems) or 'begin and end around each'}",
        )

    return Check("begin before and end after each step run", fn)


def reaches_acceptance() -> Check:
    """Pass when the run got to Acceptance.

    Evidence is any of: the main session ran ``plan-tool.sh accept``, an
    implementor was dispatched with ``STEP_RANGE: acceptance``, or the session
    printed ``## Run summary``.
    """

    def fn(ctx: Context) -> Outcome:
        main = ctx.of_type("main")
        evidence = []
        if any(re.search(r"plan-tool\.sh\s+accept\s", c) for c in _bash_commands(main)):
            evidence.append("accept called")
        if any("STEP_RANGE: acceptance" in p for p in _implementor_prompts(main)):
            evidence.append("acceptance run")
        if any(re.search(r"^#{1,6} Run summary", t, re.M) for t in _main_texts(ctx)):
            evidence.append("run summary")
        return bool(evidence), ", ".join(evidence) or "no sign of Acceptance"

    return Check("reaches Acceptance", fn)


def amendments(stem: str, count: int, field_name: str | None = None) -> Check:
    """Pass when ``## Amendments`` holds ``count`` lines, each of ``field_name``."""

    def fn(ctx: Context) -> Outcome:
        lines = _amendment_lines(ctx, stem)
        wrong = [
            line
            for line in lines
            if field_name and not re.match(rf"- Step \d+: {field_name} - ", line)
        ]
        ok = len(lines) == count and not wrong
        return ok, f"{len(lines)} amendment(s): {lines}"

    kind = f" of {field_name}" if field_name else ""
    return Check(f"{count} amendment(s){kind}", fn)


def attempt_count(
    stem: str,
    step: int,
    kind: str,
    minimum: int = 0,
    maximum: int | None = None,
) -> Check:
    """Pass when ``attempts`` holds between ``minimum`` and ``maximum`` lines of a kind."""

    def fn(ctx: Context) -> Outcome:
        count = sum(
            1 for words in _attempt_lines(ctx, stem) if words[:2] == [str(step), kind]
        )
        ok = count >= minimum and (maximum is None or count <= maximum)
        return ok, f"{count} '{step} {kind}' line(s) in attempts"

    upper = "" if maximum is None else f"..{maximum}"
    return Check(f"step {step} {kind} attempts {minimum}{upper}", fn)


def log_ever_had(stem: str, text: str) -> Check:
    """Pass when the plan's log holds ``text``, now or in any write to the plan.

    A later run of a step replaces its log entry, so a line an earlier run wrote
    is looked for in every ``Edit`` and ``Write`` of the plan, too.
    """

    def fn(ctx: Context) -> Outcome:
        if text in _section(_text(_plan_path(ctx, stem)), "Implementation log"):
            return True, f"log holds {text!r}"
        for transcript in ctx.all_transcripts():
            for use in transcript.tool_uses():
                if not _tool_matches(use.name, r"^(Edit|Write|MultiEdit)$"):
                    continue
                if not str(use.input.get("file_path", "")).endswith(f"/{stem}.md"):
                    continue
                if text in json.dumps(use.input, ensure_ascii=False):
                    return True, f"{transcript.agent_type} wrote {text!r} to the log"
        return False, f"no write of {text!r} to the log"

    return Check(f"log had {text}", fn)


def no_plan_tool_call(subcommand: str) -> Check:
    """Pass when no session ran ``plan-tool.sh <subcommand>``."""

    def fn(ctx: Context) -> Outcome:
        pattern = rf"plan-tool\.sh\s+{re.escape(subcommand)}\b"
        calls = [
            c for c in _bash_commands(ctx.all_transcripts()) if re.search(pattern, c)
        ]
        return not calls, f"{len(calls)} {subcommand} call(s)"

    return Check(f"no plan-tool {subcommand}", fn)


def record_ladder(stem: str, expect: str) -> Check:
    """Record the rungs a run used and how it ended, against an expected end.

    Reads ``attempts``, ``## Amendments``, the ``fix`` lines of the log and the
    last stop block. Never fails.
    """

    def fn(ctx: Context) -> Outcome:
        rungs = [
            " ".join(words[:3])
            for words in _attempt_lines(ctx, stem)
            if len(words) > 1 and words[1] not in {"begin", "reset"}
        ]
        log = _section(_text(_plan_path(ctx, stem)), "Implementation log")
        fixes = len(re.findall(r"^\s+- fix ", log, re.M))
        blocks = stop_blocks(_main_texts(ctx))
        if blocks:
            cause = blocks[-1].fields.get("Cause", "").strip()
            end = f"stopped at step {blocks[-1].step}: {cause.splitlines()[0] if cause else '?'}"
        else:
            end = "no stop block"
        return True, (
            f"rungs {rungs}; amendments {_amendment_lines(ctx, stem)}; "
            f"fix lines {fixes}; {end}; expected: {expect}"
        )

    return Check("ladder record", fn, record=True)


# The plan review of section 5.9: the reviewer's reply, its sidecar and the fixer.
_FINDING_RE = re.compile(
    r"^\s*-\s*F(\d+)\s*\|\s*(blocker|should)\s*\|\s*([^|]+?)\s*\|\s*(.*?)\s*"
    r"\|\s*evidence:\s*(.*?)\s*\|\s*fix:\s*(.*)$",
    re.IGNORECASE,
)
_LOCATION_RE = re.compile(r"([\w./@~-]+\.\w+):(\d+)(?:-(\d+))?")
_REVIEW_LINE_RE = re.compile(r"^- F(\d+) \((\w+)\): (\w+)", re.MULTILINE)


@dataclass(frozen=True)
class Finding:
    """One ``- F<n> | ...`` line of a plan reviewer's reply.

    Attributes
    ----------
    number : int
        The ``F<n>`` number.
    severity : str
        ``blocker`` or ``should``.
    where : str
        ``step <N>``, ``acceptance`` or ``plan``, lower-cased.
    defect : str
        The defect text.
    evidence : str
        The text after ``evidence:``.
    lens : str
        The reviewer's lens, when known.
    """

    number: int
    severity: str
    where: str
    defect: str
    evidence: str
    lens: str = ""

    def locations(self) -> list[tuple[str, int, int]]:
        """Return each ``path:line`` or ``path:start-end`` of the evidence."""
        return [
            (path, int(start), int(end or start))
            for path, start, end in _LOCATION_RE.findall(self.evidence)
        ]

    def label(self) -> str:
        """Return a short label for a report line."""
        return f"F{self.number} {self.severity} {self.where}: {self.defect[:60]}"


def parse_review(text: str, lens: str = "") -> tuple[str | None, list[Finding]]:
    """Parse a reviewer reply, or one block of the review sidecar.

    Parameters
    ----------
    text : str
        The reply.
    lens : str
        The lens to record on each finding when the text has no ``REVIEW:`` line.

    Returns
    -------
    tuple of (str or None, list of Finding)
        The lens of the first ``REVIEW: <lens>`` line (``None`` without one),
        and the findings in reply order.
    """
    head = re.search(r"^REVIEW: (\w+)", text, re.MULTILINE)
    found_lens = head.group(1) if head else None
    findings = []
    for line in text.splitlines():
        match = _FINDING_RE.match(line)
        if match:
            number, severity, where, defect, evidence, _fix = match.groups()
            findings.append(
                Finding(
                    int(number),
                    severity.lower(),
                    where.strip().lower(),
                    defect,
                    evidence,
                    found_lens or lens,
                )
            )
    return found_lens, findings


def _review_reply(ctx: Context) -> tuple[str, str]:
    """Return the plan reviewer's reply and where it came from."""
    reviewers = ctx.of_type("whygraph-plan-reviewer")
    if reviewers:
        text = reviewers[-1].final_text()
        if text.strip():
            return text, "reviewer transcript"
    return (ctx.runs[-1].reply if ctx.runs else ""), "main reply"


@dataclass(frozen=True)
class Seeded:
    """A defect seeded into a review fixture, and where its evidence may point.

    Attributes
    ----------
    label : str
        The defect, for the report.
    steps : tuple of int
        The ``step <N>`` values a finding for it may name.
    at : tuple of (str, tuple of int)
        Path suffixes, each with the lines the evidence may name. A range in
        the evidence counts when it holds one of them.
    """

    label: str
    steps: tuple[int, ...]
    at: tuple[tuple[str, tuple[int, ...]], ...]

    def matched_by(self, finding: Finding) -> bool:
        """Return whether a finding names this defect's step and seeded line."""
        named = (
            re.findall(r"\d+", finding.where)
            if finding.where.startswith("step")
            else []
        )
        if not any(int(n) in self.steps for n in named):
            return False
        return any(
            path.endswith(suffix) and any(start <= n <= end for n in lines)
            for path, start, end in finding.locations()
            for suffix, lines in self.at
        )


def review_findings(expected: tuple[Seeded, ...]) -> Check:
    """Pass when the reviewer reports every seeded defect, each at its seeded line.

    Each seeded defect needs its own finding that names one of its steps and
    gives evidence at one of its lines. Other findings are listed, not failed.
    """

    def fn(ctx: Context) -> Outcome:
        text, where = _review_reply(ctx)
        lens, findings = parse_review(text)
        if lens is None:
            return False, f"no REVIEW line in the {where}"
        used: set[int] = set()
        parts, missed = [], 0
        for seed in expected:
            hit = next(
                (f for f in findings if f.number not in used and seed.matched_by(f)),
                None,
            )
            if hit is None:
                missed += 1
                parts.append(f"missed: {seed.label}")
            else:
                used.add(hit.number)
                parts.append(f"F{hit.number}: {seed.label}")
        extra = [f.label() for f in findings if f.number not in used]
        parts.append(f"other findings {len(extra)}: {extra}")
        return not missed, "; ".join(parts)

    return Check(f"reviewer finds {len(expected)} seeded defects", fn)


def review_blocker_count(maximum: int) -> Check:
    """Pass when the reviewer reports at most ``maximum`` blockers; list every finding."""

    def fn(ctx: Context) -> Outcome:
        text, where = _review_reply(ctx)
        lens, findings = parse_review(text)
        if lens is None:
            return False, f"no REVIEW line in the {where}"
        blockers = [f for f in findings if f.severity == "blocker"]
        shoulds = [f for f in findings if f.severity == "should"]
        return len(blockers) <= maximum, (
            f"{len(blockers)} blocker(s), {len(shoulds)} should(s): "
            f"{[f.label() for f in findings]}"
        )

    return Check(f"at most {maximum} blocker finding(s)", fn)


def agent_model(agent_type: str, tag: str) -> Check:
    """Pass when every transcript of an agent type ran on a model naming ``tag``."""

    def fn(ctx: Context) -> Outcome:
        agents = ctx.of_type(agent_type)
        models = {m for t in agents for m in t.models()}
        ok = bool(agents) and all(tag in m for m in models)
        return ok, f"{len(agents)} {agent_type} transcript(s), models {sorted(models)}"

    return Check(f"{agent_type} on {tag}", fn)


def plan_tool_check(stem: str) -> Check:
    """Pass when ``plan-tool.sh check`` exits 0 on the plan."""

    def fn(ctx: Context) -> Outcome:
        proc = subprocess.run(
            ["sh", ".claude/skills/whygraph-plan/plan-tool.sh", "check"]
            + [str(_plan_path(ctx, stem))],
            cwd=ctx.workdir,
            capture_output=True,
            text=True,
            check=False,
        )
        lines = proc.stdout.strip().splitlines()
        return proc.returncode == 0, f"exit {proc.returncode}; {lines[:3]}"

    return Check("plan-tool check exits 0", fn)


def _drop_section(text: str, heading: str) -> str:
    """Return ``text`` without the ``## <heading>`` section."""
    match = re.search(rf"^## {re.escape(heading)}.*$", text, re.MULTILINE)
    if not match:
        return text
    rest = text[match.end() :]
    following = re.search(r"^## ", rest, re.MULTILINE)
    return text[: match.start()] + (rest[following.start() :] if following else "")


def _plan_writes(transcript: Transcript, stem: str) -> list[tuple[str, dict[str, Any]]]:
    """Return the successful ``Write`` / ``Edit`` / ``MultiEdit`` calls on a plan."""
    failed = set()
    for record in transcript.records:
        content = (record.get("message") or {}).get("content")
        if record.get("type") != "user" or not isinstance(content, list):
            continue
        failed.update(
            b.get("tool_use_id")
            for b in content
            if isinstance(b, dict)
            and b.get("type") == "tool_result"
            and b.get("is_error")
        )
    writes = []
    for record in transcript.assistant_records():
        for block in (record.get("message") or {}).get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = str(block.get("name", ""))
            args = block.get("input") or {}
            if (
                name in {"Write", "Edit", "MultiEdit"}
                and str(args.get("file_path", "")).endswith(
                    f"/.whygraph/plans/{stem}.md"
                )
                and block.get("id") not in failed
            ):
                writes.append((name, args))
    return writes


def _apply_edit(text: str, args: dict[str, Any]) -> str:
    """Apply one ``Edit`` as the tool does; an edit the tool would refuse is skipped."""
    old, new = str(args.get("old_string", "")), str(args.get("new_string", ""))
    count = text.count(old) if old else 0
    if count == 0 or (count > 1 and not args.get("replace_all")):
        return text
    return (
        text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
    )


def _replay(text: str | None, writes: list[tuple[str, dict[str, Any]]]) -> str | None:
    """Return the plan after a transcript's writes, starting from ``text``."""
    for name, args in writes:
        if name == "Write":
            text = str(args.get("content", ""))
        elif text is not None and name == "Edit":
            text = _apply_edit(text, args)
        elif text is not None:
            for edit in args.get("edits") or []:
                text = _apply_edit(text, edit)
    return text


def _review_states(ctx: Context, stem: str) -> tuple[str | None, str | None, str]:
    """Return the plan before the review, after the fixer, and a note on how.

    Before is the first synthesizer that wrote the plan without ``REVIEW_PATH``
    (the **2e** run), replayed through its own edits, the check loop included.
    After is that text replayed through the fixer's writes: the first
    synthesizer with ``REVIEW_PATH`` and ``ANSWERS: none``. A later revise
    (``--no-questions`` answers its defaults) is therefore left out.
    """
    synthesizers = sorted(
        ctx.of_type("whygraph-synthesizer"), key=Transcript.first_timestamp
    )
    plain = [t for t in synthesizers if "REVIEW_PATH:" not in t.first_prompt()]
    fixers = [
        t
        for t in synthesizers
        if "REVIEW_PATH:" in t.first_prompt()
        and re.search(r"^ANSWERS: none\s*$", t.first_prompt(), re.MULTILINE)
    ]
    before = next(
        (
            text
            for t in plain
            if (text := _replay(None, _plan_writes(t, stem))) is not None
        ),
        None,
    )
    if before is None:
        return None, None, "no synthesizer Write of the plan"
    if not fixers:
        return before, None, "no fixer transcript"
    after = _replay(before, _plan_writes(fixers[0], stem))
    return before, after, f"{len(plain)} synthesizer(s), {len(fixers)} fixer(s)"


def _line_keys(lines: list[str]) -> list[str]:
    """Return, per line, ``step <N>`` inside ``## Steps`` or the section heading."""
    keys, section, step = [], "", None
    for line in lines:
        heading = re.match(r"^## (\S+(?: \S+)*?)(?::.*)?\s*$", line)
        if heading:
            section, step = heading.group(1).lower(), None
        elif section == "steps" and (item := re.match(r"^(\d+)\. \*\*", line)):
            step = int(item.group(1))
        keys.append(f"step {step}" if section == "steps" and step else section)
    return keys


def _applied_blockers(ctx: Context, stem: str, after: str) -> set[str]:
    """Return the ``where`` of each blocker the fixer applied, as ``step <N>`` or a section."""
    sidecar = _text(ctx.workdir / ".whygraph" / "plans" / f"{stem}.review.md")
    blockers = {}
    for lens, body in re.findall(
        r"^=== (\w+) ===\s*$(.*?)(?=^=== |\Z)", sidecar, re.MULTILINE | re.DOTALL
    ):
        for finding in parse_review(body, lens)[1]:
            if finding.severity == "blocker":
                blockers[(lens, finding.number)] = finding.where
    applied = {
        (lens, int(number))
        for number, lens, status in _REVIEW_LINE_RE.findall(
            _section(after, "Plan review")
        )
        if status == "applied"
    }
    return {where for key, where in blockers.items() if key in applied}


_BLOCKER_ONLY_RE = re.compile(
    r"^(\s*|## Blockers.*|\*\(.*\)\*|- \*\*Review finding .*)$"
)


def plan_growth(stem: str, pct: float) -> Check:
    """Pass when the review grew the plan by at most ``pct`` percent in lines.

    Measured from the plan the first synthesizer wrote (``_review_states``) to
    the fixer's result, both without ``## Plan review``. Exempt: review-finding
    lines in ``## Blockers``, and every change inside a step or section that an
    applied ``blocker`` names.
    """

    def fn(ctx: Context) -> Outcome:
        before, after, note = _review_states(ctx, stem)
        if before is None or after is None:
            return False, note
        exempt_where = _applied_blockers(ctx, stem, after)
        base = _drop_section(before, "Plan review").splitlines()
        post = _drop_section(after, "Plan review").splitlines()
        keys = _line_keys(base) or [""]
        growth = exempt = 0
        matcher = difflib.SequenceMatcher(None, base, post, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            net = (j2 - j1) - (i2 - i1)
            at = i1 - 1 if tag == "insert" and i1 > 0 else min(i1, len(keys) - 1)
            only_blockers = all(_BLOCKER_ONLY_RE.match(x) for x in post[j1:j2])
            if net > 0 and (keys[at] in exempt_where or only_blockers):
                exempt += net
            else:
                growth += net
        share = 100.0 * growth / max(len(base), 1)
        return share <= pct, (
            f"{len(base)} -> {len(post)} lines, growth {growth} ({share:.1f}%, "
            f"cap {pct:g}%), exempt {exempt} for blockers {sorted(exempt_where)}; {note}"
        )

    return Check(f"review growth at most {pct:g}%", fn)


_QUOTED_RE = re.compile(r"\*\*(Constraints to preserve|Risks|Constraint at risk):\*\*")


def review_keeps_fixed_text(stem: str) -> Check:
    """Pass when the fixer left the Objective, Non-goals, Resolved decisions and quotes.

    Compares the same two texts as ``plan_growth``: the Objective and Non-goals
    lines and the ``## Resolved decisions`` section are byte-identical, and every
    verbatim-quote line (Constraints to preserve, Risks, Constraint at risk, and
    the bullets of ``## Risks called out across the change``) is still there.
    """

    def fn(ctx: Context) -> Outcome:
        before, after, note = _review_states(ctx, stem)
        if before is None or after is None:
            return False, note

        def fixed(text: str) -> tuple[list[str], str, Counter[str]]:
            lines = text.splitlines()
            heads = [
                line
                for line in lines
                if line.startswith(("**Objective:**", "**Non-goals:**"))
            ]
            quotes = Counter(line for line in lines if _QUOTED_RE.search(line))
            risks = _section(text, "Risks called out across the change")
            quotes.update(x for x in risks.splitlines() if x.startswith("- "))
            return heads, _section(text, "Resolved decisions"), quotes

        heads_a, resolved_a, quotes_a = fixed(before)
        heads_b, resolved_b, quotes_b = fixed(after)
        problems = []
        if heads_a != heads_b:
            problems.append("Objective or Non-goals changed")
        if resolved_a.strip() != resolved_b.strip():
            problems.append("Resolved decisions changed")
        lost = quotes_a - quotes_b
        if lost:
            problems.append(
                f"{sum(lost.values())} quote line(s) changed: {list(lost)[:2]}"
            )
        return not problems, "; ".join(problems) or f"unchanged; {note}"

    return Check("review keeps the fixed text", fn)


def plan_review_kept(stem: str) -> Check:
    """Pass when the final plan has ``## Plan review``; name any revise that ran."""

    def fn(ctx: Context) -> Outcome:
        text = _text(_plan_path(ctx, stem))
        ok = re.search(r"^## Plan review[ \t]*$", text, re.MULTILINE) is not None
        revises = [
            u
            for t in ctx.all_transcripts()
            for u in t.tool_uses()
            if _tool_matches(u.name, _AGENT_TOOL)
            and u.input.get("subagent_type") == "whygraph-planner"
            and re.search(r"^MODE: revise", str(u.input.get("prompt", "")), re.M)
        ]
        return ok, (
            f"## Plan review {'present' if ok else 'absent'}; "
            f"{len(revises)} revise dispatch(es) in this run"
        )

    return Check("plan has ## Plan review", fn)


def _token_count(usage: dict[str, Any]) -> int:
    """Return the input, cache and output tokens of one ``usage`` record."""
    return sum(
        int(usage.get(k) or 0)
        for k in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
    )


def _transcript_tokens(transcript: Transcript) -> dict[str, int]:
    """Return the tokens of a transcript per model, each API message counted once."""
    seen: set[str] = set()
    tokens: Counter[str] = Counter()
    for record in transcript.assistant_records():
        message = record.get("message") or {}
        key = str(message.get("id") or id(record))
        if key in seen:
            continue
        seen.add(key)
        tokens[str(message.get("model", ""))] += _token_count(
            message.get("usage") or {}
        )
    return dict(tokens)


def record_review_cost() -> Check:
    """Record the review's share of the run cost: the reviewers and the fixer.

    The ledger has one cost per run. Each model's cost per token comes from the
    run's ``modelUsage``; the review agents' tokens are priced at that rate. It is
    an estimate: the cache mix of an agent differs from its model's average.
    """

    def fn(ctx: Context) -> Outcome:
        totals: dict[str, tuple[float, int]] = {}
        for run in ctx.runs:
            for model, usage in (run.data.get("modelUsage") or {}).items():
                cost, count = totals.get(model, (0.0, 0))
                count += sum(
                    int(usage.get(k) or 0)
                    for k in (
                        "inputTokens",
                        "outputTokens",
                        "cacheReadInputTokens",
                        "cacheCreationInputTokens",
                    )
                )
                totals[model] = (cost + float(usage.get("costUSD") or 0), count)
        rate = {m: c / n for m, (c, n) in totals.items() if n}

        def priced(transcripts: list[Transcript]) -> float:
            return sum(
                count * rate.get(model, 0.0)
                for t in transcripts
                for model, count in _transcript_tokens(t).items()
            )

        fixers = [
            t
            for t in ctx.of_type("whygraph-synthesizer")
            if "REVIEW_PATH:" in t.first_prompt()
        ]
        reviewers = priced(ctx.of_type("whygraph-plan-reviewer"))
        fixer = priced(fixers)
        total = sum(run.cost for run in ctx.runs)
        share = 100.0 * (reviewers + fixer) / total if total else 0.0
        return True, (
            f"review ~${reviewers + fixer:.2f} of ${total:.2f} ({share:.0f}%): "
            f"reviewers ${reviewers:.2f}, fixer ${fixer:.2f} (token-share estimate)"
        )

    return Check("review cost share", fn, record=True)
