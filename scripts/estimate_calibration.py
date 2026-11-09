"""Check the Describe-now estimate against real describe calls on a commit sample.

The estimate (``whygraph.portal.estimate``) was calibrated on one run of one
repository with one model (issue #86). This script measures how far it holds
elsewhere. It samples waiting commits of a project, describes each one with
every model given, exactly as a scan would (``LlmDescriptor`` over
``Repository.diff``), and compares the real per-call usage with what the
estimate predicted for the same commits.

Nothing is written to the project. The script reads a **copy** of the
project's WhyGraph DB and never saves a description. The JSON it writes
holds counts only (SHAs, file and line counts, tokens, calls), never diff or
description text, so it can be kept to refit the constants later.

Deliberately **not** pytest: it calls a paid provider and its results vary,
so it must never gate CI.

Usage
-----
Keys come from the environment (``ANTHROPIC_API_KEY`` and so on). Without
``--yes`` the script only prints the sample and what it would cost::

    SSL_CERT_FILE=/etc/ssl/cert.pem uv run python scripts/estimate_calibration.py \\
        --db ~/.local/state/whygraph-dev/local/data/projects/local/biome/whygraph.db \\
        --repo ~/.local/state/whygraph-dev/repos/biome \\
        --model anthropic:claude-sonnet-5-5 --model anthropic:claude-haiku-4-5 \\
        --sample 30 --out /tmp/biome-calibration.json

    # the same with --yes to spend

Notes
-----
* On a corporate network the LLM SDKs need ``SSL_CERT_FILE=/etc/ssl/cert.pem``
  or every call fails with a bare "Connection error".
* Commits over ``--large`` files are left out of the sample: a scan stubs
  them without an LLM call, so there is nothing to measure.
* **Record the real numbers, including misses.** The point is to find where
  the estimate is wrong.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import statistics
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from whygraph.analyze.llm_descriptor import LlmDescriptor
from whygraph.core.config import Config, LlmConfig
from whygraph.core.context import ProjectContext, use_project
from whygraph.core.usage import UsageRecord, UsageScope, use_usage_sink
from whygraph.portal.estimate import CommitSize, estimate_tokens, waiting_commits
from whygraph.portal.prices import NO_OVERRIDES, price_for
from whygraph.services.git import Repository
from whygraph.services.llm import LlmClientFactory


@dataclass
class _Calls:
    """A usage sink that keeps one commit's calls (counts only)."""

    scope: UsageScope = field(default_factory=lambda: UsageScope(source="scan"))
    calls: list[tuple[int, int]] = field(default_factory=list)

    def record(self, rec: UsageRecord) -> None:
        self.calls.append((rec.input_tokens or 0, rec.output_tokens or 0))

    def blocked_scope(self) -> str | None:
        return None


