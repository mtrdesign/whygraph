"""Auto-rescan git hooks, installed by ``whygraph init``.

Installs ``post-commit`` / ``post-merge`` / ``post-rewrite`` /
``post-checkout`` hooks that run an incremental, offline ``whygraph scan``
(git history + CodeGraph, no LLM, no remote) in the background whenever
the developer commits, pulls, rebases, or switches branch — so the
WhyGraph and CodeGraph databases stay current without a manual scan or a
long-running daemon.

The hooks are thin dispatchers that exec a shared helper
(``.whygraph/hooks/whygraph-scan``); the helper detaches the scan so
commits return instantly, and uses a portable ``mkdir`` lock plus a
``pending`` flag so overlapping git events neither stack nor drop the
latest ``HEAD``. ``post-checkout`` is the one hook git invokes with
arguments, so the dispatcher forwards ``"$@"`` and the helper filters out
the two cases that cannot have changed the tree.

Hook coverage is governed by ``[scan].hooks`` and reconciled by
``whygraph init`` — see :func:`sync_hooks`. Managed content lives between
sentinel comments, so a pre-existing foreign hook is appended to, not
overwritten.

This is a top-level module (like ``agents.py`` and ``assets.py``) rather
than a CLI command: it is an installed-by-``init`` concern, and it must
not depend on Click.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

SENTINEL = "# >>> whygraph managed >>>"
SENTINEL_END = "# <<< whygraph managed <<<"

HELPER_RELPATH = Path(".whygraph") / "hooks" / "whygraph-scan"
"""Location of the shared helper, relative to the repo root."""

HOOK_NAMES = ("post-commit", "post-merge", "post-rewrite", "post-checkout")
"""Every hook WhyGraph manages — the reconcile set.

:func:`sync_hooks` considers **all** of these on every call, installing
the ones it is given and stripping the managed block from the rest. The
four cover every git event that can change the worktree or add commits:
``post-commit`` (commit, amend), ``post-merge`` (pull, merge),
``post-rewrite`` (rebase), ``post-checkout`` (branch switch).
"""

_HELPER_SCRIPT = """\
#!/bin/sh
# whygraph auto-rescan helper (managed by `whygraph init`).
# After a commit/merge/rebase/checkout, runs an incremental, offline scan —
# git history + CodeGraph, no LLM, no remote — detached so the git command
# returns immediately. Single-flight + coalescing so rapid commits don't
# stack and the latest HEAD is never missed. Re-created on every
# `whygraph init`; edits are lost.
command -v whygraph >/dev/null 2>&1 || exit 0
root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
# post-checkout is the only hook invoked with 3 args: <prev> <new> <is-branch>.
if [ "$#" -eq 3 ]; then
  [ "$3" = "1" ] || exit 0    # file checkout (`git checkout -- path`) — nothing changed
  [ "$1" != "$2" ] || exit 0  # `git switch -c` at the same commit — identical tree
fi
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
    '[ -x "$helper" ] && "$helper" "$@"\n'
    f"{SENTINEL_END}\n"
)
"""The dispatcher block written into each git hook file.

Forwards ``"$@"`` because ``post-checkout`` carries
``<prev-head> <new-head> <is-branch-checkout>``; the other three hooks
pass zero or one argument and the helper's arg gate ignores those.
"""

_BLOCK_RE = re.compile(
    re.escape(SENTINEL) + r".*?" + re.escape(SENTINEL_END) + r"\n?",
    re.DOTALL,
)


class HooksError(RuntimeError):
    """The hooks directory cannot be resolved or written, or a name is unknown."""


@dataclass(frozen=True)
class HooksResult:
    """What :func:`sync_hooks` did.

    Attributes
    ----------
    helper : Path or None
        Where the shared helper was written, or ``None`` when every hook
        was removed and the helper deleted.
    actions : dict[str, str]
        Per-hook outcome, keyed by hook name and covering all of
        :data:`HOOK_NAMES`. One of ``"created"``, ``"refreshed"``,
        ``"appended"``, ``"removed"``, or ``"absent"``.
    """

    helper: Path | None
    actions: dict[str, str]

    @property
    def installed(self) -> tuple[str, ...]:
        """Hook names that now carry the managed block."""
        return tuple(
            name
            for name, action in self.actions.items()
            if action in ("created", "refreshed", "appended")
        )

    @property
    def removed(self) -> tuple[str, ...]:
        """Hook names whose managed block was stripped by this call."""
        return tuple(
            name for name, action in self.actions.items() if action == "removed"
        )


