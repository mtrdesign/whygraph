"""Run the agent harness evals: live ``claude -p`` runs against fixtures.

The WhyGraph agent harness (planner, implementor, evaluator, reviewer and their
slash commands) is prompt text plus tool lists. No unit test can tell whether a
model, handed those prompts, halts on a Boundaries path or keeps a hostile
source away from its shell. So this drives the real entry points through
``claude -p``, on the owner's login, and judges only the end state: the files,
the reply and the session transcripts.

Deliberately **not** pytest: it calls a paid model and is non-deterministic, so
it must never gate CI. It is committed anyway because it is the only measure of
whether a harness change helped, and a change without a measure is an opinion.

The two limits below are enforced here rather than left to the operator:

* **Spend.** ``--ledger`` keeps one JSON line per run. Before each run the
  ledger is summed, and the run is refused when the sum plus the case budget
  exceeds ``--max-total-usd``. An unreadable ledger refuses too, because a cap
  that cannot be computed cannot be held.
* **The working copy.** An ``O`` case runs in a throwaway clone of
  ``fixtures/mini``. A ``P`` case runs in ``--repo``, in place: it refuses a
  dirty tree, works on a temporary branch ``eval-tmp`` and puts everything back.

Usage
-----
From the repository root::

    uv run python scripts/agent_evals/run.py --cases O1 --stage 1 \\
      --ledger plans/47-eval/spend.jsonl --max-total-usd 50

    # no model call: load every case, report what would run or be skipped
    uv run python scripts/agent_evals/run.py --dry-run --stage 1

``make agent-eval`` wraps the first form. See ``README.md`` for the cases and
what each result means.

Notes
-----
* A case that fails is run once more. Two failures count as a failure, and the
  report shows both.
* ``--stage N`` skips checks that test work a later rollout stage builds, so a
  baseline does not test what is not built yet.
* The invocation strips ``ANTHROPIC_API_KEY``, or a stale key in a shell profile
  would override the owner's login. Nothing here ever prints the environment.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cases import CASES, FIXTURES, Case, Workspace, validate
from checks import Context, RunResult, load_transcripts

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ASSETS = ROOT / "src" / "whygraph" / "assets" / "claude-code"
PORTAL_URL = "http://127.0.0.1:8777/"
ALLOWED_TOOLS = (
    "Agent,Bash,Read,Edit,Write,Glob,Grep,ToolSearch,ReadMcpResourceTool,"
    "mcp__whygraph__*,mcp__codegraph__*"
)
RUN_TIMEOUT_S = 3600
GIT_IDENTITY = [
    "-c",
    "user.name=agent-evals",
    "-c",
    "user.email=agent-evals@example.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.hooksPath=/dev/null",
]
LATEST_STAGE = 5


def default_repo() -> Path:
    """Return the default ``--repo``, the dev portal's ``ui-demo`` scratch repo."""
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "whygraph-dev" / "repos" / "ui-demo"


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Run git with the eval identity and with hooks disabled."""
    return subprocess.run(
        ["git", *GIT_IDENTITY, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


class Canary:
    """A listener on a free local port that counts every byte it receives.

    Attributes
    ----------
    port : int
        The bound port.
    bytes_received : int
        Bytes received so far, across connections.
    """

    def __init__(self) -> None:
        self._server = socket.create_server(("127.0.0.1", 0))
        self._server.settimeout(0.5)
        self.port: int = self._server.getsockname()[1]
        self.bytes_received = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._server.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(2)
                try:
                    while chunk := conn.recv(65536):
                        self.bytes_received += len(chunk)
                except OSError:
                    pass

    def close(self) -> None:
        """Stop listening."""
        self._stop.set()
        self._thread.join(timeout=2)
        self._server.close()


def ledger_total(path: Path) -> float:
    """Sum the ``cost`` of every line of the ledger.

    Parameters
    ----------
    path : pathlib.Path
        The ledger file. A missing file sums to 0.

    Returns
    -------
    float
        The total in USD.

    Raises
    ------
    ValueError
        When a line is not a JSON object with a numeric ``cost``.
    """
    if not path.exists():
        return 0.0
    total = 0.0
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        entry = json.loads(line)
        if not isinstance(entry, dict) or not isinstance(
            entry.get("cost"), int | float
        ):
            raise ValueError(f"ledger line {number} has no numeric cost")
        total += float(entry["cost"])
    return total


def _ledger_append(path: Path, entry: dict[str, Any]) -> None:
    """Append one line to the ledger."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def refuse_reason(ledger: Path, budget: float, cap: float) -> str | None:
    """Return why a run must not start, or ``None`` when it may.

    Parameters
    ----------
    ledger : pathlib.Path
        The ledger file.
    budget : float
        The budget of the run about to start.
    cap : float
        The ``--max-total-usd`` cap.

    Returns
    -------
    str or None
        The refusal, or ``None``.
    """
    try:
        total = ledger_total(ledger)
    except (OSError, ValueError) as exc:
        return f"refused: the ledger {ledger} cannot be read ({exc})"
    if total + budget > cap:
        return f"refused: ledger ${total:.2f} + budget ${budget:.2f} exceeds cap ${cap:.2f}"
    return None


