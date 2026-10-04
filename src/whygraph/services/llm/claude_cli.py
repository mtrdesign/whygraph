"""Anthropic Claude via the ``claude --print`` CLI (subscription billing).

Wraps the same subprocess invocation that lived in
``whygraph.llm_subprocess.invoke_claude`` before this iteration:
lean flag set (no MCP, tools, slash commands, or session persistence),
optional system-prompt routing, optional API-key injection, optional
Claude Code profile selection (``CLAUDE_CONFIG_DIR``), an optional
long-lived subscription token (``CLAUDE_CODE_OAUTH_TOKEN``, how the
portal's Docker image runs it), and the four error shapes (missing CLI,
timeout, non-zero exit, empty output).

Useful when you have a Claude Code subscription and prefer to bill
against it rather than via the Anthropic API.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from whygraph.core.config import ClaudeCliConfig

from .client import LlmClient
from .exceptions import LlmAuthError, LlmError
from .types import CompletionRequest, CompletionResponse

# Flags passed to every ``claude --print`` invocation. Trims the agent
# runtime of work the prompt doesn't need: MCP servers, tool init,
# slash command/skill discovery, on-disk session persistence.
# Cuts cold start ~40-50% in this repo's measurements.
_LEAN_FLAGS: tuple[str, ...] = (
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--tools",
    "",
    "--disable-slash-commands",
    "--no-session-persistence",
)

_AUTH_FAILURES: tuple[str, ...] = (
    "Failed to authenticate",
    "OAuth access token",
    "Invalid API key",
    "API Error: 401",
)
"""``claude`` stderr that means the credentials, not the request, are bad."""

TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
"""Where the CLI reads a ``claude setup-token`` subscription token."""

_WITHHELD_ENV = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "WHYGRAPH_DATABASE_URL",
        "WHYGRAPH_DATABASE_PASSWORD_FILE",
        "WHYGRAPH_GITHUB_TOKEN_FILE",
    }
)
"""Ambient variables ``claude`` never inherits (the key is re-added only when given)."""


class ClaudeCliAdapter(LlmClient):
    """``claude --print`` adapter.

    Parameters
    ----------
    model : str, optional
        Claude model identifier. Default ``"claude-opus-4-7"``.
    api_key : str, optional
        ``None`` (default) strips ``ANTHROPIC_API_KEY`` from the
        subprocess env so the CLI falls through to subscription
        billing. Passing a value exports it as ``ANTHROPIC_API_KEY``
        (API billing).
    timeout_sec : int, optional
        Per-call timeout in seconds. Default ``120``.
    config_dir : Path, optional
        Claude Code profile directory, exported as ``CLAUDE_CONFIG_DIR``
        so the call bills against that profile's login. ``None``
        (default) inherits the ambient ``CLAUDE_CONFIG_DIR``. Must exist —
        otherwise the CLI would silently create a fresh, logged-out
        profile.
    oauth_token : str, optional
        A ``claude setup-token`` subscription token, exported as
        ``CLAUDE_CODE_OAUTH_TOKEN``. ``None`` (default) inherits an ambient
        one (the portal's scan child gets it that way).

    Notes
    -----
    A call authenticated by a token (given or ambient) and no
    ``config_dir`` runs with a private, throw-away ``CLAUDE_CONFIG_DIR``:
    the analyze phase runs several ``claude`` processes at once, which
    would otherwise share (and race on) one ``~/.claude.json`` - in the
    container that is ``/tmp`` for every project. The auto-updater is
    always off: a describe call is no time to update the CLI, and the
    image's copy is pinned.
    """

    provider = "claude-cli"

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-7",
        api_key: str | None = None,
        timeout_sec: int = 120,
        config_dir: Path | None = None,
        oauth_token: str | None = None,
    ) -> None:
        super().__init__(model=model)
        self._api_key = api_key
        self._default_timeout = timeout_sec
        self._config_dir = config_dir
        self._oauth_token = oauth_token

    @classmethod
    def from_config(
        cls,
        config: ClaudeCliConfig,
        **overrides: Any,
    ) -> "ClaudeCliAdapter":
        """Build an adapter from a typed :class:`ClaudeCliConfig` section.

        ``overrides`` are forwarded to the constructor verbatim. The
        adapter has no injectable third-party SDK client, so this is
        primarily a hook for future use.
        """
        return cls(
            model=config.model,
            api_key=config.api_key,
            timeout_sec=config.timeout_sec,
            config_dir=config.config_dir,
            oauth_token=config.oauth_token,
            **overrides,
        )

    @staticmethod
    def is_available() -> bool:
        """``True`` iff the ``claude`` CLI is on the current ``PATH``."""
        return shutil.which("claude") is not None

    def preflight(self) -> None:
        """Fail once, with an actionable message, when ``claude`` cannot run.

        Checks what every call needs - the ``claude`` binary on ``PATH``,
        the ``config_dir`` profile when set, and inside the Docker image
        (which has no logged-in profile) a subscription token or an API
        key - so a scan skips the analyze phase with one message rather
        than failing every commit.

        Raises
        ------
        LlmError
            If the binary is missing (worded for the Docker image when
            running inside it), ``config_dir`` does not exist, or the
            image has no credential for it.
        """
        if not self.is_available():
            if os.environ.get("WHYGRAPH_IN_IMAGE") == "1":
                raise LlmError(
                    "the WhyGraph Docker image does not include the claude CLI - "
                    "use a native install or another provider ([llm].model)"
                )
            raise LlmError("claude CLI not found on PATH (provider claude-cli)")
        if self._config_dir is not None and not self._config_dir.is_dir():
            hint = ""
            home = os.environ.get("HOME", "")
            if home == "/tmp" and self._config_dir.is_relative_to("/tmp"):
                hint = " (~ expanded to /tmp: HOME is /tmp inside the container)"
            raise LlmError(
                f"claude config_dir {self._config_dir} does not exist "
                f"(check [llm.claude_cli].config_dir){hint}"
            )
        if (
            os.environ.get("WHYGRAPH_IN_IMAGE") == "1"
            and self._config_dir is None
            and not (self._oauth_token or os.environ.get(TOKEN_ENV) or self._api_key)
        ):
            raise LlmError(
                "claude-cli needs your Claude subscription token: run "
                "`claude setup-token` on your machine and paste the token under "
                "Settings > Keys in the portal"
            )

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        system_parts = [m.content for m in request.messages if m.role == "system"]
        user_parts = [m.content for m in request.messages if m.role == "user"]
        if not user_parts:
            raise LlmError("ClaudeCliAdapter requires at least one user message")
        system_prompt = "\n\n".join(system_parts) if system_parts else None
        stdin_payload = "\n\n".join(user_parts)
        timeout = request.timeout_sec or self._default_timeout

        env = {k: v for k, v in os.environ.items() if k not in _WITHHELD_ENV}
        env["DISABLE_AUTOUPDATER"] = "1"
        if self._api_key:
            env["ANTHROPIC_API_KEY"] = self._api_key
        if self._oauth_token:
            env[TOKEN_ENV] = self._oauth_token
        private_dir: str | None = None
        if self._config_dir is not None:
            if not self._config_dir.is_dir():
                raise LlmError(
                    f"claude config_dir {self._config_dir} does not exist "
                    "(check [llm.claude_cli].config_dir)"
                )
            env["CLAUDE_CONFIG_DIR"] = str(self._config_dir)
        elif env.get(TOKEN_ENV):
            private_dir = tempfile.mkdtemp(prefix="whygraph-claude-")
            env["CLAUDE_CONFIG_DIR"] = private_dir
        try:
            return self._run(system_prompt, stdin_payload, timeout, env)
        finally:
            if private_dir is not None:
                shutil.rmtree(private_dir, ignore_errors=True)

    def _run(
        self,
        system_prompt: str | None,
        stdin_payload: str,
        timeout: int,
        env: dict[str, str],
    ) -> CompletionResponse:

        cmd = ["claude", "--print", "--model", self.model, *_LEAN_FLAGS]
        if system_prompt is not None:
            cmd.extend(["--system-prompt", system_prompt])

        try:
            result = subprocess.run(
                cmd,
                input=stdin_payload,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
                env=env,
            )
        except FileNotFoundError as exc:
            raise LlmError("claude CLI is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise LlmError(f"claude timed out after {timeout}s") from exc

        if result.returncode != 0:
            stderr = (result.stderr or "").strip() or (result.stdout or "").strip()
            if any(marker in stderr for marker in _AUTH_FAILURES):
                hint = (
                    " - the Claude subscription token was rejected: run "
                    "`claude setup-token` again and replace it under Settings"
                    if env.get(TOKEN_ENV)
                    else ""
                )
                raise LlmAuthError(f"claude could not authenticate: {stderr}{hint}")
            raise LlmError(f"claude exited {result.returncode}: {stderr}")
        text = (result.stdout or "").strip()
        if not text:
            raise LlmError("claude returned empty output")

        return CompletionResponse(
            text=text,
            model=self.model,
            provider=self.provider,
        )
