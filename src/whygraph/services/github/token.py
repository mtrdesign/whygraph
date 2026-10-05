"""The GitHub token a scan child authenticates with, read fresh for every call.

A production portal hands its scan child a one-hour installation token
through a 0600 file named by
:data:`~whygraph.services.git.credentials.TOKEN_FILE_ENV`, and rewrites
that file before the token expires (M2d-2 plan section 0.2 #5). Every
``gh`` and network ``git`` subprocess the child starts therefore re-reads
the file and gets the token in its own environment. Without the variable
(a local folder, or a headless ``whygraph scan``) nothing changes: the
subprocess inherits the process environment, as before.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from whygraph.services.git.credentials import TOKEN_FILE_ENV

GH_TOKEN_ENV = "GH_TOKEN"
"""The variable ``gh`` reads its token from."""


def github_token(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the GitHub token to use now.

    Parameters
    ----------
    environ : Mapping[str, str], optional
        Where the variables are read. Defaults to :data:`os.environ`.

    Returns
    -------
    str or None
        The content of the file named by ``WHYGRAPH_GITHUB_TOKEN_FILE``
        (read on every call, stripped) when that variable is set - ``None``
        if the file is missing, unreadable or empty -; otherwise the
        environment's ``GH_TOKEN`` / ``GITHUB_TOKEN``, or ``None``.
    """
    source = os.environ if environ is None else environ
    path = source.get(TOKEN_FILE_ENV)
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip() or None
        except OSError:
            return None
    return source.get(GH_TOKEN_ENV) or source.get("GITHUB_TOKEN") or None


def token_env(var: str) -> dict[str, str] | None:
    """The environment for one subprocess that authenticates through ``var``.

    Parameters
    ----------
    var : str
        The variable the subprocess reads its token from: ``GH_TOKEN`` for
        ``gh``, ``WHYGRAPH_GIT_TOKEN`` for git's inline credential helper.

    Returns
    -------
    dict[str, str] or None
        ``None`` (inherit the process environment, as before) when no token
        file is configured; otherwise a copy of :data:`os.environ` with
        ``var`` set to the token the file holds right now (and removed when
        it holds none).
    """
    if not os.environ.get(TOKEN_FILE_ENV):
        return None
    env = dict(os.environ)
    token = github_token(env)
    if token:
        env[var] = token
    else:
        env.pop(var, None)
    return env


__all__ = ["GH_TOKEN_ENV", "github_token", "token_env"]
