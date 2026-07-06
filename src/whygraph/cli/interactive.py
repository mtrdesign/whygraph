"""Interactive ``whygraph init`` prompt flow.

``whygraph init`` becomes a create-next-app-style guided setup: arrow-key
menus for the agent, the analyze/rationale LLMs (+ per-provider API keys),
and the source-control provider (+ GitHub token), followed by a summary
panel that **masks every secret** and a final *"Write these files?"*
confirmation before anything is written.

Every prompt is defaulted so a bare Enter accepts it. The flow is pure
collection — it returns an :class:`~whygraph.core.config.InitAnswers` and
performs **no** file writes; the command owns writing (so a Ctrl-C or a
declined confirm aborts with zero side effects).

The questionary calls sit behind a small :class:`Prompter` protocol so
tests inject a :class:`ScriptedPrompter` without a TTY (mirrors the
``GraphBackend`` protocol reasoning — a second concrete consumer, tests,
justifies the seam).

Notes
-----
The provider→default-model and provider→env-var maps are derived from the
config sub-dataclasses (:mod:`whygraph.core.config`), never re-hardcoded.
Provider tags use the **hyphen** form (``"claude-cli"``) to match the LLM
factory tag written into ``[analyze]/[rationale].provider``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from whygraph.core.config import (
    CONFIG_FILENAME,
    EXAMPLE_CONFIG_FILENAME,
    AnthropicConfig,
    ClaudeCliConfig,
    DeepSeekConfig,
    InitAnswers,
    OllamaConfig,
    OpenAIConfig,
)

# Provider tags (hyphen form — matches the LLM factory tag).
_PROVIDERS = ("anthropic", "openai", "deepseek", "ollama", "claude-cli")

# Provider → its built-in default model, read straight from the config
# sub-dataclasses so the prompt defaults never drift from the real ones.
_PROVIDER_DEFAULT_MODEL: dict[str, str] = {
    "anthropic": AnthropicConfig().model,
    "openai": OpenAIConfig().model,
    "deepseek": DeepSeekConfig().model,
    "ollama": OllamaConfig().model,
    "claude-cli": ClaudeCliConfig().model,
}

# Key-bearing providers and the env var each falls back to when no key is
# typed. ``ollama`` (local) and ``claude-cli`` (subscription billing) carry
# no key, so they never prompt.
_PROVIDER_ENV_VAR: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}

_SCAN_PROVIDERS = ("off", "github", "auto")
_SCAN_ENV_VARS = ("GH_TOKEN", "GITHUB_TOKEN")


class InitAborted(Exception):
    """Raised when the user aborts the interactive init flow.

    Signals either a Ctrl-C / EOF at a prompt (questionary's ``.ask()``
    returns ``None``) or a declined *"Write these files?"* confirmation.
    The command catches it and exits non-zero **before** any file write
    or DB bootstrap, so an abort leaves the project untouched.
    """


@runtime_checkable
class Prompter(Protocol):
    """The four prompt primitives the flow needs.

    A protocol so tests can drive the flow with a scripted stand-in and
    the real implementation can wrap ``questionary`` without the flow
    knowing which is in play. A ``None`` return from any method signals an
    abort (Ctrl-C / EOF); the flow turns it into :class:`InitAborted`.
    """

    def select(self, message: str, choices: list[str], default: str) -> str | None:
        """Arrow-key single choice; ``default`` is pre-selected."""
        ...

    def text(self, message: str, default: str) -> str | None:
        """Free-text entry; a bare Enter yields ``default``."""
        ...

    def password(self, message: str) -> str | None:
        """No-echo secret entry; a bare Enter yields the empty string."""
        ...

    def confirm(self, message: str, *, default: bool) -> bool | None:
        """Yes/No; a bare Enter yields ``default``."""
        ...


class QuestionaryPrompter:
    """Default :class:`Prompter` backed by ``questionary`` (real TTY).

    Each method delegates to the matching ``questionary`` widget and
    returns its ``.ask()`` result, which is ``None`` on Ctrl-C / EOF —
    passed straight through so the flow can abort.
    """

    def select(self, message: str, choices: list[str], default: str) -> str | None:
        import questionary

        return questionary.select(message, choices=choices, default=default).ask()

    def text(self, message: str, default: str) -> str | None:
        import questionary

        return questionary.text(message, default=default).ask()

    def password(self, message: str) -> str | None:
        import questionary

        return questionary.password(message).ask()

    def confirm(self, message: str, *, default: bool) -> bool | None:
        import questionary

        return questionary.confirm(message, default=default).ask()


def _require(value: str | bool | None) -> str | bool:
    """Return ``value`` or abort — turns a ``None`` prompt result into abort."""
    if value is None:
        raise InitAborted("interactive init aborted")
    return value


def _key_status(provider: str, answers: InitAnswers) -> str:
    """Masked status string for one provider's API key (never the value)."""
    if answers.api_keys.get(provider):
        return "set (hidden)"
    env_var = _PROVIDER_ENV_VAR.get(provider)
    if env_var and os.environ.get(env_var):
        return f"from ${env_var}"
    return "not set"


