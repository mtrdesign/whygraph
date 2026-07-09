"""The ``whygraph hooks`` command group — opt-in auto-rescan git hooks.

Installs ``post-commit`` / ``post-merge`` / ``post-rewrite`` hooks that run
an incremental, offline ``whygraph scan`` (git history + CodeGraph, no LLM,
no remote) in the background whenever the developer adds commits — so the
WhyGraph and CodeGraph databases stay current without a manual scan or a
long-running daemon.

The hooks are thin dispatchers that exec a shared helper
(``.whygraph/hooks/whygraph-scan``); the helper detaches the scan so commits
return instantly, and uses a portable ``mkdir`` lock plus a ``pending`` flag
so overlapping commits neither stack nor drop the latest ``HEAD``.

Everything is **opt-in** (never installed by a bare ``whygraph init``) and
**non-clobbering** — managed content lives between sentinel comments, so a
pre-existing foreign hook is appended to, not overwritten.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import click

from ..console import console

SENTINEL = "# >>> whygraph managed >>>"
SENTINEL_END = "# <<< whygraph managed <<<"

HELPER_RELPATH = Path(".whygraph") / "hooks" / "whygraph-scan"
"""Location of the shared helper, relative to the repo root."""

HOOK_NAMES = ("post-commit", "post-merge", "post-rewrite")
"""Git hooks that fire when commits land locally, via merge/pull, or via rebase/amend."""

_HELPER_SCRIPT = """\
#!/bin/sh
# whygraph auto-rescan helper (managed by `whygraph hooks install`).
# After a commit/merge/rebase, runs an incremental, offline scan — git
# history + CodeGraph, no LLM, no remote — detached so the commit returns
# immediately. Single-flight + coalescing so rapid commits don't stack and
# the latest HEAD is never missed. Re-created on reinstall; edits are lost.
command -v whygraph >/dev/null 2>&1 || exit 0
root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
mkdir -p "$root/.whygraph/logs"
lock="$root/.whygraph/scan.lock"
pending="$root/.whygraph/scan.pending"
log="$root/.whygraph/logs/hooks.log"
: > "$pending"
(
  cd "$root" || exit 0
  while [ -e "$pending" ]; do
    if mkdir "$lock" 2>/dev/null; then
      trap 'rmdir "$lock" 2>/dev/null' EXIT INT TERM
      rm -f "$pending"
      whygraph scan --skip-analyze --no-remote >> "$log" 2>&1
      rmdir "$lock" 2>/dev/null
      trap - EXIT INT TERM
    else
      # Another run holds the lock; it will see the re-armed pending flag.
      break
    fi
  done
) </dev/null >/dev/null 2>&1 &
exit 0
"""

_HOOK_BLOCK = (
    f"{SENTINEL}\n"
    'helper="$(git rev-parse --show-toplevel 2>/dev/null)/.whygraph/hooks/whygraph-scan"\n'
    '[ -x "$helper" ] && "$helper"\n'
    f"{SENTINEL_END}\n"
)
"""The dispatcher block written into each git hook file."""

_BLOCK_RE = re.compile(
    re.escape(SENTINEL) + r".*?" + re.escape(SENTINEL_END) + r"\n?",
    re.DOTALL,
)


@click.group(name="hooks")
def hooks_cmd() -> None:
    """Manage opt-in git hooks that auto-rescan on new commits."""


@hooks_cmd.command(name="install")
def install_cmd() -> None:
    """Install the auto-rescan hooks into the current repository.

    Idempotent and non-clobbering: writes the shared helper and adds a
    sentinel-guarded dispatcher to each of :data:`HOOK_NAMES`, refreshing
    an existing managed block in place or appending to a foreign hook.

    Raises
    ------
    click.ClickException
        If the current directory is not inside a git work tree.
    """
    project_root = Path.cwd()
    hooks_dir = _git_hooks_dir(project_root)
    hooks_dir.mkdir(parents=True, exist_ok=True)

    helper = project_root / HELPER_RELPATH
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text(_HELPER_SCRIPT)
    helper.chmod(0o755)
    console.print(f"Wrote rescan helper: {helper}")

    for name in HOOK_NAMES:
        action = _install_hook(hooks_dir / name)
        console.print(f"  {name}: {action}")

    console.print(
        "Auto-rescan hooks installed — new commits refresh WhyGraph + CodeGraph "
        "in the background (git + CodeGraph only; run `whygraph scan` for PRs/issues "
        "+ LLM descriptions). Remove with `whygraph hooks uninstall`."
    )


@hooks_cmd.command(name="uninstall")
def uninstall_cmd() -> None:
    """Remove the auto-rescan hooks, leaving any foreign hook content intact.

    Raises
    ------
    click.ClickException
        If the current directory is not inside a git work tree.
    """
    project_root = Path.cwd()
    hooks_dir = _git_hooks_dir(project_root)

    removed_any = False
    for name in HOOK_NAMES:
        if _uninstall_hook(hooks_dir / name):
            console.print(f"  {name}: removed managed block")
            removed_any = True

    helper = project_root / HELPER_RELPATH
    if helper.exists():
        helper.unlink()
        console.print(f"Removed rescan helper: {helper}")
        removed_any = True

    console.print(
        "Auto-rescan hooks uninstalled."
        if removed_any
        else "No WhyGraph hooks were installed."
    )


@hooks_cmd.command(name="status")
def status_cmd() -> None:
    """Report whether the auto-rescan hooks are installed.

    Raises
    ------
    click.ClickException
        If the current directory is not inside a git work tree.
    """
    project_root = Path.cwd()
    hooks_dir = _git_hooks_dir(project_root)

    console.print(f"Hooks dir: {hooks_dir}")
    helper = project_root / HELPER_RELPATH
    console.print(
        f"Helper:    {'present' if helper.exists() else 'missing'} ({helper})"
    )
    for name in HOOK_NAMES:
        hp = hooks_dir / name
        if hp.exists() and SENTINEL in hp.read_text():
            state = "managed"
        elif hp.exists():
            state = "present (not managed by whygraph)"
        else:
            state = "missing"
        console.print(f"  {name}: {state}")


def _git_hooks_dir(project_root: Path) -> Path:
    """Resolve the repository's hooks directory (worktree-aware).

    Uses ``git rev-parse --git-path hooks`` so the result is correct for
    linked worktrees and a custom ``core.hooksPath``.

    Raises
    ------
    click.ClickException
        If ``git`` reports the directory is not a work tree.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-path", "hooks"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise click.ClickException(
            "not a git repository (run `whygraph hooks` from inside a repo)"
        ) from exc
    p = Path(result.stdout.strip())
    return p if p.is_absolute() else (project_root / p)