def invoke(
    prompt: str, budget: float, workdir: Path, out_stem: Path | None
) -> RunResult:
    """Run one ``claude -p`` and read back its result and transcripts.

    Parameters
    ----------
    prompt : str
        The prompt, with ``{dir}`` already replaced.
    budget : float
        The run's ``--max-budget-usd``.
    workdir : pathlib.Path
        The working directory.
    out_stem : pathlib.Path or None
        Base path for the raw ``.json`` and ``.err`` of this run.

    Returns
    -------
    RunResult
        The run. A run with no parseable JSON is charged its whole budget.
    """
    cmd = [
        "env",
        "-u",
        "ANTHROPIC_API_KEY",
        "SSL_CERT_FILE=",
        "LC_ALL=C",
        "LANG=C",
        "claude",
        "-p",
        prompt,
        "--model",
        "opus",
        "--output-format",
        "json",
        "--max-budget-usd",
        f"{budget:.2f}",
        "--setting-sources",
        "project,local",
        "--strict-mcp-config",
        "--mcp-config",
        str(workdir / ".mcp.json"),
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        ALLOWED_TOOLS,
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return RunResult(prompt=prompt, cost=budget, cost_known=False, exit_code=-1)
    data: dict[str, Any] = {}
    try:
        parsed = json.loads(proc.stdout)
        if isinstance(parsed, list):
            parsed = next(
                (
                    x
                    for x in reversed(parsed)
                    if isinstance(x, dict) and x.get("type") == "result"
                ),
                {},
            )
        if isinstance(parsed, dict):
            data = parsed
    except json.JSONDecodeError:
        data = {}
    if out_stem is not None:
        out_stem.with_suffix(".json").write_text(proc.stdout, encoding="utf-8")
        out_stem.with_suffix(".err").write_text(proc.stderr, encoding="utf-8")
    cost = data.get("total_cost_usd")
    known = isinstance(cost, int | float)
    session_id = str(data.get("session_id", ""))
    return RunResult(
        prompt=prompt,
        reply=str(data.get("result", "")),
        data=data,
        cost=float(cost) if known else budget,
        cost_known=known,
        session_id=session_id,
        exit_code=proc.returncode,
        transcripts=load_transcripts(session_id),
    )


@contextmanager
def clone_workspace(
    case: Case, assets: Path, canary: Canary | None
) -> Iterator[Workspace]:
    """Build a throwaway clone of ``fixtures/mini`` and delete it afterwards.

    Parameters
    ----------
    case : Case
        The case to set up.
    assets : pathlib.Path
        The agent files to copy to ``.claude/``.
    canary : Canary or None
        The case's listener, if any.

    Yields
    ------
    Workspace
        The committed clone.
    """
    tmp = Path(tempfile.mkdtemp(prefix="agent-eval-")).resolve()
    try:
        repo = tmp / "repo"
        shutil.copytree(
            FIXTURES / "mini", repo, ignore=shutil.ignore_patterns("__pycache__")
        )
        _git(["init", "-q", "-b", "main"], repo)
        shutil.copytree(
            assets,
            repo / ".claude",
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        plans = repo / ".whygraph" / "plans"
        plans.mkdir(parents=True)
        for name in case.plans:
            shutil.copy(FIXTURES / "plans" / name, plans / name)
        (repo / ".mcp.json").write_text('{"mcpServers":{}}\n', encoding="utf-8")
        _git(["add", "-A"], repo)
        _git(["commit", "-q", "-m", "eval fixture"], repo)
        workspace = Workspace(repo, canary)
        if case.setup:
            case.setup(workspace)
        yield workspace
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def portal_skip_reason(case: Case, repo: Path) -> str | None:
    """Return why a ``P`` case cannot run, or ``None``.

    Parameters
    ----------
    case : Case
        The case.
    repo : pathlib.Path
        The ``--repo`` directory.

    Returns
    -------
    str or None
        A skip reason, or ``None``.
    """
    if case.kind != "P":
        return None
    probe = subprocess.run(
        ["curl", "-s", "-m", "2", "-o", os.devnull, PORTAL_URL],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        return f"portal not reachable at {PORTAL_URL} (curl exit {probe.returncode})"
    if not (repo / ".git").exists():
        return f"--repo {repo} is not a git repository"
    if not (repo / ".mcp.json").is_file():
        return f"--repo {repo} has no .mcp.json"
    status = _git(["status", "--porcelain"], repo).stdout.strip()
    if status:
        return f"--repo {repo} has uncommitted changes"
    if (
        _git(["rev-parse", "-q", "--verify", "eval-tmp"], repo, check=False).returncode
        == 0
    ):
        return f"--repo {repo} already has a branch eval-tmp"
    return None


@contextmanager
def portal_workspace(
    case: Case, repo: Path, assets: Path, canary: Canary | None
) -> Iterator[Workspace]:
    """Set up ``--repo`` in place on ``eval-tmp`` and restore it afterwards.

    Parameters
    ----------
    case : Case
        The case to set up.
    repo : pathlib.Path
        The clean ``--repo``.
    assets : pathlib.Path
        The agent files to copy over ``.claude/``.
    canary : Canary or None
        The case's listener, if any.

    Yields
    ------
    Workspace
        The repository, committed on ``eval-tmp``.
    """
    first = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip()
    if first == "HEAD":
        first = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    _git(["checkout", "-q", "-b", "eval-tmp"], repo)
    try:
        shutil.copytree(
            assets,
            repo / ".claude",
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        _git(["add", ".claude"], repo)
        _git(["commit", "-q", "--allow-empty", "-m", "eval: agent assets"], repo)
        workspace = Workspace(repo, canary)
        if case.setup:
            case.setup(workspace)
        yield workspace
    finally:
        for args in (
            ["checkout", "-q", "--", "."],
            ["clean", "-fdq"],
            ["checkout", "-q", first],
            ["branch", "-qD", "eval-tmp"],
        ):
            result = _git(args, repo, check=False)
            if result.returncode != 0:
                print(
                    f"warning: git {' '.join(args)} failed in {repo}: exit {result.returncode}"
                )


def _cleanup_portal_plans(repo: Path) -> None:
    """Delete the ``eval-*`` plans at the end of the invocation."""
    for path in (repo / ".whygraph" / "plans").glob("eval-*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


@dataclass
class CheckResult:
    """One check's result in an attempt."""

    name: str
    ok: bool
    detail: str
    recorded: bool


@dataclass
class Attempt:
    """One attempt at a case and its companions."""

    number: int
    runs: list[RunResult] = field(default_factory=list)
    results: dict[str, list[CheckResult]] = field(default_factory=dict)
    error: str | None = None
    refused: str | None = None

    def failed(self, case_id: str) -> bool:
        """Return whether this attempt failed for one case id."""
        if self.error or self.refused:
            return True
        return any(not r.ok and not r.recorded for r in self.results.get(case_id, []))


def run_attempt(
    case: Case,
    companions: list[Case],
    attempt_no: int,
    args: argparse.Namespace,
    out: Path,
) -> Attempt:
    """Run one attempt of a case and judge it and its companions.

    Parameters
    ----------
    case : Case
        The case to run.
    companions : list of Case
        Cases whose checks read this case's runs.
    attempt_no : int
        1 or 2.
    args : argparse.Namespace
        The parsed command line.
    out : pathlib.Path
        The report directory.

    Returns
    -------
    Attempt
        The attempt.
    """
    attempt = Attempt(attempt_no)
    canary = Canary() if case.canary else None
    try:
        workspace_cm = (
            portal_workspace(case, args.repo, args.assets, canary)
            if case.kind == "P"
            else clone_workspace(case, args.assets, canary)
        )
        with workspace_cm as workspace:
            spent = 0.0
            for index, template in enumerate(case.prompts):
                budget = round(case.budget - spent, 2)
                if budget <= 0:
                    attempt.error = f"case budget ${case.budget:.2f} spent before prompt {index + 1}"
                    break
                refused = refuse_reason(args.ledger, budget, args.max_total_usd)
                if refused:
                    attempt.refused = refused
                    break
                prompt = template.replace("{dir}", str(workspace.dir))
                stem = (
                    out / f"{case.id}-r{args.current_repeat}-a{attempt_no}-p{index + 1}"
                )
                run = invoke(prompt, budget, workspace.dir, stem)
                _ledger_append(
                    args.ledger,
                    {
                        "ts": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                        "case": case.id,
                        "stage": args.stage,
                        "repeat": args.current_repeat,
                        "attempt": attempt_no,
                        "prompt": index + 1,
                        "cost": run.cost,
                        "cost_known": run.cost_known,
                        "session_id": run.session_id,
                    },
                )
                attempt.runs.append(run)
                spent += run.cost
            if attempt.refused or attempt.error:
                return attempt
            ctx = Context(workspace.dir, attempt.runs, canary)
            for current in [case, *companions]:
                outcomes = []
                for check in current.active_checks(args.stage):
                    ok, detail = check.run(ctx)
                    outcomes.append(CheckResult(check.name, ok, detail, check.record))
                attempt.results[current.id] = outcomes
            _keep_plans(
                workspace.dir, out, f"{case.id}-r{args.current_repeat}-a{attempt_no}"
            )
    except (subprocess.CalledProcessError, OSError, RuntimeError) as exc:
        attempt.error = f"setup failed: {type(exc).__name__}: {exc}"
    finally:
        if canary:
            canary.close()
    return attempt


def _keep_plans(workdir: Path, out: Path, name: str) -> None:
    """Copy the case's ``.whygraph/plans`` into the report directory."""
    plans = workdir / ".whygraph" / "plans"
    if plans.is_dir():
        shutil.copytree(plans, out / name, dirs_exist_ok=True)


def _print_attempt(case_id: str, attempt: Attempt, label: str) -> None:
    """Print one attempt's runs and check results."""
    runs = attempt.runs
    cost = sum(r.cost for r in runs)
    status = "FAIL" if attempt.failed(case_id) else "pass"
    print(
        f"  {case_id} attempt {attempt.number}: {status} (${cost:.2f}, {len(runs)} run(s)) {label}"
    )
    if attempt.refused:
        print(f"    {attempt.refused}")
    if attempt.error:
        print(f"    {attempt.error}")
    for run in runs:
        usage = run.data.get("usage", {})
        subtype = run.data.get("subtype", "?")
        cost_note = "" if run.cost_known else " (cost unknown, budget charged)"
        print(
            f"    run: exit {run.exit_code}, subtype {subtype}, turns "
            f"{run.data.get('num_turns', '?')}, ${run.cost:.2f}{cost_note}, "
            f"tokens in/out {usage.get('input_tokens', '?')}/{usage.get('output_tokens', '?')}, "
            f"{len(run.transcripts)} transcript(s)"
        )
    for result in attempt.results.get(case_id, []):
        tag = "rec " if result.recorded else ("ok  " if result.ok else "FAIL")
        print(f"    [{tag}] {result.name}: {result.detail}")


def select_cases(requested: list[str] | None) -> list[Case]:
    """Resolve ``--cases`` to cases in suite order.

    Parameters
    ----------
    requested : list of str or None
        Ids, comma-separated or repeated. ``None`` selects every case.

    Returns
    -------
    list of Case
        The selection, with each ``same_run_as`` and ``after`` target added.

    Raises
    ------
    SystemExit
        On an unknown id.
    """
    by_id = {case.id: case for case in CASES}
    if requested is None:
        return list(CASES)
    wanted: set[str] = set()
    for item in requested:
        for name in item.split(","):
            case_id = name.strip().upper()
            if not case_id:
                continue
            if case_id not in by_id:
                raise SystemExit(f"Unknown case: {name.strip()}")
            wanted.add(case_id)
    for case in list(by_id.values()):
        if case.id in wanted:
            if case.same_run_as:
                wanted.add(case.same_run_as)
            if case.after:
                wanted.add(case.after)
    return [case for case in CASES if case.id in wanted]


def stage_skip_reason(case: Case, stage: int) -> str | None:
    """Return why ``--stage`` leaves nothing to check in a case, or ``None``.

    Parameters
    ----------
    case : Case
        The case.
    stage : int
        The ``--stage`` value.

    Returns
    -------
    str or None
        A skip reason, or ``None``.
    """
    if case.active_checks(stage):
        return None
    lowest = min(check.min_stage for check in case.checks)
    return f"every check needs stage >= {lowest}, and --stage is {stage}"


def dry_run(cases: list[Case], args: argparse.Namespace) -> int:
    """Report what would run, and why anything would not. Calls no model.

    Parameters
    ----------
    cases : list of Case
        The selection.
    args : argparse.Namespace
        The parsed command line.

    Returns
    -------
    int
        0 when every fixture is in place, otherwise 1.
    """
    problems = validate()
    for problem in problems:
        print(f"FIXTURE PROBLEM: {problem}")
    total = 0.0
    for case in cases:
        reason = stage_skip_reason(case, args.stage) or portal_skip_reason(
            case, args.repo
        )
        prefix = f"{case.id:<3} {case.name}"
        if reason:
            print(f"{prefix}: SKIP - {reason}")
            continue
        total += case.estimate
        print(
            f"{prefix}: would run, {len(case.prompts)} prompt(s), budget ${case.budget:.2f}, "
            f"est ${case.estimate:.2f}, checks {len(case.active_checks(args.stage))}/{len(case.checks)}"
        )
    print(f"{len(cases)} case(s) loaded, estimate for the runnable ones ${total:.2f}")
    if args.ledger:
        print(f"ledger {args.ledger}: ${ledger_total(args.ledger):.2f}")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    """Parse the command line, run the selected cases and report.

    Parameters
    ----------
    argv : list of str or None
        Arguments, ``None`` for ``sys.argv``.

    Returns
    -------
    int
        0 when nothing failed or was refused, 1 otherwise.
    """
    parser = argparse.ArgumentParser(description="Run the agent harness evals.")
    parser.add_argument(
        "--cases", nargs="+", help="Case ids, e.g. O1 O2 or O1,O2. Default: all."
    )
    parser.add_argument(
        "--stage",
        type=int,
        default=LATEST_STAGE,
        help="Skip checks with a higher min_stage.",
    )
    parser.add_argument("--repeat", type=int, default=1, help="Runs per case.")
    parser.add_argument(
        "--assets", type=Path, default=DEFAULT_ASSETS, help="Agent files to test."
    )
    parser.add_argument(
        "--ledger", type=Path, help="The spend ledger (required for a live run)."
    )
    parser.add_argument(
        "--max-total-usd",
        type=float,
        help="Hard cap over the whole ledger (required for a live run).",
    )
    parser.add_argument(
        "--out", type=Path, help="Report directory. Default: a new temp directory."
    )
    parser.add_argument(
        "--repo", type=Path, default=default_repo(), help="The repo for P cases."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Call no model; report what would run."
    )
    args = parser.parse_args(argv)
    args.assets = args.assets.resolve()
    args.repo = args.repo.expanduser().resolve()
    cases = select_cases(args.cases)
    if args.dry_run:
        return dry_run(cases, args)
    if args.ledger is None or args.max_total_usd is None:
        parser.error("a live run needs --ledger and --max-total-usd (or use --dry-run)")
    problems = validate()
    if problems:
        print("\n".join(problems))
        return 1
    out = (args.out or Path(tempfile.mkdtemp(prefix="agent-eval-report-"))).resolve()
    out.mkdir(parents=True, exist_ok=True)
    print(f"report directory: {out}")
    summary: list[str] = []
    failed = False
    ran: set[str] = set()
    ran_portal = False
    invocation_spend = 0.0
    try:
        for case in cases:
            companions = [c for c in cases if c.same_run_as == case.id]
            if case.same_run_as:
                continue
            reason = None
            if case.after and case.after not in ran:
                reason = f"needs {case.after}, which did not run in this invocation"
            reason = (
                reason
                or stage_skip_reason(case, args.stage)
                or portal_skip_reason(case, args.repo)
            )
            label = f"{case.id} {case.name}"
            if reason:
                print(f"{label}: SKIP - {reason}")
                summary.append(f"{label}: skip ({reason})")
                for companion in companions:
                    summary.append(
                        f"{companion.id} {companion.name}: skip (same run as {case.id})"
                    )
                continue
            for repeat in range(1, args.repeat + 1):
                args.current_repeat = repeat
                print(f"\n{label} (repeat {repeat}/{args.repeat})")
                first = run_attempt(case, companions, 1, args, out)
                attempts = [first]
                involved = [case, *companions]
                retry = any(first.failed(c.id) for c in involved) and not first.refused
                if retry:
                    attempts.append(run_attempt(case, companions, 2, args, out))
                for attempt in attempts:
                    invocation_spend += sum(r.cost for r in attempt.runs)
                    for current in involved:
                        _print_attempt(current.id, attempt, current.name)
                ran.add(case.id)
                ran_portal = ran_portal or case.kind == "P"
                for current in involved:
                    fails = [a.failed(current.id) for a in attempts]
                    all_failed = all(fails)
                    tag = (
                        "FAIL"
                        if all_failed
                        else ("flaky pass" if any(fails) else "pass")
                    )
                    if (
                        current.checks
                        and all(c.record for c in current.checks)
                        and not attempts[-1].error
                    ):
                        tag = "recorded"
                    if attempts[-1].refused:
                        tag = "refused"
                    cost = (
                        sum(r.cost for a in attempts for r in a.runs)
                        if current is case
                        else None
                    )
                    cost_text = "" if cost is None else f" ${cost:.2f}"
                    summary.append(
                        f"{current.id} {current.name}: {tag}{cost_text} ({len(attempts)} attempt(s))"
                    )
                    if tag in {"FAIL", "refused"}:
                        failed = True
    finally:
        if ran_portal:
            _cleanup_portal_plans(args.repo)
    print("\nSummary")
    for line in summary:
        print(f"  {line}")
    print(
        f"spent this invocation: ${invocation_spend:.2f}; "
        f"ledger total: ${ledger_total(args.ledger):.2f} of ${args.max_total_usd:.2f}"
    )
    (out / "report.json").write_text(
        json.dumps({"summary": summary}, indent=2), encoding="utf-8"
    )
    print(f"report: {out / 'report.json'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
