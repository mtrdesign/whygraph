"""Auto-rescan git hooks, installed by the WhyGraph portal.

Installs ``post-commit`` / ``post-merge`` / ``post-rewrite`` /
``post-checkout`` hooks that ask the running portal to queue an
incremental, offline rescan (git history + CodeGraph, no LLM, no remote)
whenever the developer commits, pulls, rebases, or switches branch - so
the WhyGraph and CodeGraph databases stay current without a manual scan.
The helper **never scans locally**: it reads the slug and port from
``.whygraph/portal.env`` (parsed, never sourced), POSTs
``{"trigger": "hook"}`` to ``/api/projects/<slug>/scans`` with ``curl``,
and exits. Coalescing is the portal runner's job, so there is no lock.
When the portal is down, ``curl`` is missing, or ``portal.env`` is
missing, tracked by git or invalid, it appends one line to
``.whygraph/logs/hooks.log`` and exits ``0``; the portal's catch-up scan
picks the commits up when it next starts.

The hooks are thin dispatchers that exec a shared helper kept in the git
directory (``<git-common-dir>/whygraph/whygraph-scan``, resolved by the
dispatcher at run time so linked worktrees share it); the POST runs
detached, so commits return instantly. The helper deliberately lives
**outside the work tree**: ``.whygraph/`` is gitignored, and git silently
overwrites an ignored file when a checkout or merge brings in a tracked
file at the same path, so a helper under ``.whygraph/`` could be replaced
by a hostile upstream commit and then run by ``post-merge``. Checkout never
writes into the git directory. The helper also never writes through a
symbolic link (``scan.pending``, ``hooks.log``, ``.whygraph`` or
``.whygraph/logs``) and ignores a ``portal.env`` reached through one. ``post-checkout`` is the one hook git invokes with
arguments, so the dispatcher forwards ``"$@"`` and the helper filters out
the two cases that cannot have changed the tree - before it touches
anything under ``.whygraph/``.

Hook coverage is governed by ``[scan].hooks`` and reconciled by the
portal (Initialize, a ``[scan].hooks`` change, project removal) - see
:func:`sync_hooks`. Managed content lives between sentinel comments, so a
pre-existing foreign hook is appended to, not overwritten.

This is a top-level module (like ``agents.py`` and ``assets.py``) rather
than part of the portal package: it is Click- and FastAPI-free.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

SENTINEL = "# >>> whygraph managed >>>"
SENTINEL_END = "# <<< whygraph managed <<<"

HELPER_GIT_RELPATH = Path("whygraph") / "whygraph-scan"
"""Location of the shared helper, relative to ``git rev-parse --git-common-dir``."""

LEGACY_HELPER_RELPATH = Path(".whygraph") / "hooks" / "whygraph-scan"
"""Where earlier builds wrote the helper (in the work tree); removed by :func:`sync_hooks`."""

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
# whygraph auto-rescan helper (managed by the WhyGraph portal).
# After a commit/merge/rebase/checkout, asks the running portal to queue an
# offline rescan (git history + CodeGraph, no LLM, no remote). It never scans
# locally and takes no lock: the portal coalesces. Portal down, curl missing or
# .whygraph/portal.env missing / tracked / invalid -> one line in
# .whygraph/logs/hooks.log, exit 0. Re-created whenever the portal initializes
# this repo; edits are lost.
root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
# post-checkout is the only hook invoked with 3 args: <prev> <new> <is-branch>.
if [ "$#" -eq 3 ]; then
  [ "$3" = "1" ] || exit 0    # file checkout (`git checkout -- path`) - nothing changed
  [ "$1" != "$2" ] || exit 0  # `git switch -c` at the same commit - identical tree
fi
wg="$root/.whygraph"
log="$wg/logs/hooks.log"
# Nothing is ever written or read through a symbolic link: a committed one
# could point anywhere.
note() {
  if [ ! -L "$wg" ] && [ ! -L "$wg/logs" ] && [ ! -L "$log" ]; then
    mkdir -p "$wg/logs" 2>/dev/null &&
      printf '%s whygraph hook: %s\\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "$log" 2>/dev/null
  fi
  exit 0
}
[ -L "$wg" ] && exit 0
env_file="$wg/portal.env"
[ -L "$env_file" ] && note ".whygraph/portal.env is a symbolic link; ignored"
[ -f "$env_file" ] || note "no .whygraph/portal.env; initialize this repo in the WhyGraph portal"
if git -C "$root" ls-files --error-unmatch .whygraph/portal.env >/dev/null 2>&1; then
  note ".whygraph/portal.env is tracked by git; ignored"
fi
# Parsed, never sourced: only these two values are read, then validated.
slug=$(sed -n 's/^slug=//p' "$env_file" | head -n 1)
port=$(sed -n 's/^port=//p' "$env_file" | head -n 1)
case "$slug" in
  ""|*[!abcdefghijklmnopqrstuvwxyz0123456789-]*) note "invalid slug in .whygraph/portal.env" ;;
esac
case "$port" in
  ""|*[!0123456789]*) note "invalid port in .whygraph/portal.env" ;;
esac
[ -L "$wg/scan.pending" ] || : > "$wg/scan.pending"
(
  command -v curl >/dev/null 2>&1 || note "curl not found; scan not requested"
  # --noproxy: a loopback request must never go through an HTTP(S)_PROXY.
  curl -fsS -m 2 --noproxy '*' -X POST -H 'X-WhyGraph-Client: 1' \\
    -H 'Content-Type: application/json' -d '{"trigger":"hook"}' \\
    "http://127.0.0.1:$port/api/projects/$slug/scans" >/dev/null 2>&1 \\
    || note "portal not reachable on port $port; it scans this commit when it starts"
) </dev/null >/dev/null 2>&1 &
exit 0
"""

