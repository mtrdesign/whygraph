"""TOML-backed configuration for WhyGraph.

The :class:`Config` value object holds all user-tunable settings. It is
loaded once from ``<project_root>/whygraph.toml`` (see
:func:`whygraph.core.get_config`) or falls back to :meth:`Config.defaults`
if the file is absent.

LLM provider settings are kept as typed sub-dataclasses
(:class:`AnthropicConfig`, :class:`OpenAIConfig`, …) grouped under
:class:`LlmConfig`. Each adapter in :mod:`whygraph.services.llm`
consumes its own typed section via ``from_config``; the values are
loaded from ``[llm.<provider>]`` tables in ``whygraph.toml``.

Config v2 is **dict-first**: :meth:`Config.from_dict` builds a
:class:`Config` from a plain mapping (a portal DB row, a JSON env var, a
parsed TOML file), and :meth:`Config.from_toml` is a thin wrapper over it.
Every input layer goes through :func:`normalize_v2` first, which maps the
1.x spellings onto the v2 keys and reports each deprecated key once per
process; :func:`merge_v2` deep-merges already normalized layers. Which
``(provider, model)`` a task runs on is answered in one place,
:meth:`Config.model_for`.
"""

from __future__ import annotations

import copy
import logging
import os
import re
import threading
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import NamedTuple

from whygraph.core.logger import LogLevel

_log = logging.getLogger(__name__)

CONFIG_FILENAME = "whygraph.toml"
"""Name of the project-root config file loaded by :func:`whygraph.core.get_config`."""


class ConfigError(RuntimeError):
    """Raised when ``whygraph.toml`` is malformed or contains invalid values.

    Distinguishes user-supplied configuration mistakes from unexpected
    runtime errors so callers can surface a clean message instead of a
    stack trace.
    """


KNOWN_PROVIDERS: tuple[str, ...] = (
    "anthropic",
    "openai",
    "deepseek",
    "openrouter",
    "ollama",
    "claude-cli",
)
"""Provider tags of the built-in LLM adapters.

Mirrors :attr:`whygraph.services.llm.LlmClientFactory.BUILTIN_PROVIDERS`
(a test pins the two together) - duplicated because ``core`` must not
import from ``services``. Used to decide whether a ``[<task>].model``
prefix names a provider."""

CHAT_PROVIDERS: tuple[str, ...] = ("anthropic", "openai", "deepseek", "openrouter")
"""Provider tags that can drive the tool-calling chat.

Mirrors :data:`whygraph.services.llm.CHAT_PROVIDERS` (pinned by a test)."""

TASKS: tuple[str, ...] = ("analyze", "rationale", "chat")
"""The tasks :meth:`Config.model_for` resolves a ``(provider, model)`` for."""

_LEGACY_DEFAULT_PROVIDER = "anthropic"
"""The 1.x task-level ``provider`` default, applied **last** in v2."""

# provider tag -> LlmConfig attribute. `claude_cli` is accepted as a
# spelling of the `claude-cli` tag (the TOML section's idiom).
_PROVIDER_ATTRS: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "deepseek": "deepseek",
    "openrouter": "openrouter",
    "ollama": "ollama",
    "claude-cli": "claude_cli",
}


def _canonical_provider(tag: str) -> str:
    """Map the ``claude_cli`` spelling onto the ``claude-cli`` adapter tag."""
    return "claude-cli" if tag == "claude_cli" else tag


def _split_provider_model(value: str) -> tuple[str, str] | None:
    """Split ``"provider/model"`` on the first ``/``; ``None`` if not that shape."""
    head, sep, tail = value.partition("/")
    if not sep or not head or not tail:
        return None
    return _canonical_provider(head), tail


class ModelChoice(NamedTuple):
    """The resolved ``(provider, model)`` pair a task runs on.

    Returned by :meth:`Config.model_for`. A tuple, so callers may unpack
    it as ``provider, model = cfg.model_for("analyze")``.

    Attributes
    ----------
    provider : str
        Adapter tag, e.g. ``"anthropic"`` or ``"claude-cli"``.
    model : str or None
        Model identifier. ``None`` only for a provider that is not built
        in (a third-party adapter registered on the factory), whose own
        config then supplies the model.
    """

    provider: str
    model: str | None


# ---------------------------------------------------------------------------
# Deprecations: warn once per process
# ---------------------------------------------------------------------------

_warned: set[str] = set()
_warned_lock = threading.Lock()


def _warn_deprecated(messages: list[str]) -> None:
    """Log each deprecation message at WARNING, at most once per process."""
    for message in messages:
        with _warned_lock:
            if message in _warned:
                continue
            _warned.add(message)
        _log.warning("%s", message)


def _reset_deprecation_warnings() -> None:
    """Forget which deprecations were logged. Test isolation only."""
    with _warned_lock:
        _warned.clear()


@dataclass(frozen=True, slots=True)
class AnthropicConfig:
    """Configuration for :class:`AnthropicAdapter` (anthropic SDK).

    Attributes
    ----------
    model : str
        Anthropic model identifier (e.g. ``"claude-opus-4-7"``).
    api_key : str or None
        Explicit API key. ``None`` (default) lets the SDK read
        ``ANTHROPIC_API_KEY`` from the environment.
    timeout_sec : int
        Per-request timeout in seconds. Default ``60``.
    """

    model: str = "claude-opus-4-7"
    api_key: str | None = field(default=None, repr=False)
    timeout_sec: int = 60


@dataclass(frozen=True, slots=True)
class OpenAIConfig:
    """Configuration for :class:`OpenAIAdapter` (openai SDK).

    Attributes
    ----------
    model : str
        OpenAI model identifier (e.g. ``"gpt-4o"``).
    api_key : str or None
        Explicit API key. ``None`` (default) lets the SDK read
        ``OPENAI_API_KEY`` from the environment.
    base_url : str or None
        Override the API endpoint. ``None`` (default) keeps the SDK's
        built-in ``https://api.openai.com/v1``.
    timeout_sec : int
        Per-request timeout in seconds. Default ``60``.
    """

    model: str = "gpt-4o"
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    timeout_sec: int = 60


@dataclass(frozen=True, slots=True)
class DeepSeekConfig:
    """Configuration for :class:`DeepSeekAdapter` (openai SDK + DeepSeek URL).

    Attributes
    ----------
    model : str
        DeepSeek model identifier (e.g. ``"deepseek-chat"``).
    api_key : str or None
        Explicit API key. ``None`` (default) reads ``DEEPSEEK_API_KEY``
        from the environment (the adapter handles this — DeepSeek does
        *not* use ``OPENAI_API_KEY``).
    timeout_sec : int
        Per-request timeout in seconds. Default ``60``.
    """

    model: str = "deepseek-chat"
    api_key: str | None = field(default=None, repr=False)
    timeout_sec: int = 60


