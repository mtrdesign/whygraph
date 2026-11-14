"""Summarise plan 54's rationale benchmark from a report directory's ``results.jsonl``.

``run.py`` appends one line per attempt and case to ``<out>/results.jsonl``.
This script reads the lines of the benchmark cases and prints, per scenario and
arm, how many runs kept the constraint (the hidden test passed), read the card
and read history, then the verdict of the rule that plan 54 section 5.2 set
before any run:

* ``helps``: the history arm keeps the constraint in at least 4 of 5 runs and
  the blank arm in at most 2 of 5.
* ``no effect``: the two arms differ by at most 1 run of 5.
* ``mixed``: anything else.

With another number of runs the same shares apply (4 of 5 is 80 %, 2 of 5 is
40 %, 1 of 5 is 20 %). The benchmark says **the why helps** when at least two
scenarios are ``helps`` and none has the blank arm ahead, **no effect** when all
three are ``no effect``, and **mixed** otherwise.

Usage
-----
From the repository root::

    uv run python scripts/agent_evals/whybench.py plans/54-eval/benchmark

Calls no model. Exit 0 when every scenario has runs in both arms, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Scenario -> (history-arm case, blank-arm case).
SCENARIOS = {"S1": ("R1", "R4"), "S2": ("R2", "R5"), "S3": ("R3", "R6")}
PLAN_CASES = {"R7": "history", "R8": "blank"}


@dataclass
class Arm:
    """The runs of one case: what each kept, read and cost."""

    runs: int = 0
    kept: int = 0
    card: int = 0
    history: int = 0
    errors: int = 0
    # (repeat, kept, card, history) per run, for the trace questions.
    rows: list[tuple[int, bool, bool, bool]] = field(default_factory=list)


def _check(line: dict, prefix: str) -> bool | None:
    """Return the ``ok`` of the first check whose name starts with ``prefix``."""
    for check in line.get("checks", []):
        if str(check.get("name", "")).startswith(prefix):
            return bool(check.get("ok"))
    return None


def load(path: Path) -> list[dict]:
    """Return the JSON lines of ``results.jsonl``; a bad line is skipped with a warning."""
    lines = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            lines.append(json.loads(raw))
        except json.JSONDecodeError:
            print(f"warning: {path}:{number} is not JSON, skipped", file=sys.stderr)
    return lines


def arms(lines: list[dict]) -> dict[str, Arm]:
    """Group the first attempt of each benchmark run by case id."""
    out: dict[str, Arm] = {}
    for line in lines:
        case = str(line.get("case", ""))
        if not case.startswith("R") or line.get("attempt") != 1:
            continue
        arm = out.setdefault(case, Arm())
        if line.get("error"):
            arm.errors += 1
            continue
        kept = _check(line, "hidden test") or _check(line, "plan keeps") or False
        card = bool(_check(line, "card read"))
        history = bool(_check(line, "history read"))
        arm.runs += 1
        arm.kept += kept
        arm.card += card
        arm.history += history
        arm.rows.append((int(line.get("repeat", 0)), kept, card, history))
    return out


def scenario_verdict(history: Arm, blank: Arm) -> str:
    """Apply the section 5.2 rule to one scenario's two arms."""
    h = history.kept / history.runs
    b = blank.kept / blank.runs
    if h >= 0.8 and b <= 0.4:
        return "helps"
    if abs(h - b) <= 0.2 + 1e-9:
        return "no effect"
    return "mixed"


def overall(verdicts: dict[str, str], blank_ahead: set[str]) -> str:
    """Apply the section 5.2 rule to the three scenario verdicts."""
    helps = sum(v == "helps" for v in verdicts.values())
    if helps >= 2 and not blank_ahead:
        return "the why helps"
    if all(v == "no effect" for v in verdicts.values()):
        return "no effect"
    return "mixed"


def main(argv: list[str] | None = None) -> int:
    """Print the benchmark table, the verdict and the trace answers."""
    parser = argparse.ArgumentParser(description="Summarise the rationale benchmark.")
    parser.add_argument("out", type=Path, help="The report directory of the round.")
    args = parser.parse_args(argv)
    path = args.out / "results.jsonl"
    if not path.is_file():
        print(f"no {path}: run the R cases with --out {args.out} first")
        return 1
    by_case = arms(load(path))

    print("| Scenario | Arm | Runs | Kept | Read the card | Read history | Errors |")
    print("|---|---|---|---|---|---|---|")
    verdicts: dict[str, str] = {}
    blank_ahead: set[str] = set()
    missing = []
    for scenario, (h_id, b_id) in SCENARIOS.items():
        pair = []
        for label, case_id in (("history", h_id), ("blank", b_id)):
            arm = by_case.get(case_id, Arm())
            pair.append(arm)
            print(
                f"| {scenario} | {label} ({case_id}) | {arm.runs} | {arm.kept} | "
                f"{arm.card} | {arm.history} | {arm.errors} |"
            )
        if not (pair[0].runs and pair[1].runs):
            missing.append(scenario)
            continue
        verdicts[scenario] = scenario_verdict(*pair)
        if pair[1].kept / pair[1].runs > pair[0].kept / pair[0].runs:
            blank_ahead.add(scenario)

    print()
    for scenario, verdict in verdicts.items():
        ahead = " (blank arm ahead)" if scenario in blank_ahead else ""
        print(f"{scenario}: {verdict}{ahead}")
    for scenario in missing:
        print(f"{scenario}: no verdict, an arm has no runs")
    if len(verdicts) == len(SCENARIOS):
        print(f"\nVerdict: {overall(verdicts, blank_ahead)}")

    plans = {c: by_case[c] for c in PLAN_CASES if c in by_case}
    if plans:
        print("\nPlans (R7, R8): runs whose plan names the S1 constraint")
        for case_id, arm in plans.items():
            print(f"  {case_id} ({PLAN_CASES[case_id]}): {arm.kept} of {arm.runs}")

    history_rows = [
        (scenario, row)
        for scenario, (h_id, _) in SCENARIOS.items()
        for row in by_case.get(h_id, Arm()).rows
    ]
    saw = sum(card for _, (_, _, card, _) in history_rows)
    print("\nTrace questions (history arm)")
    print(f"1. Runs that read the card: {saw} of {len(history_rows)}")
    broke = [
        f"{s} repeat {r}" for s, (r, kept, card, _) in history_rows if card and not kept
    ]
    print(
        "2. Read the card and still broke the constraint: "
        + (", ".join(broke) if broke else "none")
        + ". Read those cards in the run's transcript."
    )
    by_log = [
        f"{s} repeat {r}" + (" (also read the card)" if card else "")
        for s, (r, kept, card, hist) in history_rows
        if kept and hist
    ]
    print(
        "3. Kept it and read git history itself: "
        + (", ".join(by_log) if by_log else "none")
    )
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