_HOOK_BLOCK = (
    f"{SENTINEL}\n"
    'helper="$(git rev-parse --git-common-dir 2>/dev/null)/whygraph/whygraph-scan"\n'
    '[ -x "$helper" ] && "$helper" "$@"\n'
    f"{SENTINEL_END}\n"
)
"""The dispatcher block written into each git hook file.

Resolves the helper under the git common dir at run time (git runs a
hook from the work-tree root, so a relative ``.git`` resolves correctly;
a linked worktree gets the main repo's common dir). Forwards ``"$@"``
because ``post-checkout`` carries
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
    rather than orphaning them. Writes the shared helper (into the git
    common dir, see :func:`helper_path`) when ``names`` is non-empty and
    deletes it when empty; ``sync_hooks(root, ())`` is therefore the
    uninstall. Either way it removes a helper an earlier build left in the
    work tree (:data:`LEGACY_HELPER_RELPATH`), unless ``.whygraph`` or
    ``.whygraph/hooks`` is a symbolic link. Foreign hook content is never
    touched.

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
    target = helper_path(project_root)
    wanted = set(names)

    helper: Path | None = None
    try:
        _remove_legacy_helper(project_root)
        if wanted:
            hooks_dir.mkdir(parents=True, exist_ok=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            # Written beside, then renamed over: never through a symlink.
            tmp = target.with_name(target.name + ".tmp")
            tmp.unlink(missing_ok=True)
            tmp.write_text(_HELPER_SCRIPT)
            tmp.chmod(0o755)
            tmp.replace(target)
            helper = target

        actions: dict[str, str] = {}
        for name in HOOK_NAMES:
            path = hooks_dir / name
            if name in wanted:
                actions[name] = _install_hook(path)
            else:
                actions[name] = "removed" if _uninstall_hook(path) else "absent"

        if not wanted:
            target.unlink(missing_ok=True)
            try:
                target.parent.rmdir()
            except OSError:
                pass  # not empty, or already gone
    except OSError as exc:
        raise HooksError(f"cannot write git hooks under {hooks_dir}: {exc}") from exc

    return HooksResult(helper=helper, actions=actions)


def helper_path(project_root: Path) -> Path:
    """Return where the shared helper lives for this repository.

    ``<git-common-dir>/whygraph/whygraph-scan`` - inside the git directory,
    which checkout and merge never write to, and shared by linked worktrees.

    Parameters
    ----------
    project_root : Path
        The repository working tree.

    Returns
    -------
    Path
        Absolute path of the helper (it may not exist).

    Raises
    ------
    HooksError
        If ``git`` reports the directory is not a work tree.
    """
    return _git_path(project_root, "--git-common-dir") / HELPER_GIT_RELPATH


def managed_hook_names(project_root: Path) -> tuple[str, ...]:
    """Return the hooks that currently carry the whygraph managed block.

    Read-only; used to report existing 1.x auto-rescan hooks when a repo
    is added to the portal.

    Parameters
    ----------
    project_root : Path
        Repository root.

    Returns
    -------
    tuple[str, ...]
        Names from :data:`HOOK_NAMES`, in that order. Empty when the
        directory is not a git work tree or no hook is managed.
    """
    try:
        hooks_dir = _git_hooks_dir(project_root)
    except HooksError:
        return ()
    found = []
    for name in HOOK_NAMES:
        path = hooks_dir / name
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if SENTINEL in text:
            found.append(name)
    return tuple(found)


def _git_hooks_dir(project_root: Path) -> Path:
    """Resolve the repository's hooks directory (worktree-aware).

    Uses ``git rev-parse --git-path hooks`` so the result is correct for
    linked worktrees and a custom ``core.hooksPath``.

    Raises
    ------
    HooksError
        If ``git`` reports the directory is not a work tree.
    """
    return _git_path(project_root, "--git-path", "hooks")


def _git_path(project_root: Path, *args: str) -> Path:
    """Run ``git rev-parse <args>`` in ``project_root``; return the path, made absolute."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", *args],
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


def _remove_legacy_helper(project_root: Path) -> None:
    """Delete the work-tree helper of earlier builds, never through a symlinked dir."""
    wg = project_root / ".whygraph"
    hooks = project_root / LEGACY_HELPER_RELPATH.parent
    if wg.is_symlink() or hooks.is_symlink() or not hooks.is_dir():
        return
    (project_root / LEGACY_HELPER_RELPATH).unlink(missing_ok=True)
    try:
        hooks.rmdir()
    except OSError:
        pass  # something else lives there; leave it


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
    "HELPER_GIT_RELPATH",
    "HOOK_NAMES",
    "LEGACY_HELPER_RELPATH",
    "SENTINEL",
    "SENTINEL_END",
    "HooksError",
    "HooksResult",
    "helper_path",
    "managed_hook_names",
    "resolve_hook_names",
    "sync_hooks",
]