@dataclass(frozen=True, slots=True)
class OpenRouterConfig:
    """Configuration for :class:`OpenRouterAdapter` (openai SDK + OpenRouter URL).

    Attributes
    ----------
    model : str
        OpenRouter model identifier (e.g. ``"openrouter/auto"``, which
        lets OpenRouter route the prompt itself). Note that not every
        routed model supports tool calling — pin a tool-capable model
        when using OpenRouter for chat.
    api_key : str or None
        Explicit API key. ``None`` (default) reads ``OPENROUTER_API_KEY``
        from the environment (the adapter handles this — OpenRouter does
        *not* use ``OPENAI_API_KEY``).
    timeout_sec : int
        Per-request timeout in seconds. Default ``60``.
    """

    model: str = "openrouter/auto"
    api_key: str | None = field(default=None, repr=False)
    timeout_sec: int = 60


@dataclass(frozen=True, slots=True)
class OllamaConfig:
    """Configuration for :class:`OllamaAdapter` (local ollama server).

    Attributes
    ----------
    model : str
        Local Ollama model tag (e.g. ``"llama3"``).
    host : str or None
        Override the Ollama server URL. ``None`` (default) keeps
        ``http://localhost:11434``.
    timeout_sec : int
        Per-request timeout in seconds. Default ``120`` — local models
        are slower than hosted ones, so the default is generous.
    """

    model: str = "llama3"
    host: str | None = None
    timeout_sec: int = 120


@dataclass(frozen=True, slots=True)
class ClaudeCliConfig:
    """Configuration for :class:`ClaudeCliAdapter` (``claude --print``).

    Attributes
    ----------
    model : str
        Claude model identifier (e.g. ``"claude-opus-4-7"``).
    api_key : str or None
        ``None`` (default) strips ``ANTHROPIC_API_KEY`` from the
        subprocess env so the CLI falls through to subscription billing.
        Passing a value exports it as ``ANTHROPIC_API_KEY`` (API billing).
    timeout_sec : int
        Per-invocation timeout in seconds. Default ``120``.
    config_dir : Path or None
        Claude Code profile directory, exported to the subprocess as
        ``CLAUDE_CONFIG_DIR``. ``None`` (default) inherits the ambient
        ``CLAUDE_CONFIG_DIR`` (or the CLI's own ``~/.claude``). ``~`` and
        ``$VARS`` are expanded; a relative path resolves against the
        directory holding ``whygraph.toml``.
    oauth_token : str or None
        A long-lived Claude subscription token (``claude setup-token``),
        exported as ``CLAUDE_CODE_OAUTH_TOKEN``: subscription billing
        without a logged-in profile - how the portal's Docker image runs
        the CLI. ``None`` (default) inherits an ambient
        ``CLAUDE_CODE_OAUTH_TOKEN``, else the CLI's own login.
    """

    model: str = "claude-opus-4-7"
    api_key: str | None = field(default=None, repr=False)
    timeout_sec: int = 120
    config_dir: Path | None = None
    oauth_token: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """Configuration for the file-logging side of :func:`configure_logging`.

    Loaded from the ``[logging]`` table in ``whygraph.toml``. The Rich →
    stderr handler is always attached by :func:`configure_logging`; this
    config controls the **additional** rotating file handler.

    Attributes
    ----------
    file : Path or None
        Path to the rotating log file. ``None`` (default) disables file
        logging entirely. Relative paths in the TOML are resolved against
        the config file's directory (mirroring ``whygraph_db``).
    level : str or None
        Optional per-handler verbosity for the file. ``None`` (default)
        means the file inherits the top-level ``log_level``; setting it
        lets the file be more verbose than the console (e.g. file at
        ``"DEBUG"`` while console stays at ``"INFO"``). Must match a
        :class:`LogLevel` member name when set.
    max_bytes : int
        Size threshold at which the file rotates, in bytes. Must be
        ``>= 1``. Default ``5_000_000`` (5 MB).
    backup_count : int
        How many rotated copies to keep alongside the live file. Must be
        ``>= 0``. Default ``3``.
    """

    file: Path | None = None
    level: str | None = None
    max_bytes: int = 5_000_000
    backup_count: int = 3

    def __post_init__(self) -> None:
        """Validate field values immediately after construction.

        Raises
        ------
        ConfigError
            If ``level`` is set but doesn't name a :class:`LogLevel`
            member, if ``max_bytes < 1``, or if ``backup_count < 0``.
        """
        if self.level is not None:
            try:
                LogLevel[self.level.upper()]
            except KeyError as exc:
                raise ConfigError(f"invalid logging.level: {self.level!r}") from exc
        if self.max_bytes < 1:
            raise ConfigError(f"logging.max_bytes must be >= 1, got {self.max_bytes}")
        if self.backup_count < 0:
            raise ConfigError(
                f"logging.backup_count must be >= 0, got {self.backup_count}"
            )