def _scan_token_status(answers: InitAnswers) -> str:
    """Masked status string for the scan token (never the value)."""
    if answers.scan_token:
        return "set (hidden)"
    for env_var in _SCAN_ENV_VARS:
        if os.environ.get(env_var):
            return f"from ${env_var}"
    return "not set"


def _file_flag(path: Path, *, will_write: bool) -> str:
    """``(new)`` / ``(overwrite)`` / ``(kept)`` / ``(skipped)`` for a target."""
    if will_write:
        return "(overwrite)" if path.exists() else "(new)"
    return "(kept)" if path.exists() else "(skipped)"


def render_summary(
    answers: InitAnswers,
    *,
    example_path: Path,
    user_path: Path,
    write_user: bool,
) -> str:
    """Build the pre-write review text — secrets shown as status only.

    This is the single place secret status is rendered. It is **never**
    handed a raw key/token to interpolate: for every secret it emits one
    of ``set (hidden)`` / ``from $ENV`` / ``not set``. Returning a plain
    string (rather than a Rich object) keeps it unit-testable without a
    TTY — the command wraps the result in a ``Panel``.

    Parameters
    ----------
    answers : InitAnswers
        The collected choices.
    example_path, user_path : Path
        Absolute targets for ``whygraph.example.toml`` / ``whygraph.toml``.
    write_user : bool
        Whether the command will (over)write ``whygraph.toml`` — drives
        the file flag shown next to ``user_path``.

    Returns
    -------
    str
        The multi-line summary body for the confirmation panel.
    """
    agent = answers.agent or "— (none)"

    lines = [
        f"Agent:      {agent}",
        f"Analyze:    {answers.analyze_provider} · {answers.analyze_model or 'provider default'}",
        f"Rationale:  {answers.rationale_provider} · {answers.rationale_model or 'provider default'}",
    ]

    # API-key status, once per unique key-bearing provider in use.
    key_providers = [
        p
        for p in (answers.analyze_provider, answers.rationale_provider)
        if p in _PROVIDER_ENV_VAR
    ]
    seen: set[str] = set()
    for provider in key_providers:
        if provider in seen:
            continue
        seen.add(provider)
        lines.append(f"API key ({provider}): {_key_status(provider, answers)}")

    lines.append(f"Scan:       {answers.scan_provider}")
    if answers.scan_provider != "off":
        lines.append(f"Scan token: {_scan_token_status(answers)}")

    lines.append("")
    lines.append(f"{example_path}  {_file_flag(example_path, will_write=True)}")
    lines.append(f"{user_path}  {_file_flag(user_path, will_write=write_user)}")

    return "\n".join(lines)


def _prompt_agent(prompter: Prompter, preset_agent: str | None) -> str | None:
    """Return the resolved agent name, prompting when not preset."""
    if preset_agent is not None:
        return preset_agent
    from whygraph import agents

    choices = sorted(agents.AGENTS)
    default = "claude" if "claude" in choices else choices[0]
    return str(_require(prompter.select("Which agent?", choices, default)))


def _prompt_llm(prompter: Prompter) -> tuple[str, str, str, str]:
    """Prompt analyze + rationale provider/model (steps 2-5).

    Rationale provider defaults to the analyze provider; the rationale
    model defaults to the analyze model when the provider is unchanged,
    else to the rationale provider's own default model.
    """
    analyze_provider = str(
        _require(
            prompter.select("Analyze LLM provider?", list(_PROVIDERS), "anthropic")
        )
    )
    analyze_model = str(
        _require(
            prompter.text(
                "Analyze model?", _PROVIDER_DEFAULT_MODEL[analyze_provider]
            )
        )
    ).strip()

    rationale_provider = str(
        _require(
            prompter.select(
                "Rationale LLM provider?", list(_PROVIDERS), analyze_provider
            )
        )
    )
    rationale_default_model = (
        analyze_model
        if rationale_provider == analyze_provider
        else _PROVIDER_DEFAULT_MODEL[rationale_provider]
    )
    rationale_model = str(
        _require(prompter.text("Rationale model?", rationale_default_model))
    ).strip()

    return analyze_provider, analyze_model, rationale_provider, rationale_model