def _sample(args: argparse.Namespace) -> dict[str, CommitSize]:
    """The sampled waiting commits, sized as the estimate sizes them."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "whygraph.db"
        shutil.copyfile(args.db, copy)
        config = Config(
            whygraph_db=copy, codegraph_db=args.repo / ".codegraph" / "codegraph.db"
        )
        with use_project(
            ProjectContext(slug="calibration", root=args.repo, config=config)
        ):
            waiting = waiting_commits(
                args.repo,
                max_diff_chars=args.max_diff_chars,
                large_commit_file_count=args.large,
            )
    described = sorted(
        sha for sha, size in waiting.items() if size.files_changed <= args.large
    )
    picked = random.Random(args.seed).sample(
        described, min(args.sample, len(described))
    )
    print(
        f"{len(waiting)} commits waiting, {len(described)} would be described; "
        f"sampled {len(picked)} (seed {args.seed})"
    )
    return {sha: waiting[sha] for sha in picked}


def _estimate(size: CommitSize, args: argparse.Namespace) -> dict:
    est = estimate_tokens(
        [size], max_diff_chars=args.max_diff_chars, large_commit_file_count=args.large
    )
    return {"calls": est.calls, "input": est.input_tokens, "output": est.output_tokens}


def _usd(price: object, input_tokens: float, output_tokens: float) -> float:
    if price is None:
        return float("nan")
    return (
        input_tokens * float(price.input) + output_tokens * float(price.output)
    ) / 1_000_000


def _describe_all(
    target: str, sample: dict[str, CommitSize], args: argparse.Namespace
) -> dict[str, dict]:
    """Describe every sampled commit with ``target``; per SHA, its calls or error."""
    provider, _, model = target.partition(":")
    client = LlmClientFactory(LlmConfig()).make(provider, model=model or None)
    client.preflight()
    descriptor = LlmDescriptor(client, max_diff_chars=args.max_diff_chars)
    repository = Repository(args.repo)
    results: dict[str, dict] = {}
    lock = threading.Lock()

    def one(sha: str) -> None:
        sink = _Calls()
        try:
            commit = repository.commit_metadata(sha)
            with use_usage_sink(sink):
                descriptor.describe(repository.diff(commit), subject=sha)
            outcome = {"calls": sink.calls}
        except Exception as exc:  # report it, keep measuring the rest
            outcome = {"calls": sink.calls, "error": f"{type(exc).__name__}: {exc}"}
        with lock:
            results[sha] = outcome
            print(f"  {target} {len(results)}/{len(sample)} {sha[:10]}", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(one, sample))
    return results


def _report(target: str, sample: dict[str, CommitSize], results: dict, args) -> dict:
    """Print one model's comparison; return its summary."""
    provider, _, model = target.partition(":")
    priced = price_for(provider, model, NO_OVERRIDES, custom_endpoint=False)
    price = priced[0] if priced else None
    est_tot = {"calls": 0, "input": 0, "output": 0}
    act_tot = {"calls": 0, "input": 0, "output": 0}
    single_out: list[float] = []
    errors: list[float] = []
    failed = 0
    for sha, size in sample.items():
        outcome = results.get(sha, {})
        if "error" in outcome or not outcome.get("calls"):
            failed += 1
            continue
        est = _estimate(size, args)
        calls = outcome["calls"]
        act = {
            "calls": len(calls),
            "input": sum(c[0] for c in calls),
            "output": sum(c[1] for c in calls),
        }
        for key in est_tot:
            est_tot[key] += est[key]
            act_tot[key] += act[key]
        if len(calls) == 1 and est["calls"] == 1:
            single_out.append(act["output"] / est["output"])
        actual_usd = _usd(price, act["input"], act["output"])
        if actual_usd:
            errors.append(_usd(price, est["input"], est["output"]) / actual_usd - 1)
    print(f"\n{target}: {len(sample) - failed} commits measured, {failed} failed")
    reasons = sorted({o["error"] for o in results.values() if "error" in o})
    for reason in reasons[:3]:
        print(f"  error: {reason[:300]}")
    if failed == len(sample):
        return {"target": target, "failed": failed, "errors": reasons[:3]}
    for key in ("calls", "input", "output"):
        ratio = est_tot[key] / act_tot[key] if act_tot[key] else float("nan")
        print(
            f"  {key:<7} estimate {est_tot[key]:>10,}  actual {act_tot[key]:>10,}"
            f"  estimate/actual {ratio:.2f}"
        )
    est_usd = _usd(price, est_tot["input"], est_tot["output"])
    act_usd = _usd(price, act_tot["input"], act_tot["output"])
    print(
        f"  cost    estimate ${est_usd:.3f}  actual ${act_usd:.3f}"
        f"  error {100 * (est_usd / act_usd - 1):+.1f}%"
    )
    if errors:
        errors.sort()
        print(
            "  per-commit cost error: p10 {:+.0f}%  median {:+.0f}%  p90 {:+.0f}%".format(
                100 * errors[len(errors) // 10],
                100 * statistics.median(errors),
                100 * errors[9 * len(errors) // 10],
            )
        )
    if single_out:
        print(
            f"  single-call output actual/estimate: median "
            f"{statistics.median(single_out):.2f} over {len(single_out)} commits"
        )
    return {
        "target": target,
        "estimate": est_tot,
        "actual": act_tot,
        "estimate_usd": est_usd,
        "actual_usd": act_usd,
        "failed": failed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--db", type=Path, required=True, help="the project's whygraph.db"
    )
    parser.add_argument(
        "--repo", type=Path, required=True, help="the project's repository"
    )
    parser.add_argument(
        "--model",
        action="append",
        required=True,
        help="provider:model to measure (repeatable)",
    )
    parser.add_argument("--sample", type=int, default=30)
    parser.add_argument("--seed", type=int, default=86)
    parser.add_argument("--max-diff-chars", type=int, default=50_000)
    parser.add_argument("--large", type=int, default=30, help="large_commit_file_count")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", type=Path, help="write the per-commit counts as JSON")
    parser.add_argument("--yes", action="store_true", help="spend: call the models")
    args = parser.parse_args()
    args.repo = args.repo.expanduser().resolve()
    args.db = args.db.expanduser().resolve()

    sample = _sample(args)
    if not sample:
        print("nothing to measure")
        return 1
    total_est = {"input": 0, "output": 0, "calls": 0}
    for size in sample.values():
        for key, value in _estimate(size, args).items():
            total_est[key] += value
    print(
        f"estimate for the sample: {total_est['calls']} calls, "
        f"{total_est['input']:,} in, {total_est['output']:,} out"
    )
    for target in args.model:
        provider, _, model = target.partition(":")
        priced = price_for(provider, model, NO_OVERRIDES, custom_endpoint=False)
        cost = _usd(
            priced[0] if priced else None, total_est["input"], total_est["output"]
        )
        print(f"  {target}: about ${cost:.2f}")
    if not args.yes:
        print("\ndry run: pass --yes to call the models")
        return 0

    summaries, commits = [], {}
    for target in args.model:
        results = _describe_all(target, sample, args)
        summaries.append(_report(target, sample, results, args))
        for sha, outcome in results.items():
            commits.setdefault(
                sha,
                {
                    "files_changed": sample[sha].files_changed,
                    "file_lines": list(sample[sha].file_lines),
                    "estimate": _estimate(sample[sha], args),
                    "actual": {},
                },
            )["actual"][target] = outcome
    if args.out:
        args.out.write_text(
            json.dumps(
                {
                    "repo": args.repo.name,
                    "max_diff_chars": args.max_diff_chars,
                    "large_commit_file_count": args.large,
                    "summaries": summaries,
                    "commits": commits,
                },
                indent=1,
            )
            + "\n"
        )
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