@dataclass(frozen=True, slots=True)
class AnalyzeConfig:
    """Configuration for the LLM-driven commit descriptor.

    Loaded from the ``[analyze]`` table in ``whygraph.toml``. Consumed
    by :class:`whygraph.analyze.LlmDescriptor.from_config` to construct
    a descriptor against an existing :class:`LlmConfig`-backed provider.

    Attributes
    ----------
    provider : str or None
        Tag of the :class:`whygraph.services.llm.LlmClient` adapter to
        use. ``None`` (default) inherits the provider of ``[llm].model``,
        else ``"anthropic"`` - resolve the effective pair with
        :meth:`Config.model_for`. Must match one of
        :attr:`LlmClientFactory.providers` at construction time; unknown
        providers surface as :class:`whygraph.services.llm.LlmError` from
        :meth:`~whygraph.analyze.LlmDescriptor.from_config`, not here —
        ``core/config`` deliberately does not import from
        ``services/llm`` to keep the dependency direction clean.
    model : str or None
        Model override for commit descriptions only. ``None`` (default)
        defers to ``[llm].model`` (then the provider's default). A
        ``"provider/model"`` value in a table with no ``provider`` key is
        split, see :meth:`Config.model_for`.
    max_diff_chars : int
        Cap on diff length before prompting. Diffs longer than this are
        truncated with an explicit marker so the model knows the input
        was clipped. Must be ``>= 1``.
    large_commit_file_count : int
        Commits touching strictly more than this many files are treated
        as *bulk* commits (imports, squash merges, repo-wide sweeps).
        Their whole-diff description is skipped at scan time in favour of
        a cheap stub, and descriptions are instead generated lazily
        per-file on the MCP read path — so a single huge commit does not
        cost a repo-wide LLM pass nor anchor every symbol to one vague
        summary. Must be ``>= 1``.
    timeout_sec : int or None
        **Deprecated** (warns; removed in 3.0) - set
        ``[llm.<provider>].timeout_sec`` instead. Per-call timeout
        forwarded into :class:`CompletionRequest`; ``None`` (default)
        defers to the provider's.
    pr_origin_min_commits : int
        Commit-rich half of the squash-merge enrichment gate
        (:mod:`whygraph.scan.pr_origin_enricher`). A squash-merged PR has
        its original feature-branch commits recovered when it collapsed at
        least this many commits (the file-bulk half reuses
        ``large_commit_file_count``). Must be ``>= 1``.
    max_workers : int
        Thread-pool size for the LLM-description phase of a scan. Must be
        ``>= 1``. Default ``2``. Replaces the 1.x ``[scan].max_workers``,
        which still parses (with a deprecation warning).
    """

    provider: str | None = None
    model: str | None = None
    max_diff_chars: int = 50_000
    large_commit_file_count: int = 30
    timeout_sec: int | None = None
    pr_origin_min_commits: int = 5
    max_workers: int = 2


@dataclass(frozen=True, slots=True)
class RationaleConfig:
    """Configuration for the LLM-driven rationale generator.

    Loaded from the ``[rationale]`` table in ``whygraph.toml``. Consumed by
    :meth:`whygraph.analyze.RationaleGenerator.from_config` to construct a
    generator against an existing :class:`LlmConfig`-backed provider.

    Attributes
    ----------
    provider : str or None
        Tag of the :class:`whygraph.services.llm.LlmClient` adapter to use.
        ``None`` (default) inherits the provider of ``[llm].model``, else
        ``"anthropic"`` - resolve the effective pair with
        :meth:`Config.model_for`.
        Must match one of :attr:`LlmClientFactory.providers` at construction
        time; unknown providers surface as
        :class:`whygraph.services.llm.LlmError` from
        :meth:`~whygraph.analyze.RationaleGenerator.from_config`, not here —
        ``core/config`` deliberately does not import from ``services/llm``
        to keep the dependency direction clean.
    model : str or None
        Model override for rationale generation only. ``None`` (default)
        defers to ``[llm].model`` (then the provider's default).
    timeout_sec : int or None
        **Deprecated** (warns; removed in 3.0) - set
        ``[llm.<provider>].timeout_sec`` instead. Per-call timeout
        forwarded into :class:`CompletionRequest`; ``None`` (default)
        defers to the provider's.
    pr_roster_max_commits : int
        Cap on how many squashed-commit headlines are rendered into a
        single PR block in the rationale prompt. Bounds the prompt size
        when a squash collapsed a long feature branch. Must be ``>= 1``.
    pr_discussion_max_comments : int
        Cap on how many PR comments are rendered into a single PR block
        in the rationale prompt. Must be ``>= 1``.
    pr_comment_max_chars : int
        Per-comment body clip applied before rendering a PR comment into
        the rationale prompt. Must be ``>= 1``.
    """

    provider: str | None = None
    model: str | None = None
    timeout_sec: int | None = None
    pr_roster_max_commits: int = 30
    pr_discussion_max_comments: int = 20
    pr_comment_max_chars: int = 500


@dataclass(frozen=True, slots=True)
class ChatConfig:
    """Configuration for the portal's Chat assistant.

    Loaded from the ``[chat]`` table in ``whygraph.toml``. Provider and
    model here are only **defaults for new sessions** — each session
    records its own pair on its row, so changing this never rewrites an
    existing conversation's model.

    Attributes
    ----------
    provider : str or None
        Default chat provider for new sessions. ``None`` (default)
        inherits the provider of ``[llm].model``, else ``"anthropic"``.
        Must be one of :data:`CHAT_PROVIDERS`; a non-chat tag set here is
        refused by :meth:`Config.model_for` (an inherited one falls back
        to ``"anthropic"``).
    model : str or None
        Default model. ``None`` (default; an empty string normalizes to
        it) defers to ``[llm].model``, then the provider's default.
    max_tool_rounds : int
        Hard bound on tool *rounds* — model round-trips, not individual
        tool calls, of which one round may contain many. Must be ``>= 1``.
        Hitting it stops the loop and spends one final call with no tools
        offered, so the turn ends in prose rather than looping forever on a
        model that keeps calling tools (see
        :class:`whygraph.chat.harness.RoundLimit`).
    max_rationale_generations : int
        How many uncached rationale cards one user turn may generate.
        Must be ``>= 0``; ``0`` makes the tool cache-only. This is what
        keeps one question from fanning out into N nested LLM calls.
    context_token_budget : int
        Approximate token ceiling for the history sent to the model
        (estimated as chars/4). Must be ``>= 1000``. Older messages stay
        in the DB and the UI — only the model's view is windowed.
    """

    provider: str | None = None
    model: str | None = None
    max_tool_rounds: int = 8
    max_rationale_generations: int = 2
    context_token_budget: int = 60_000