def _prompt_api_keys(
    prompter: Prompter, analyze_provider: str, rationale_provider: str
) -> dict[str, str]:
    """Prompt one password per unique key-bearing provider in use (step 6)."""
    api_keys: dict[str, str] = {}
    seen: set[str] = set()
    for provider in (analyze_provider, rationale_provider):
        if provider in seen or provider not in _PROVIDER_ENV_VAR:
            continue
        seen.add(provider)
        env_var = _PROVIDER_ENV_VAR[provider]
        key = str(
            _require(
                prompter.password(
                    f"API key for {provider} (blank → read ${env_var})"
                )
            )
        ).strip()
        if key:
            api_keys[provider] = key
    return api_keys


def _prompt_scan(prompter: Prompter) -> tuple[str, str | None]:
    """Prompt the scan provider and (for github/auto) the token (step 7)."""
    scan_provider = str(
        _require(
            prompter.select(
                "Source-control provider?", list(_SCAN_PROVIDERS), "off"
            )
        )
    )
    scan_token: str | None = None
    if scan_provider != "off":
        token = str(
            _require(
                prompter.password(
                    "GitHub token (blank → use $GH_TOKEN / `gh auth login`)"
                )
            )
        ).strip()
        scan_token = token or None
    return scan_provider, scan_token


def prompt_for_init(
    project_root: Path,
    *,
    preset_agent: str | None,
    prompter: Prompter | None = None,
    on_summary=None,
) -> InitAnswers:
    """Run the guided init flow and return the collected answers.

    Order (§3 of the plan): overwrite gate → agent → analyze/rationale
    LLMs (+ keys) → scan (+ token) → **review & confirm**. The confirm is
    the last thing this function does, so a **No** (or a Ctrl-C at any
    prompt) raises :class:`InitAborted` *before* the caller writes
    anything.

    Parameters
    ----------
    project_root : Path
        Repo root — used to resolve the two target paths and detect an
        existing ``whygraph.toml`` (which triggers the overwrite gate).
    preset_agent : str or None
        Agent from ``--agent``; when set, the agent prompt is skipped.
    prompter : Prompter, optional
        Prompt backend. Defaults to :class:`QuestionaryPrompter`; tests
        pass a :class:`ScriptedPrompter`.
    on_summary : callable, optional
        Hook receiving the summary text just before the final confirm —
        the command uses it to render the Rich panel. Kept as a callback
        so this module needs no Rich import and stays pure.

    Returns
    -------
    InitAnswers
        The user's choices, with ``reconfigure_toml`` reflecting the
        overwrite decision. Only returned once the user confirms the
        write.

    Raises
    ------
    InitAborted
        On Ctrl-C / EOF at any prompt, or a declined final confirm.
    """
    prompter = prompter or QuestionaryPrompter()
    user_path = project_root / CONFIG_FILENAME
    example_path = project_root / EXAMPLE_CONFIG_FILENAME

    # Step 0 — overwrite gate (only when whygraph.toml already exists).
    reconfigure = True
    if user_path.exists():
        reconfigure = bool(
            _require(
                prompter.confirm(
                    "whygraph.toml already exists. Reconfigure it?", default=False
                )
            )
        )

    # Step 1 — agent (independent of the overwrite decision; MCP wiring
    # does not touch whygraph.toml).
    agent = _prompt_agent(prompter, preset_agent)

    # Steps 2-7 — only when (re)configuring whygraph.toml.
    if reconfigure:
        analyze_provider, analyze_model, rationale_provider, rationale_model = (
            _prompt_llm(prompter)
        )
        api_keys = _prompt_api_keys(prompter, analyze_provider, rationale_provider)
        scan_provider, scan_token = _prompt_scan(prompter)
        answers = InitAnswers(
            agent=agent,
            analyze_provider=analyze_provider,
            analyze_model=analyze_model,
            rationale_provider=rationale_provider,
            rationale_model=rationale_model,
            api_keys=api_keys,
            scan_provider=scan_provider,
            scan_token=scan_token,
            reconfigure_toml=True,
        )
    else:
        # Keep the existing whygraph.toml; still refresh the example and
        # wire the agent.
        answers = InitAnswers(agent=agent, reconfigure_toml=False)

    # Step 8 — review & confirm (the single gate before any write).
    will_write_user = (not user_path.exists()) or answers.reconfigure_toml
    summary = render_summary(
        answers,
        example_path=example_path,
        user_path=user_path,
        write_user=will_write_user,
    )
    if on_summary is not None:
        on_summary(summary)
    if not _require(prompter.confirm("Write these files?", default=True)):
        raise InitAborted("declined at confirmation")

    return answers


__all__ = [
    "InitAborted",
    "Prompter",
    "QuestionaryPrompter",
    "prompt_for_init",
    "render_summary",
]
