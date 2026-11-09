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

import json
import os
import re
import subprocess
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


def halted(stem: str, step: int, reason: str) -> Check:
    """Pass when the reply says a step halted for ``reason``."""

    def fn(ctx: Context) -> Outcome:
        reply = ctx.runs[-1].reply.lower()
        ok = f"halted at step {step}" in reply and reason.lower() in reply
        return (
            ok,
            f"{stem} step {step}: {'halted' if ok else 'no halt line'} ({reason})",
        )

    return Check(f"halts: {reason}", fn)


def verdict_is_only(expected: str) -> Check:
    """Pass when the evaluators gave exactly one verdict, and it is ``expected``."""

    def fn(ctx: Context) -> Outcome:
        found = [
            v
            for t in ctx.of_type("whygraph-evaluator")
            for _step, v in VERDICT_RE.findall(t.final_text())
        ]
        return found == [expected], f"verdicts {found}"

    return Check(f"one verdict, {expected}", fn)


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
        blocks = re.findall(r"^=== (\S+) ===\s*$", _text(sidecar), re.MULTILINE)
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


def external_reaches_synthesizer(stem: str) -> Check:
    """Pass when the research a synthesizer got holds an ``external`` report.

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
                r"=== external ===\s*\n\s*(# Researcher report:|\(unavailable:)", p
            )
            for p in texts
        )
        return (
            ok,
            f"{len(texts)} {where}(s); external {'reached' if ok else 'absent'}",
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
