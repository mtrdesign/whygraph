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
from importlib import resources
from pathlib import Path

from whygraph.core.logger import LogLevel

_log = logging.getLogger(__name__)

CONFIG_FILENAME = "whygraph.toml"
"""Name of the project-root config file loaded by :func:`whygraph.core.get_config`."""

EXAMPLE_CONFIG_FILENAME = "whygraph.example.toml"
"""Name of the committable example config scaffolded by ``whygraph init``.

Users copy it to :data:`CONFIG_FILENAME` and edit; the real ``whygraph.toml``
is gitignored (it may hold API keys), while the example tracks the package
defaults and is safe to commit."""


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
    large_commit_file_count : int
        Commits touching strictly more than this many files are treated
        as *bulk* commits (imports, squash merges, repo-wide sweeps).
        Their whole-diff description is skipped at scan time in favour of
        a cheap stub, and descriptions are instead generated lazily
        per-file on the MCP read path — so a single huge commit does not
        cost a repo-wide LLM pass nor anchor every symbol to one vague
        summary. Must be ``>= 1``.
    timeout_sec : int or None
        Per-call timeout forwarded into :class:`CompletionRequest`.
        ``None`` (default) defers to the bound adapter's default.
    pr_origin_min_commits : int
        Commit-rich half of the squash-merge enrichment gate
        (:mod:`whygraph.scan.pr_origin_enricher`). A squash-merged PR has
        its original feature-branch commits recovered when it collapsed at
        least this many commits (the file-bulk half reuses
        ``large_commit_file_count``). Must be ``>= 1``.
    """

    provider: str = "anthropic"
    model: str | None = None
    max_diff_chars: int = 50_000
    large_commit_file_count: int = 30
    timeout_sec: int | None = None
    pr_origin_min_commits: int = 5


@dataclass(frozen=True, slots=True)
class RationaleConfig:
    """Configuration for the LLM-driven rationale generator.

    Loaded from the ``[rationale]`` table in ``whygraph.toml``. Consumed by
    :meth:`whygraph.analyze.RationaleGenerator.from_config` to construct a
    generator against an existing :class:`LlmConfig`-backed provider.

    Attributes
    ----------
    provider : str
        Tag of the :class:`whygraph.services.llm.LlmClient` adapter to use.
        Must match one of :attr:`LlmClientFactory.providers` at construction
        time; unknown providers surface as
        :class:`whygraph.services.llm.LlmError` from
        :meth:`~whygraph.analyze.RationaleGenerator.from_config`, not here —
        ``core/config`` deliberately does not import from ``services/llm``
        to keep the dependency direction clean.
    model : str or None
        Model identifier the generator should use. ``None`` (default)
        defers to the provider's own ``[llm.<provider>]`` model; otherwise
        it overrides that model for rationale generation only.
    timeout_sec : int or None
        Per-call timeout forwarded into :class:`CompletionRequest`.
        ``None`` (default) defers to the bound adapter's default.
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

    provider: str = "anthropic"
    model: str | None = None
    timeout_sec: int | None = None
    pr_roster_max_commits: int = 30
    pr_discussion_max_comments: int = 20
    pr_comment_max_chars: int = 500


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
    scan_max_workers : int
        Thread-pool size for the scan phase. Must be ``>= 1``.
        Default ``2``.
    scan_provider : str
        Source-control backend the scan crawls for PRs / issues. One of
        ``"off"`` (default — pull nothing), ``"github"`` (pull from the
        GitHub remote), or ``"auto"`` (detect the backend from the remote
        URL; GitHub-only today). Loaded from ``[scan].provider``; an empty
        value is treated as ``"off"``.
    scan_remote : str
        Name of the git remote whose URL is inspected to resolve the
        provider for ``"github"`` / ``"auto"``. Default ``"origin"``.
        Loaded from ``[scan].remote``; an empty value falls back to
        ``"origin"``.
    scan_token : str or None
        GitHub token used to authenticate the ``gh`` CLI during the
        remote crawl. Loaded from ``[scan].token``; an empty value is
        treated as ``None``. When ``None``, the scan falls back to the
        ambient ``GH_TOKEN`` / ``GITHUB_TOKEN`` environment variables (or
        an existing ``gh auth login`` session). Kept per-project so one
        shared scanning container can serve repos across different orgs.
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
    """

    log_level: str = "INFO"
    scan_max_workers: int = 2
    scan_provider: str = "off"
    scan_remote: str = "origin"
    scan_token: str | None = None
    whygraph_db: Path | None = None
    codegraph_db: Path | None = None
    llm: LlmConfig = field(default_factory=LlmConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    analyze: AnalyzeConfig = field(default_factory=AnalyzeConfig)
    rationale: RationaleConfig = field(default_factory=RationaleConfig)

    def __post_init__(self) -> None:
        """Validate field values immediately after construction.

        Raises
        ------
        ConfigError
            If ``log_level`` is not a known :class:`LogLevel` name, if
            ``scan_max_workers`` is less than ``1``, if ``scan_provider``
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
        if self.scan_max_workers < 1:
            raise ConfigError(
                f"scan_max_workers must be >= 1, got {self.scan_max_workers}"
            )
        if self.scan_provider not in {"off", "github", "auto"}:
            raise ConfigError(
                f"invalid scan.provider: {self.scan_provider!r}, "
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

        base = path.parent

        scan = raw.pop("scan", {}) or {}
        if "max_workers" in scan:
            raw["scan_max_workers"] = scan.pop("max_workers")
        if "provider" in scan:
            provider = (scan.pop("provider") or "").strip().lower()
            raw["scan_provider"] = provider or "off"
        if "remote" in scan:
            remote = (scan.pop("remote") or "").strip()
            raw["scan_remote"] = remote or "origin"
        if "token" in scan:
            token = (scan.pop("token") or "").strip()
            raw["scan_token"] = token or None
        for unknown in scan:
            _log.warning("ignoring unknown key in [scan]: %r", unknown)

        llm_raw = raw.pop("llm", {}) or {}
        if llm_raw:
            raw["llm"] = _build_llm_config(llm_raw)

        analyze_raw = raw.pop("analyze", {}) or {}
        if analyze_raw:
            raw["analyze"] = _build_analyze_config(analyze_raw)

        rationale_raw = raw.pop("rationale", {}) or {}
        if rationale_raw:
            raw["rationale"] = _build_rationale_config(rationale_raw)

        logging_raw = raw.pop("logging", {}) or {}
        if logging_raw:
            raw["logging"] = _build_logging_config(logging_raw, base)

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


def default_config_text() -> str:
    """Return the bundled commented default config as text.

    Read from the packaged ``whygraph/core/default_config.toml`` resource
    (same ``importlib.resources`` mechanism as the analyze prompt
    templates). The shown values match the :class:`Config` defaults, so a
    copy with no edits behaves exactly as if no config were present.

    Returns
    -------
    str
        The full template, including comments and a trailing newline.
    """
    return (resources.files("whygraph.core") / "default_config.toml").read_text(
        encoding="utf-8"
    )


def write_example_config(project_root: Path) -> Path:
    """Scaffold :data:`EXAMPLE_CONFIG_FILENAME` into ``project_root``.

    The example is a committable, package-owned reference (like
    ``.env.example``): users copy it to :data:`CONFIG_FILENAME` and edit.
    Because it tracks the package defaults rather than user edits, it is
    **always (re)written** — re-running ``whygraph init`` refreshes it so
    it stays in sync with the shipped defaults.

    Parameters
    ----------
    project_root : Path
        Directory to write the example into (usually the repo root).

    Returns
    -------
    Path
        The path of the written example config.
    """
    path = project_root / EXAMPLE_CONFIG_FILENAME
    path.write_text(default_config_text(), encoding="utf-8")
    return path
