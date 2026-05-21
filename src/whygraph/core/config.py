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
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from whygraph.core.logger import LogLevel

_log = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """Raised when ``whygraph.toml`` is malformed or contains invalid values.

    Distinguishes user-supplied configuration mistakes from unexpected
    runtime errors so callers can surface a clean message instead of a
    stack trace.
    """


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
    api_key: str | None = None
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
    api_key: str | None = None
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
    api_key: str | None = None
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
    """

    model: str = "claude-opus-4-7"
    api_key: str | None = None
    timeout_sec: int = 120


@dataclass(frozen=True, slots=True)
class AnalyzeConfig:
    """Configuration for the LLM-driven commit descriptor.

    Loaded from the ``[analyze]`` table in ``whygraph.toml``. Consumed
    by :class:`whygraph.analyze.LlmDescriptor.from_config` to construct
    a descriptor against an existing :class:`LlmConfig`-backed provider.

    Attributes
    ----------
    provider : str
        Tag of the :class:`whygraph.services.llm.LlmClient` adapter to
        use. Must match one of :attr:`LlmClientFactory.providers` at
        construction time; unknown providers surface as
        :class:`whygraph.services.llm.LlmError` from
        :meth:`~whygraph.analyze.LlmDescriptor.from_config`, not here —
        ``core/config`` deliberately does not import from
        ``services/llm`` to keep the dependency direction clean.
    model : str or None
        Model identifier the analyzer should use. ``None`` (default)
        defers to the provider's own ``[llm.<provider>]`` model;
        otherwise it overrides that model for commit descriptions only.
    max_diff_chars : int
        Cap on diff length before prompting. Diffs longer than this are
        truncated with an explicit marker so the model knows the input
        was clipped. Must be ``>= 1``.
    timeout_sec : int or None
        Per-call timeout forwarded into :class:`CompletionRequest`.
        ``None`` (default) defers to the bound adapter's default.
    """

    provider: str = "anthropic"
    model: str | None = None
    max_diff_chars: int = 50_000
    timeout_sec: int | None = None


@dataclass(frozen=True, slots=True)
class LlmConfig:
    """Aggregate of every per-provider :class:`LlmClient` configuration.

    Populated from ``[llm.<provider>]`` tables in ``whygraph.toml``.
    Each adapter in :mod:`whygraph.services.llm` is constructed from
    its matching sub-attribute via ``Adapter.from_config(cfg.<provider>)``.
    """

    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    openai: OpenAIConfig = field(default_factory=OpenAIConfig)
    deepseek: DeepSeekConfig = field(default_factory=DeepSeekConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    claude_cli: ClaudeCliConfig = field(default_factory=ClaudeCliConfig)


# TOML section name → (Config attribute name, sub-dataclass) so the
# TOML loader can build typed sections from raw dicts in one pass.
_LLM_SECTIONS: tuple[tuple[str, str, type], ...] = (
    ("anthropic", "anthropic", AnthropicConfig),
    ("openai", "openai", OpenAIConfig),
    ("deepseek", "deepseek", DeepSeekConfig),
    ("ollama", "ollama", OllamaConfig),
    # `claude_cli` (Python attr) ↔ `claude-cli` (TOML section) — TOML
    # idiomatically uses dashes; Python identifiers cannot, so we keep
    # both forms and let either one parse.
    ("claude_cli", "claude_cli", ClaudeCliConfig),
    ("claude-cli", "claude_cli", ClaudeCliConfig),
)


def _build_llm_config(raw: dict) -> LlmConfig:
    """Parse a raw ``[llm]`` dict into a typed :class:`LlmConfig`."""
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
        sections[attr_name] = cls(
            **{k: v for k, v in block.items() if k in known_fields}
        )
    for unknown in set(raw) - {n for n, *_ in _LLM_SECTIONS}:
        _log.warning("ignoring unknown key in [llm]: %r", unknown)
    return LlmConfig(**{k: v for k, v in sections.items() if k in known_attrs})


def _build_analyze_config(raw: dict) -> AnalyzeConfig:
    """Parse a raw ``[analyze]`` dict into a typed :class:`AnalyzeConfig`."""
    known = {f.name for f in fields(AnalyzeConfig)}
    for unknown in set(raw) - known:
        _log.warning("ignoring unknown key in [analyze]: %r", unknown)
    return AnalyzeConfig(**{k: v for k, v in raw.items() if k in known})


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable runtime configuration for the WhyGraph package.

    Constructed from ``whygraph.toml`` via :meth:`from_toml` or with
    default values via :meth:`defaults`. Validated at construction time
    by :meth:`__post_init__`.

    Attributes
    ----------
    log_level : str
        Logging verbosity; must match a :class:`LogLevel` member name
        (case-insensitive). Default ``"INFO"``.
    rationale_model : str
        Claude model identifier used when generating rationale cards.
        Default ``"claude-opus-4-7"``.
    scan_max_workers : int
        Thread-pool size for the scan phase. Must be ``>= 1``.
        Default ``2``.
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
    analyze : AnalyzeConfig
        Settings for the LLM commit descriptor. Loaded from the
        ``[analyze]`` table; consumed by
        :meth:`whygraph.analyze.LlmDescriptor.from_config`.
    """

    log_level: str = "INFO"
    rationale_model: str = "claude-opus-4-7"
    scan_max_workers: int = 2
    whygraph_db: Path | None = None
    codegraph_db: Path | None = None
    llm: LlmConfig = field(default_factory=LlmConfig)
    analyze: AnalyzeConfig = field(default_factory=AnalyzeConfig)

    def __post_init__(self) -> None:
        """Validate field values immediately after construction.

        Raises
        ------
        ConfigError
            If ``log_level`` is not a known :class:`LogLevel` name, if
            ``scan_max_workers`` is less than ``1``, or if
            ``analyze.max_diff_chars`` is less than ``1``.
        """
        try:
            LogLevel[self.log_level.upper()]
        except KeyError as exc:
            raise ConfigError(f"invalid log_level: {self.log_level!r}") from exc
        if self.scan_max_workers < 1:
            raise ConfigError(
                f"scan_max_workers must be >= 1, got {self.scan_max_workers}"
            )
        if self.analyze.max_diff_chars < 1:
            raise ConfigError(
                "analyze.max_diff_chars must be >= 1, "
                f"got {self.analyze.max_diff_chars}"
            )

    @classmethod
    def from_toml(cls, path: Path) -> Config:
        """Load and validate configuration from a TOML file.

        Relative ``whygraph_db`` / ``codegraph_db`` paths are resolved
        against the *directory containing the config file*, not the
        current working directory — so paths in the TOML remain
        meaningful regardless of where the process is launched.

        Unknown top-level and ``[scan]`` keys produce a warning on the
        ``whygraph.core.config`` logger and are otherwise ignored, to
        preserve forward compatibility with future fields.

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

        scan = raw.pop("scan", {}) or {}
        if "max_workers" in scan:
            raw["scan_max_workers"] = scan.pop("max_workers")
        for unknown in scan:
            _log.warning("ignoring unknown key in [scan]: %r", unknown)

        llm_raw = raw.pop("llm", {}) or {}
        if llm_raw:
            raw["llm"] = _build_llm_config(llm_raw)

        analyze_raw = raw.pop("analyze", {}) or {}
        if analyze_raw:
            raw["analyze"] = _build_analyze_config(analyze_raw)

        base = path.parent
        for key in ("whygraph_db", "codegraph_db"):
            if key in raw:
                p = Path(raw[key])
                raw[key] = p if p.is_absolute() else (base / p).resolve()

        known = {f.name for f in fields(cls)}
        for unknown in set(raw) - known:
            _log.warning("ignoring unknown key in whygraph.toml: %r", unknown)
        return cls(**{k: v for k, v in raw.items() if k in known})

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