@dataclass(frozen=True, slots=True)
class LlmConfig:
    """Aggregate of every per-provider :class:`LlmClient` configuration.

    Populated from the ``[llm]`` table and its ``[llm.<provider>]``
    sub-tables. Each adapter in :mod:`whygraph.services.llm` is
    constructed from its matching sub-attribute via
    ``Adapter.from_config(cfg.<provider>)``.

    Attributes
    ----------
    model : str or None
        The v2 default model for every task, as ``"provider/model"``
        (split on the first ``/``, so ``"openrouter/openrouter/auto"`` is
        OpenRouter's ``openrouter/auto``). ``None`` (default) leaves the
        choice to the tasks and the provider defaults.
    anthropic, openai, deepseek, openrouter, ollama, claude_cli
        Per-provider connection settings (key, endpoint, ``timeout_sec``).
        Their ``model`` field is the adapter default; setting it in
        ``[llm.<provider>]`` is deprecated in favour of ``model`` above.
    """

    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    openai: OpenAIConfig = field(default_factory=OpenAIConfig)
    deepseek: DeepSeekConfig = field(default_factory=DeepSeekConfig)
    openrouter: OpenRouterConfig = field(default_factory=OpenRouterConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    claude_cli: ClaudeCliConfig = field(default_factory=ClaudeCliConfig)
    model: str | None = None

    def __post_init__(self) -> None:
        """Validate the ``[llm].model`` shape.

        Raises
        ------
        ConfigError
            If ``model`` is set but is not ``"provider/model"``.
        """
        if self.model is not None and _split_provider_model(self.model) is None:
            raise ConfigError(
                f'invalid llm.model: {self.model!r}, must be "provider/model" '
                '(e.g. "anthropic/claude-opus-4-7")'
            )

    @property
    def default_provider(self) -> str | None:
        """The provider half of :attr:`model`, or ``None`` when unset."""
        if self.model is None:
            return None
        split = _split_provider_model(self.model)
        return split[0] if split else None

    def section(self, provider: str) -> object | None:
        """Return the typed ``[llm.<provider>]`` section, or ``None``.

        Parameters
        ----------
        provider : str
            A provider tag (``"claude-cli"`` and ``"claude_cli"`` both work).

        Returns
        -------
        object or None
            The provider's sub-config, or ``None`` for a tag that is not
            built in.
        """
        attr = _PROVIDER_ATTRS.get(_canonical_provider(provider))
        return getattr(self, attr) if attr is not None else None

    def default_model(self, provider: str) -> str | None:
        """The model ``provider`` runs when no task pins one.

        ``[llm].model`` when it names this provider, else the provider
        section's own model (a deprecated ``[llm.<provider>].model``, or
        the adapter default). The shared tail of
        :meth:`Config.model_for`, exposed for callers that hold only an
        :class:`LlmConfig` (the client factories).

        Parameters
        ----------
        provider : str
            A provider tag.

        Returns
        -------
        str or None
            The model, or ``None`` for a provider that is not built in.
        """
        return self._default_model(provider)[0]

    def _default_model(self, provider: str) -> tuple[str | None, bool]:
        """``(model, pinned)`` for ``provider``; pinned means from ``[llm].model``."""
        provider = _canonical_provider(provider)
        if self.model is not None:
            split = _split_provider_model(self.model)
            if split is not None and split[0] == provider:
                return split[1], True
        section = self.section(provider)
        return (getattr(section, "model", None) if section else None), False


# TOML section name → (Config attribute name, sub-dataclass) so the
# TOML loader can build typed sections from raw dicts in one pass.
_LLM_SECTIONS: tuple[tuple[str, str, type], ...] = (
    ("anthropic", "anthropic", AnthropicConfig),
    ("openai", "openai", OpenAIConfig),
    ("deepseek", "deepseek", DeepSeekConfig),
    ("openrouter", "openrouter", OpenRouterConfig),
    ("ollama", "ollama", OllamaConfig),
    # `claude_cli` (Python attr) ↔ `claude-cli` (TOML section) — TOML
    # idiomatically uses dashes; Python identifiers cannot, so we keep
    # both forms and let either one parse.
    ("claude_cli", "claude_cli", ClaudeCliConfig),
    ("claude-cli", "claude_cli", ClaudeCliConfig),
)


REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
"""What ``[scan].remote`` may be: a plain remote name, never a git option."""


def check_scan_remote(value: str) -> str:
    """Return ``value`` if it is a safe git remote name.

    ``[scan].remote`` is passed to ``git fetch`` / ``git remote get-url``;
    a value such as ``--upload-pack=<cmd>`` would be parsed as an option
    and run a command. Only :data:`REMOTE_NAME_RE` is accepted.

    Parameters
    ----------
    value : str
        The stripped, non-empty ``[scan].remote``.

    Returns
    -------
    str
        ``value`` unchanged.

    Raises
    ------
    ConfigError
        If ``value`` does not match :data:`REMOTE_NAME_RE`.
    """
    if not isinstance(value, str) or not REMOTE_NAME_RE.match(value):
        raise ConfigError(
            f"[scan].remote must be a git remote name "
            f"(letters, digits, '.', '_', '/', '-'), got {value!r}"
        )
    return value


def check_default_branch(value: str) -> str:
    """Return ``value`` unless it could be read as a git option.

    Parameters
    ----------
    value : str
        The stripped, non-empty ``[scan].default_branch``.

    Returns
    -------
    str
        ``value`` unchanged.

    Raises
    ------
    ConfigError
        If ``value`` starts with ``-`` (or is not a string).
    """
    if not isinstance(value, str) or value.startswith("-"):
        raise ConfigError(
            f"[scan].default_branch must not start with '-', got {value!r}"
        )
    return value


def _parse_hooks(value: object) -> bool | tuple[str, ...]:
    """Normalize the *shape* of ``[scan].hooks``.

    Accepts a bool verbatim, or a list of strings as a tuple (so the
    frozen :class:`Config` stays hashable). An empty list collapses to
    ``False`` — "install none" has one representation downstream.

    Hook **names** are deliberately not validated here: that would mean
    importing :mod:`whygraph.hooks` from ``core``, inverting the
    dependency direction of the cross-cutting leaf package.
    :func:`whygraph.hooks.resolve_hook_names` validates at the point of
    use, and the portal - the only caller that acts on the value (on
    Initialize and on a ``[scan].hooks`` change) - surfaces a typo as a
    warning.

    Raises
    ------
    ConfigError
        If the value is neither a bool nor a list of strings.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, list):
        if not all(isinstance(item, str) for item in value):
            raise ConfigError("[scan].hooks list entries must all be strings")
        return tuple(value) or False
    raise ConfigError(
        f"[scan].hooks must be a bool or a list of hook names, "
        f"got {type(value).__name__}"
    )


def _build_llm_config(raw: dict, base: Path) -> LlmConfig:
    """Parse a raw ``[llm]`` dict into a typed :class:`LlmConfig`.

    ``base`` is the directory containing the TOML file — a relative
    ``[llm.claude_cli].config_dir`` resolves against it.
    """
    sections: dict[str, object] = {}
    known_attrs = {f.name for f in fields(LlmConfig)}
    for toml_name, attr_name, cls in _LLM_SECTIONS:
        block = raw.get(toml_name)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise ConfigError(
                f"[llm.{toml_name}] must be a table, got {type(block).__name__}"
            )
        known_fields = {f.name for f in fields(cls)}
        for unknown in set(block) - known_fields:
            _log.warning("ignoring unknown key in [llm.%s]: %r", toml_name, unknown)
        accepted = {k: v for k, v in block.items() if k in known_fields}
        if accepted.get("config_dir") is not None:
            p = Path(os.path.expandvars(accepted["config_dir"])).expanduser()
            accepted["config_dir"] = p if p.is_absolute() else (base / p).resolve()
        sections[attr_name] = cls(**accepted)
    if raw.get("model") is not None:
        sections["model"] = raw["model"]
    for unknown in set(raw) - {n for n, *_ in _LLM_SECTIONS} - {"model"}:
        _log.warning("ignoring unknown key in [llm]: %r", unknown)
    return LlmConfig(**{k: v for k, v in sections.items() if k in known_attrs})


def _build_analyze_config(raw: dict) -> AnalyzeConfig:
    """Parse a raw ``[analyze]`` dict into a typed :class:`AnalyzeConfig`."""
    known = {f.name for f in fields(AnalyzeConfig)}
    for unknown in set(raw) - known:
        _log.warning("ignoring unknown key in [analyze]: %r", unknown)
    return AnalyzeConfig(**{k: v for k, v in raw.items() if k in known})


def _build_logging_config(raw: dict, base: Path) -> LoggingConfig:
    """Parse a raw ``[logging]`` dict into a typed :class:`LoggingConfig`.

    ``base`` is the directory containing the TOML file — relative ``file``
    paths resolve against it (same convention as ``whygraph_db``).
    """
    known = {f.name for f in fields(LoggingConfig)}
    for unknown in set(raw) - known:
        _log.warning("ignoring unknown key in [logging]: %r", unknown)
    accepted = {k: v for k, v in raw.items() if k in known}
    if "file" in accepted and accepted["file"] is not None:
        p = Path(accepted["file"])
        accepted["file"] = p if p.is_absolute() else (base / p).resolve()
    return LoggingConfig(**accepted)


def _build_rationale_config(raw: dict) -> RationaleConfig:
    """Parse a raw ``[rationale]`` dict into a typed :class:`RationaleConfig`."""
    known = {f.name for f in fields(RationaleConfig)}
    for unknown in set(raw) - known:
        _log.warning("ignoring unknown key in [rationale]: %r", unknown)
    return RationaleConfig(**{k: v for k, v in raw.items() if k in known})


def _build_chat_config(raw: dict) -> ChatConfig:
    """Parse a raw ``[chat]`` dict into a typed :class:`ChatConfig`."""
    known = {f.name for f in fields(ChatConfig)}
    for unknown in set(raw) - known:
        _log.warning("ignoring unknown key in [chat]: %r", unknown)
    return ChatConfig(**{k: v for k, v in raw.items() if k in known})


def _resolve_path(value: object, base: Path, *, expand: bool = False) -> object:
    """Resolve a relative path string against ``base``; leave anything else as-is."""
    if not isinstance(value, (str, os.PathLike)):
        return value
    p = Path(value)
    if expand:
        p = Path(os.path.expandvars(str(p))).expanduser()
    return str(p if p.is_absolute() else (base / p).resolve())


def _move_alias(
    src: dict,
    old: str,
    dst: dict,
    new: str,
    old_label: str,
    new_label: str,
    warnings: list[str],
) -> None:
    """Move ``src[old]`` to ``dst[new]`` unless ``dst`` already has ``new``."""
    if old not in src:
        return
    value = src.pop(old)
    if new in dst:
        warnings.append(
            f"{old_label} is deprecated and ignored because {new_label} is also "
            f"set; remove {old_label} (support ends in 3.0)"
        )
        return
    dst[new] = value
    warnings.append(
        f"{old_label} is deprecated; use {new_label} instead (support ends in 3.0)"
    )


def _blank_model_to_none(table: dict) -> None:
    """Normalize an empty-string ``model`` to ``None`` in place."""
    if isinstance(table.get("model"), str) and not table["model"].strip():
        table["model"] = None


def normalize_v2(raw: Mapping, base: Path) -> tuple[dict, list[str]]:
    """Translate one config layer to the v2 shape.

    Applied to **each** layer (a parsed ``whygraph.toml``, a portal
    defaults row, a project row) *before* :func:`merge_v2`, so a 1.x
    alias on one layer and its v2 key on another never both survive into
    the merged dict with undefined precedence. Pure: the input is
    deep-copied and never mutated, and warnings are returned rather than
    logged (:meth:`Config.from_dict` logs them once per process).

    Translation, in order:

    * ``[scan].provider`` -> ``[scan].forge``, ``[scan].max_workers`` ->
      ``[analyze].max_workers``. When a layer sets both spellings, the
      v2 key wins and the alias is dropped (with a warning).
    * ``[llm.claude-cli]`` -> ``[llm.claude_cli]`` (the 1.x loader let the
      dashed table replace the underscored one; so does this).
    * A ``model`` in an ``[llm.<provider>]`` table and a task-level
      ``timeout_sec`` stay where they are - they still work - but warn.
    * An empty-string ``model`` becomes ``None``.
    * ``whygraph_db``, ``codegraph_db``, ``[logging].file`` and
      ``[llm.claude_cli].config_dir`` become absolute path strings
      (relative ones resolve against ``base``), so a merged dict carried
      to another process (``WHYGRAPH_CONFIG_JSON``) means the same thing.

    ``None`` values are kept: in a merge a ``None`` resets the key to its
    default (:func:`merge_v2`), and :meth:`Config.from_dict` treats a
    ``None`` as absent. Values are otherwise not validated here; that is
    :meth:`Config.from_dict`'s job.

    Parameters
    ----------
    raw : Mapping
        One layer, e.g. the result of :func:`tomllib.load`.
    base : Path
        Directory relative paths resolve against - the project root.

    Returns
    -------
    tuple of (dict, list of str)
        The normalized layer, and one human-readable message per
        deprecated key it used.
    """
    data = copy.deepcopy(dict(raw))
    warnings: list[str] = []

    scan = data.get("scan")
    if isinstance(scan, dict):
        _move_alias(
            scan, "provider", scan, "forge", "[scan].provider", "[scan].forge", warnings
        )
        if "max_workers" in scan:
            analyze = data.get("analyze")
            if analyze is None:
                analyze = data["analyze"] = {}
            if isinstance(analyze, dict):
                _move_alias(
                    scan,
                    "max_workers",
                    analyze,
                    "max_workers",
                    "[scan].max_workers",
                    "[analyze].max_workers",
                    warnings,
                )

    llm = data.get("llm")
    if isinstance(llm, dict):
        if "claude-cli" in llm:
            llm["claude_cli"] = llm.pop("claude-cli")
        _blank_model_to_none(llm)
        for attr in _PROVIDER_ATTRS.values():
            block = llm.get(attr)
            if not isinstance(block, dict):
                continue
            _blank_model_to_none(block)
            if block.get("model") is not None:
                tag = _canonical_provider(attr)
                warnings.append(
                    f"[llm.{attr}].model is deprecated; set [llm].model = "
                    f'"{tag}/<model>" or [<task>].model instead '
                    "(support ends in 3.0)"
                )
            if block.get("config_dir") is not None:
                block["config_dir"] = _resolve_path(
                    block["config_dir"], base, expand=True
                )

    for task in TASKS:
        table = data.get(task)
        if not isinstance(table, dict):
            continue
        _blank_model_to_none(table)
        if table.get("timeout_sec") is not None:
            warnings.append(
                f"[{task}].timeout_sec is deprecated; set timeout_sec in the "
                "provider's [llm.<provider>] table instead (support ends in 3.0)"
            )

    for key in ("whygraph_db", "codegraph_db"):
        if data.get(key) is not None:
            data[key] = _resolve_path(data[key], base)
    logging_table = data.get("logging")
    if isinstance(logging_table, dict) and logging_table.get("file") is not None:
        logging_table["file"] = _resolve_path(logging_table["file"], base)

    return data, warnings


def merge_v2(*layers: Mapping) -> dict:
    """Deep-merge normalized config layers, lowest precedence first.

    Merge rules: a key absent from a higher layer inherits the lower
    value; two tables merge key by key; lists and scalars replace; a
    ``None`` (JSON ``null``) replaces too, which resets the key to its
    default once :meth:`Config.from_dict` builds the result.

    Parameters
    ----------
    *layers : Mapping
        Layers already passed through :func:`normalize_v2`, e.g.
        ``merge_v2(global_defaults, project_row)``.

    Returns
    -------
    dict
        A new dict; the inputs are not mutated.
    """
    merged: dict = {}
    for layer in layers:
        merged = _merge_two(merged, layer)
    return merged


def _merge_two(lower: Mapping, upper: Mapping) -> dict:
    """Merge ``upper`` over ``lower`` per :func:`merge_v2`'s rules."""
    out = copy.deepcopy(dict(lower))
    for key, value in upper.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = _merge_two(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _drop_nones(value: object) -> object:
    """Recursively drop ``None`` values from dicts (``None`` means "default")."""
    if isinstance(value, dict):
        return {k: _drop_nones(v) for k, v in value.items() if v is not None}
    return value


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable runtime configuration for the WhyGraph package.

    Constructed from a config mapping via :meth:`from_dict`, from
    ``whygraph.toml`` via :meth:`from_toml`, or with default values via
    :meth:`defaults`. Validated at construction time by
    :meth:`__post_init__`. Ask :meth:`model_for` which
    ``(provider, model)`` a task runs on rather than reading the
    ``provider`` / ``model`` fields directly.

    Attributes
    ----------
    log_level : str
        Logging verbosity; must match a :class:`LogLevel` member name
        (case-insensitive). Default ``"INFO"``.
    scan_forge : str
        Source-control forge the scan crawls for PRs / issues. One of
        ``"off"`` (default — pull nothing), ``"github"`` (pull from the
        GitHub remote), or ``"auto"`` (detect the forge from the remote
        URL; GitHub-only today). Loaded from ``[scan].forge`` (the 1.x
        ``[scan].provider`` is a deprecated alias); an empty value is
        treated as ``"off"``.
    scan_remote : str
        Name of the git remote whose URL is inspected to resolve the
        forge for ``"github"`` / ``"auto"``. Default ``"origin"``.
        Loaded from ``[scan].remote``; an empty value falls back to
        ``"origin"``. Must match :data:`REMOTE_NAME_RE` (see
        :func:`check_scan_remote`) - it reaches ``git`` argv.
    scan_token : str or None
        GitHub token used to authenticate the ``gh`` CLI during the
        remote crawl. Loaded from ``[scan].token``; an empty value is
        treated as ``None``. When ``None``, the scan falls back to the
        ambient ``GH_TOKEN`` / ``GITHUB_TOKEN`` environment variables (or
        an existing ``gh auth login`` session). Kept per-project so one
        shared scanning container can serve repos across different orgs.
    scan_hooks : bool or tuple[str, ...]
        Which auto-rescan git hooks the portal keeps installed.
        ``True`` (default) → all of
        :data:`whygraph.hooks.HOOK_NAMES`; ``False`` or an empty list →
        none; a list of names → exactly those, with the rest removed.
        Loaded from ``[scan].hooks``. Only the *shape* is validated here;
        the names are checked by
        :func:`whygraph.hooks.resolve_hook_names` at the point of use, so
        ``core`` keeps no dependency on the hooks module.
    scan_default_branch : str or None
        Override the branch WhyGraph treats as shipped history, e.g.
        ``"develop"``. Loaded from ``[scan].default_branch``; an empty
        value is treated as ``None``, which auto-resolves from
        ``origin/HEAD`` then ``origin/main`` / ``origin/master``. An
        unresolvable value is not an error — it degrades to "cannot
        judge" and is reported in the scan panel. A value starting with
        ``-`` is (:func:`check_default_branch`).
    whygraph_db : Path or None
        Override path to the WhyGraph SQLite DB. If ``None``, callers
        use the project-relative default ``.whygraph/whygraph.db``.
    codegraph_db : Path or None
        Override path to the CodeGraph SQLite DB. If ``None``, callers
        use the project-relative default ``.codegraph/codegraph.db``.
    llm : LlmConfig
        Per-provider LLM client settings. Loaded from
        ``[llm.<provider>]`` tables; each :mod:`whygraph.services.llm`
        adapter consumes its own typed sub-config via
        ``Adapter.from_config(cfg.llm.<provider>)``.
    logging : LoggingConfig
        Settings for the optional rotating file-log handler. Loaded from
        the ``[logging]`` table; consumed by :func:`configure_logging` to
        attach a ``RotatingFileHandler`` alongside the Rich → stderr one.
    analyze : AnalyzeConfig
        Settings for the LLM commit descriptor. Loaded from the
        ``[analyze]`` table; consumed by
        :meth:`whygraph.analyze.LlmDescriptor.from_config`.
    rationale : RationaleConfig
        Settings for the LLM rationale generator. Loaded from the
        ``[rationale]`` table; consumed by
        :meth:`whygraph.analyze.RationaleGenerator.from_config`.
    chat : ChatConfig
        Settings for the portal's Chat assistant. Loaded from the
        ``[chat]`` table; consumed by :mod:`whygraph.chat` and
        :mod:`whygraph.serve.chat`.
    """

    log_level: str = "INFO"
    scan_forge: str = "off"
    scan_remote: str = "origin"
    scan_token: str | None = field(default=None, repr=False)
    scan_hooks: bool | tuple[str, ...] = True
    scan_default_branch: str | None = None
    whygraph_db: Path | None = None
    codegraph_db: Path | None = None
    llm: LlmConfig = field(default_factory=LlmConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    analyze: AnalyzeConfig = field(default_factory=AnalyzeConfig)
    rationale: RationaleConfig = field(default_factory=RationaleConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)

    def __post_init__(self) -> None:
        """Validate field values immediately after construction.

        Raises
        ------
        ConfigError
            If ``log_level`` is not a known :class:`LogLevel` name, if
            ``analyze.max_workers`` is less than ``1``, if ``scan_forge``
            is not one of ``"off"`` / ``"github"`` / ``"auto"``, if
            ``analyze.max_diff_chars``, ``analyze.large_commit_file_count``
            or ``analyze.pr_origin_min_commits`` is less than ``1``, or if
            any of the ``rationale`` PR-rendering caps
            (``pr_roster_max_commits``, ``pr_discussion_max_comments``,
            ``pr_comment_max_chars``) is less than ``1``.
        """
        try:
            LogLevel[self.log_level.upper()]
        except KeyError as exc:
            raise ConfigError(f"invalid log_level: {self.log_level!r}") from exc
        if self.analyze.max_workers < 1:
            raise ConfigError(
                f"analyze.max_workers must be >= 1, got {self.analyze.max_workers}"
            )
        if self.scan_forge not in {"off", "github", "auto"}:
            raise ConfigError(
                f"invalid scan.forge: {self.scan_forge!r}, "
                'must be one of "off", "github", "auto"'
            )
        if self.analyze.max_diff_chars < 1:
            raise ConfigError(
                "analyze.max_diff_chars must be >= 1, "
                f"got {self.analyze.max_diff_chars}"
            )
        if self.analyze.large_commit_file_count < 1:
            raise ConfigError(
                "analyze.large_commit_file_count must be >= 1, "
                f"got {self.analyze.large_commit_file_count}"
            )
        if self.analyze.pr_origin_min_commits < 1:
            raise ConfigError(
                "analyze.pr_origin_min_commits must be >= 1, "
                f"got {self.analyze.pr_origin_min_commits}"
            )
        if self.rationale.pr_roster_max_commits < 1:
            raise ConfigError(
                "rationale.pr_roster_max_commits must be >= 1, "
                f"got {self.rationale.pr_roster_max_commits}"
            )
        if self.rationale.pr_discussion_max_comments < 1:
            raise ConfigError(
                "rationale.pr_discussion_max_comments must be >= 1, "
                f"got {self.rationale.pr_discussion_max_comments}"
            )
        if self.rationale.pr_comment_max_chars < 1:
            raise ConfigError(
                "rationale.pr_comment_max_chars must be >= 1, "
                f"got {self.rationale.pr_comment_max_chars}"
            )
        if self.chat.max_tool_rounds < 1:
            raise ConfigError(
                f"chat.max_tool_rounds must be >= 1, got {self.chat.max_tool_rounds}"
            )
        if self.chat.max_rationale_generations < 0:
            raise ConfigError(
                "chat.max_rationale_generations must be >= 0, "
                f"got {self.chat.max_rationale_generations}"
            )
        if self.chat.context_token_budget < 1000:
            raise ConfigError(
                "chat.context_token_budget must be >= 1000, "
                f"got {self.chat.context_token_budget}"
            )

    @classmethod
    def from_dict(cls, raw: Mapping, base: Path) -> Config:
        """Build and validate a configuration from a plain mapping.

        The v2 entry point: the portal builds a project's :class:`Config`
        from DB rows, a child scan from ``WHYGRAPH_CONFIG_JSON``, and
        :meth:`from_toml` from a parsed file - all through here. The input
        is passed through :func:`normalize_v2` (so 1.x keys still work;
        each deprecated key is logged once per process) and is never
        mutated. A ``None`` value means "use the default".

        Unknown top-level, ``[scan]`` and section keys produce a warning
        on the ``whygraph.core.config`` logger and are otherwise ignored,
        to preserve forward compatibility with future fields.

        Parameters
        ----------
        raw : Mapping
            The config tree, shaped like ``whygraph.toml``.
        base : Path
            Directory relative paths (``whygraph_db``, ``codegraph_db``,
            ``[logging].file``, ``[llm.claude_cli].config_dir``) resolve
            against - the project root.

        Returns
        -------
        Config
            A validated, immutable configuration.

        Raises
        ------
        ConfigError
            If any field fails validation in :meth:`__post_init__`.
        """
        normalized, warnings = normalize_v2(raw, base)
        _warn_deprecated(warnings)
        data = _drop_nones(normalized)

        scan = data.pop("scan", {}) or {}
        if "forge" in scan:
            forge = (scan.pop("forge") or "").strip().lower()
            data["scan_forge"] = forge or "off"
        if "remote" in scan:
            remote = (scan.pop("remote") or "").strip()
            data["scan_remote"] = check_scan_remote(remote) if remote else "origin"
        if "token" in scan:
            token = (scan.pop("token") or "").strip()
            data["scan_token"] = token or None
        if "hooks" in scan:
            data["scan_hooks"] = _parse_hooks(scan.pop("hooks"))
        if "default_branch" in scan:
            branch = (scan.pop("default_branch") or "").strip()
            data["scan_default_branch"] = (
                check_default_branch(branch) if branch else None
            )
        for unknown in scan:
            _log.warning("ignoring unknown key in [scan]: %r", unknown)

        llm_raw = data.pop("llm", {}) or {}
        if llm_raw:
            data["llm"] = _build_llm_config(llm_raw, base)

        analyze_raw = data.pop("analyze", {}) or {}
        if analyze_raw:
            data["analyze"] = _build_analyze_config(analyze_raw)

        rationale_raw = data.pop("rationale", {}) or {}
        if rationale_raw:
            data["rationale"] = _build_rationale_config(rationale_raw)

        chat_raw = data.pop("chat", {}) or {}
        if chat_raw:
            data["chat"] = _build_chat_config(chat_raw)

        logging_raw = data.pop("logging", {}) or {}
        if logging_raw:
            data["logging"] = _build_logging_config(logging_raw, base)

        for key in ("whygraph_db", "codegraph_db"):
            if key in data:
                p = Path(data[key])
                data[key] = p if p.is_absolute() else (base / p).resolve()

        known = {f.name for f in fields(cls)}
        for unknown in set(data) - known:
            _log.warning("ignoring unknown key in whygraph.toml: %r", unknown)
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def from_toml(cls, path: Path) -> Config:
        """Load and validate configuration from a TOML file.

        A thin wrapper: :func:`tomllib.load` then :meth:`from_dict` with
        the file's directory as ``base``. Relative ``whygraph_db`` /
        ``codegraph_db`` paths are therefore resolved against the
        *directory containing the config file*, not the current working
        directory - so paths in the TOML remain meaningful regardless of
        where the process is launched.

        Parameters
        ----------
        path : Path
            Path to the TOML file to load.

        Returns
        -------
        Config
            A validated, immutable configuration.

        Raises
        ------
        ConfigError
            If any field fails validation in :meth:`__post_init__`.
        FileNotFoundError
            If ``path`` does not exist (callers should test
            ``path.exists()`` first or fall back to :meth:`defaults`).
        tomllib.TOMLDecodeError
            If the file is not valid TOML.
        """
        with path.open("rb") as f:
            raw = tomllib.load(f)
        return cls.from_dict(raw, path.parent)

    def _task_table(self, task: str) -> AnalyzeConfig | RationaleConfig | ChatConfig:
        """Return the typed table for ``task``; ``ValueError`` if unknown."""
        if task not in TASKS:
            raise ValueError(f"unknown task {task!r}; expected one of {TASKS}")
        return getattr(self, task)

    def _resolve_model(
        self, task: str, provider: str | None
    ) -> tuple[str, str | None, bool]:
        """``(provider, model, pinned)`` for ``task``; see :meth:`model_for`."""
        table = self._task_table(task)
        own_provider = _canonical_provider(table.provider) if table.provider else None
        own_model = table.model or None
        if own_model is not None and own_provider is None:
            # Split only when the table names no provider and the prefix is
            # a known tag, so a 1.x `provider = "openrouter"` +
            # `model = "openai/gpt-4o"` stays one OpenRouter model.
            split = _split_provider_model(own_model)
            if split is not None and split[0] in KNOWN_PROVIDERS:
                own_provider, own_model = split

        task_provider = (
            own_provider or self.llm.default_provider or _LEGACY_DEFAULT_PROVIDER
        )
        if task == "chat" and provider is None and task_provider not in CHAT_PROVIDERS:
            if own_provider is not None:
                raise ConfigError(
                    f"[chat] names {task_provider!r}, which is not a chat provider; "
                    f"use one of {CHAT_PROVIDERS} (ollama and claude-cli support "
                    "analyze/rationale but not tool-calling chat)"
                )
            # Inherited from [llm].model: fall back to the legacy default.
            task_provider = CHAT_PROVIDERS[0]

        resolved = _canonical_provider(provider) if provider else task_provider
        if task == "chat" and resolved not in CHAT_PROVIDERS:
            raise ConfigError(
                f"{resolved!r} is not a chat provider; available: {CHAT_PROVIDERS}"
            )
        if own_model is not None and resolved == task_provider:
            return resolved, own_model, True
        model, pinned = self.llm._default_model(resolved)
        return resolved, model, pinned

    def model_for(self, task: str, *, provider: str | None = None) -> ModelChoice:
        """Resolve the ``(provider, model)`` pair ``task`` runs on.

        The single answer to "which model?" - every LLM call site reads
        it from here. Precedence, highest first:

        1. ``[<task>].model``. Split on the first ``/`` only when that
           table has **no** ``provider`` key and the prefix is a known
           provider tag.
        2. ``[llm].model`` (``"provider/model"``), when its provider is
           the one being resolved.
        3. ``[llm.<provider>].model`` - deprecated, warns at load.
        4. The adapter default.

        The provider is ``[<task>].provider``, else the prefix split off
        ``[<task>].model``, else the provider of ``[llm].model``, else the
        1.x default ``"anthropic"`` - applied last.

        Parameters
        ----------
        task : str
            One of :data:`TASKS`.
        provider : str, optional
            Resolve for this provider instead of the task's own - e.g. the
            default model shown for each provider in the chat picker. The
            task's own ``model`` applies only when it belongs to the same
            provider.

        Returns
        -------
        ModelChoice
            The resolved pair.

        Raises
        ------
        ValueError
            If ``task`` is not one of :data:`TASKS`.
        ConfigError
            For ``task="chat"``, if ``provider`` or ``[chat]`` names a
            provider outside :data:`CHAT_PROVIDERS`. A non-chat provider
            *inherited* from ``[llm].model`` falls back to ``"anthropic"``
            instead.
        """
        resolved, model, _ = self._resolve_model(task, provider)
        return ModelChoice(resolved, model)

    def timeout_for(self, task: str) -> int | None:
        """Per-call timeout, in seconds, for ``task``.

        The deprecated task-level ``timeout_sec`` when set (1.x
        behaviour), else the resolved provider's
        ``[llm.<provider>].timeout_sec``.

        Parameters
        ----------
        task : str
            One of :data:`TASKS`.

        Returns
        -------
        int or None
            The timeout, or ``None`` for a provider that is not built in.
        """
        table = self._task_table(task)
        own = getattr(table, "timeout_sec", None)
        if own is not None:
            return own
        section = self.llm.section(self.model_for(task).provider)
        return getattr(section, "timeout_sec", None) if section else None

    def cache_identity(self, task: str) -> tuple[str, str | None]:
        """The ``(provider, pinned model)`` a result cache keys ``task`` on.

        The model is the resolved one when it was **pinned** - by
        ``[<task>].model`` or ``[llm].model`` - and ``None`` otherwise
        (the rationale cache stores ``None`` as the literal
        ``"default"``). So changing ``[llm].model`` changes the key and a
        stale card is not served, while a 1.x file - whose only model
        source is a task's own ``model`` or a deprecated
        ``[llm.<provider>].model`` - derives exactly the key 1.x did and
        keeps hitting its old rows.

        Parameters
        ----------
        task : str
            One of :data:`TASKS`.

        Returns
        -------
        tuple of (str, str or None)
            The provider tag and the pinned model, if any.
        """
        resolved, model, pinned = self._resolve_model(task, None)
        return resolved, (model if pinned else None)

    @classmethod
    def defaults(cls) -> Config:
        """Return a :class:`Config` populated entirely from defaults.

        Used when no ``whygraph.toml`` is present at the project root.

        Returns
        -------
        Config
            A configuration object with every field set to its default.
        """
        return cls()
