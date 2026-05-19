"""TOML-backed configuration for WhyGraph.

The :class:`Config` value object holds all user-tunable settings. It is
loaded once from ``<project_root>/whygraph.toml`` (see
:func:`whygraph.core.get_config`) or falls back to :meth:`Config.defaults`
if the file is absent.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, fields
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
    """

    log_level: str = "INFO"
    rationale_model: str = "claude-opus-4-7"
    scan_max_workers: int = 2
    whygraph_db: Path | None = None
    codegraph_db: Path | None = None

    def __post_init__(self) -> None:
        """Validate field values immediately after construction.

        Raises
        ------
        ConfigError
            If ``log_level`` is not a known :class:`LogLevel` name, or
            if ``scan_max_workers`` is less than ``1``.
        """
        try:
            LogLevel[self.log_level.upper()]
        except KeyError as exc:
            raise ConfigError(f"invalid log_level: {self.log_level!r}") from exc
        if self.scan_max_workers < 1:
            raise ConfigError(
                f"scan_max_workers must be >= 1, got {self.scan_max_workers}"
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