def _install_hook(hook_path: Path) -> str:
    """Write or refresh the managed dispatcher in one hook file; return the action taken."""
    if not hook_path.exists():
        hook_path.write_text("#!/bin/sh\n" + _HOOK_BLOCK)
        hook_path.chmod(0o755)
        return "created"

    text = hook_path.read_text()
    if SENTINEL in text:
        hook_path.write_text(_BLOCK_RE.sub(_HOOK_BLOCK, text))
        hook_path.chmod(0o755)
        return "refreshed managed block"

    sep = "" if text.endswith("\n") else "\n"
    hook_path.write_text(text + sep + _HOOK_BLOCK)
    hook_path.chmod(0o755)
    return "appended to existing hook"


def _uninstall_hook(hook_path: Path) -> bool:
    """Strip the managed block from one hook file; return ``True`` if anything changed.

    If removing the block leaves only a bare ``#!/bin/sh`` shebang (i.e. the
    hook was created by WhyGraph), the file is deleted; otherwise the
    foreign remainder is kept.
    """
    if not hook_path.exists():
        return False
    text = hook_path.read_text()
    if SENTINEL not in text:
        return False

    stripped = _BLOCK_RE.sub("", text)
    if stripped.strip() in ("", "#!/bin/sh"):
        hook_path.unlink()
    else:
        hook_path.write_text(stripped)
        hook_path.chmod(0o755)
    return True


__all__ = ["hooks_cmd"]