def resolve_hook_names(value: bool | Sequence[str]) -> tuple[str, ...]:
    """Normalize a ``[scan].hooks`` value to concrete hook names.

    Parameters
    ----------
    value : bool or Sequence[str]
        ``True`` → all of :data:`HOOK_NAMES`; ``False`` or an empty
        sequence → none; a sequence of names → exactly those.

    Returns
    -------
    tuple[str, ...]
        The hooks to keep installed, in :data:`HOOK_NAMES` order so the
        result is stable regardless of how the config listed them.

    Raises
    ------
    HooksError
        If a name is not one of :data:`HOOK_NAMES`. Validation lives here
        rather than in ``core.config`` so the cross-cutting ``core``
        package keeps no dependency on this module.
    """
    if isinstance(value, bool):
        return HOOK_NAMES if value else ()
    unknown = [name for name in value if name not in HOOK_NAMES]
    if unknown:
        raise HooksError(
            f"unknown hook name(s): {', '.join(sorted(unknown))} "
            f"(valid: {', '.join(HOOK_NAMES)})"
        )
    wanted = set(value)
    return tuple(name for name in HOOK_NAMES if name in wanted)


def sync_hooks(project_root: Path, names: Sequence[str]) -> HooksResult:
    """Reconcile the repo's git hooks to exactly ``names``.

    Installs or refreshes the managed block in each named hook, and
    **strips it from every hook in** :data:`HOOK_NAMES` **that is not
    named** — so shrinking the configured list removes the dropped hooks
    rather than orphaning them. Writes the shared helper when ``names``
    is non-empty and deletes it when empty; ``sync_hooks(root, ())`` is
    therefore the uninstall. Foreign hook content is never touched.

    Both directions live in one function deliberately: the removal half
    is the part that is easy to forget on one branch of an
    install/uninstall pair, and folding them together makes omitting it
    structurally impossible.

    Parameters
    ----------
    project_root : Path
        The repository working tree.
    names : Sequence[str]
        Hook names to keep installed — normally the output of
        :func:`resolve_hook_names`.

    Returns
    -------
    HooksResult
        The helper path (or ``None``) and the per-hook action taken.

    Raises
    ------
    HooksError
        If the hooks directory cannot be resolved or written.
    """
    hooks_dir = _git_hooks_dir(project_root)
    wanted = set(names)

    helper: Path | None = None
    try:
        if wanted:
            hooks_dir.mkdir(parents=True, exist_ok=True)
            helper = project_root / HELPER_RELPATH
            helper.parent.mkdir(parents=True, exist_ok=True)
            helper.write_text(_HELPER_SCRIPT)
            helper.chmod(0o755)

        actions: dict[str, str] = {}
        for name in HOOK_NAMES:
            path = hooks_dir / name
            if name in wanted:
                actions[name] = _install_hook(path)
            else:
                actions[name] = "removed" if _uninstall_hook(path) else "absent"

        if not wanted:
            stale = project_root / HELPER_RELPATH
            if stale.exists():
                stale.unlink()
    except OSError as exc:
        raise HooksError(f"cannot write git hooks under {hooks_dir}: {exc}") from exc

    return HooksResult(helper=helper, actions=actions)


def _git_hooks_dir(project_root: Path) -> Path:
    """Resolve the repository's hooks directory (worktree-aware).

    Uses ``git rev-parse --git-path hooks`` so the result is correct for
    linked worktrees and a custom ``core.hooksPath``.

    Raises
    ------
    HooksError
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
        raise HooksError(
            f"not a git repository ({project_root}) — no hooks to manage"
        ) from exc
    p = Path(result.stdout.strip())
    return p if p.is_absolute() else (project_root / p)


def _install_hook(hook_path: Path) -> str:
    """Write or refresh the managed dispatcher in one hook file; return the action."""
    if not hook_path.exists():
        hook_path.write_text("#!/bin/sh\n" + _HOOK_BLOCK)
        hook_path.chmod(0o755)
        return "created"

    text = hook_path.read_text()
    if SENTINEL in text:
        hook_path.write_text(_BLOCK_RE.sub(_HOOK_BLOCK, text))
        hook_path.chmod(0o755)
        return "refreshed"

    sep = "" if text.endswith("\n") else "\n"
    hook_path.write_text(text + sep + _HOOK_BLOCK)
    hook_path.chmod(0o755)
    return "appended"


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


__all__ = [
    "HELPER_RELPATH",
    "HOOK_NAMES",
    "SENTINEL",
    "SENTINEL_END",
    "HooksError",
    "HooksResult",
    "resolve_hook_names",
    "sync_hooks",
]
