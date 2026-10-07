"""Portal config policy: what an import, a project ``PUT`` and a defaults ``PUT`` may set.

``Config`` accepts keys that point somewhere - provider endpoints, DB
paths, a log file. A committed ``whygraph.toml``
with a ``base_url`` aimed at an attacker would receive the user's key on
the first LLM call, and a ``whygraph_db`` aimed at another project's DB
would be opened and migrated. So each way config reaches the portal DB
has an allowlist (plan section 4.2.1):

* Rule 1a - :data:`IMPORT_ALLOWLIST`: model selection, the
  ``[analyze]`` / ``[rationale]`` / ``[chat]`` tuning keys and
  ``[scan].forge`` / ``remote`` / ``default_branch`` / ``hooks``. Everything
  else in a repo file is dropped and reported; API keys and the scan token
  are moved into the secret store instead (:func:`preview_import`). A
  ``remote`` / ``default_branch`` that ``Config`` would reject (it could
  reach git as an option) is dropped and reported too.
* Rule 1b - :data:`PUT_ALLOWLIST`: 1a plus the connection-only keys
  ``[llm.<provider>].base_url`` / ``host`` / ``timeout_sec``. A value the
  user types in the UI is trusted; changing an endpoint clears that
  scope's key (rule 3, :func:`whygraph.portal.config_layers.save_layer`).
* Rule 6 - :data:`DEFAULTS_ALLOWLIST`: the global defaults hold only
  ``[llm]`` (with the 1b connection keys), ``[analyze]``, ``[rationale]``
  and ``[chat]`` - and are the only layer that holds the org limits of
  :data:`ORG_ONLY_KEYS`, which 1a (hence 1b) leaves out.

In production a project ``PUT`` uses :data:`PRODUCTION_PUT_ALLOWLIST`
(:func:`put_allowlist`), and a *linked* project's
:data:`LINKED_PUT_ALLOWLIST` (M2e): everything but ``[scan].hooks`` is
managed on its platform. Beside the config allowlists,
:func:`allowed_sources` is the one place that says which project sources a
mode accepts.

Layers are passed through :func:`whygraph.core.config.normalize_v2`
before filtering, so a 1.x alias (``[scan].provider``,
``[scan].max_workers``) is judged by its v2 key, and a reference to a removed
provider (``claude-cli``) is dropped with a warning.
"""

from __future__ import annotations

import copy
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Mapping

from whygraph.core.config import (
    CONFIG_FILENAME,
    AnalyzeConfig,
    ChatConfig,
    ConfigError,
    LlmConfig,
    RationaleConfig,
    check_default_branch,
    check_scan_remote,
    normalize_v2,
)

from whygraph.core.safe_paths import UnsafePathError, check_inside

from .secrets import LLM_KEY_PROVIDERS

Spec = Mapping[str, Any]
"""An allowlist: ``True`` allows a key (and its whole value); a nested
mapping allows only the keys it lists."""

_LLM_DEFAULTS = LlmConfig()
PROVIDER_TABLES: dict[str, type] = {
    f.name: type(getattr(_LLM_DEFAULTS, f.name))
    for f in fields(LlmConfig)
    if is_dataclass(getattr(_LLM_DEFAULTS, f.name))
}
"""``[llm.<table>]`` name -> its config dataclass."""

CONNECTION_KEYS: tuple[str, ...] = ("base_url", "host", "timeout_sec")
"""Connection-only provider keys a ``PUT`` may set (rule 1b)."""


ORG_ONLY_KEYS: dict[str, tuple[str, ...]] = {
    "analyze": (
        "agent_descriptions_per_hour",
        "agent_descriptions_per_member_per_hour",
    ),
    "rationale": (
        "agent_generations_per_hour",
        "agent_generations_per_member_per_hour",
    ),
}
"""The org limits on agent LLM spend (M2e plan section 0.1 #7): set by an
owner on the org defaults only - never imported from a repo (a repo is
untrusted input), never set per project. The platform reads them from the
org layer row."""


def _all_fields(cls: type, section: str | None = None) -> dict[str, bool]:
    skip = ORG_ONLY_KEYS.get(section or "", ())
    return {f.name: True for f in fields(cls) if f.name not in skip}


IMPORT_ALLOWLIST: Spec = {
    "llm": {"model": True, **{name: {"model": True} for name in PROVIDER_TABLES}},
    "analyze": _all_fields(AnalyzeConfig, "analyze"),
    "rationale": _all_fields(RationaleConfig, "rationale"),
    "chat": _all_fields(ChatConfig),
    "scan": {"forge": True, "remote": True, "default_branch": True, "hooks": True},
}
"""Rule 1a: what a repo's ``whygraph.toml`` may contribute on import."""

PUT_ALLOWLIST: Spec = {
    **IMPORT_ALLOWLIST,
    "llm": {
        "model": True,
        **{
            name: {
                "model": True,
                **{
                    key: True
                    for key in CONNECTION_KEYS
                    if key in {f.name for f in fields(cls)}
                },
            }
            for name, cls in PROVIDER_TABLES.items()
        },
    },
}
"""Rule 1b: what ``PUT /api/projects/{slug}/config`` may store."""

DEFAULTS_ALLOWLIST: Spec = {
    **{k: v for k, v in PUT_ALLOWLIST.items() if k != "scan"},
    **{
        section: {**PUT_ALLOWLIST[section], **{key: True for key in keys}}
        for section, keys in ORG_ONLY_KEYS.items()
    },
}
"""Rule 6: what ``PUT /api/portal/defaults`` may store - plus, alone among
the allowlists, :data:`ORG_ONLY_KEYS`."""

PRODUCTION_PUT_ALLOWLIST: Spec = {**PUT_ALLOWLIST, "scan": {"forge": True}}
"""Rule 1b in production: ``[scan].remote``, ``default_branch`` and ``hooks``
are fixed for a GitHub App project (M2d-2 plan section 0.2 #21); only the PR
crawl switch ``[scan].forge`` stays writable."""

LINKED_PUT_ALLOWLIST: Spec = {"scan": {"hooks": True}}
"""What a *linked* project's ``PUT .../config`` may store (M2e plan section 4.11).

A project linked to a WhyGraph platform is configured there: the whole
config route is refused (``403 managed_on_platform``) except ``[scan].hooks``,
which is a property of this checkout - which local git hooks keep its
CodeGraph index fresh - and so stays writable here."""


def put_allowlist(mode: str | None) -> Spec:
    """The project ``PUT`` allowlist of a portal in ``mode``.

    Parameters
    ----------
    mode : str or None
        ``"production"`` or ``"local"``.

    Returns
    -------
    Spec
        :data:`PRODUCTION_PUT_ALLOWLIST` in production, else
        :data:`PUT_ALLOWLIST`.
    """
    return PRODUCTION_PUT_ALLOWLIST if mode == "production" else PUT_ALLOWLIST


def allowed_sources(mode: str | None) -> frozenset[str]:
    """The project sources a portal in ``mode`` accepts (M2d-2 plan section 0.2 #15).

    Applied on the server by the add route and by every path that acts on
    a project's source (scans, Initialize): production holds GitHub
    projects only, local mode local folders only.

    Parameters
    ----------
    mode : str or None
        ``"production"`` or ``"local"`` (``None`` before start-up counts as
        local; a degraded portal refuses requests anyway).

    Returns
    -------
    frozenset[str]
        ``{"github"}`` in production, ``{"local", "platform"}`` otherwise.
    """
    if mode == "production":
        return frozenset({"github"})
    return frozenset({"local", "platform"})


def filter_layer(layer: Mapping[str, Any], spec: Spec) -> tuple[dict, list[str]]:
    """Split a config layer into its allowlisted part and the dropped keys.

    Parameters
    ----------
    layer : Mapping
        A (normalized) config layer.
    spec : Mapping
        One of the allowlists above.

    Returns
    -------
    tuple of (dict, list of str)
        A new dict with only the allowed keys (empty tables are left out),
        and the dotted path of every dropped key, sorted. A value where
        a table is expected is dropped as a whole.
    """
    kept: dict = {}
    dropped: list[str] = []
    _filter(layer, spec, "", kept, dropped)
    return kept, sorted(dropped)


def _filter(layer: Mapping, spec: Spec, prefix: str, kept: dict, dropped: list) -> None:
    for key, value in layer.items():
        path = f"{prefix}{key}"
        rule = spec.get(key)
        if rule is True:
            kept[key] = copy.deepcopy(value)
        elif isinstance(rule, Mapping) and isinstance(value, Mapping):
            sub: dict = {}
            _filter(value, rule, f"{path}.", sub, dropped)
            if sub:
                kept[key] = sub
        elif isinstance(rule, Mapping) and value is None:
            kept[key] = None  # a null table resets it, which is always safe
        else:
            dropped.append(path)


# ---------------------------------------------------------------------------
# Import of a repo's whygraph.toml
# ---------------------------------------------------------------------------

_ENDPOINT_HINT = "endpoint not imported from a repo file; re-enter it under Settings"
_DB_PATH_HINT = "the portal always uses the repository's default database path"
_DEFAULT_HINT = "not imported"


@dataclass
class ImportPreview:
    """What importing a repository's ``whygraph.toml`` does.

    Attributes
    ----------
    found : bool
        Whether ``<root>/whygraph.toml`` exists.
    error : str or None
        Why the file could not be read or parsed (nothing is imported).
    layer : dict
        The allowlisted, normalized layer to store (no secrets).
    llm_keys : dict[str, str]
        Provider tag -> API key found in the file, to move into the
        secret store. Never serialized to an API response.
    github_token : str or None
        ``[scan].token`` from the file, likewise moved.
    secrets_moved : list[str]
        Dotted keys of the moved secrets - the lines the user should
        delete from the file.
    dropped : list[dict]
        ``{"key", "hint"}`` per key rule 1a dropped.
    custom_db_paths : list[dict]
        ``{"key", "path", "exists", "message"}`` per custom
        ``whygraph_db`` / ``codegraph_db`` that the portal will not use.
    warnings : list[str]
        Deprecation messages from :func:`normalize_v2`.
    """

    found: bool = False
    error: str | None = None
    layer: dict = field(default_factory=dict)
    llm_keys: dict[str, str] = field(default_factory=dict, repr=False)
    github_token: str | None = field(default=None, repr=False)
    secrets_moved: list[str] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    custom_db_paths: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def report(self) -> dict:
        """Return the API-safe view (no secret values)."""
        return {
            "found": self.found,
            "error": self.error,
            "secrets_moved": list(self.secrets_moved),
            "dropped": [dict(d) for d in self.dropped],
            "custom_db_paths": [dict(d) for d in self.custom_db_paths],
            "warnings": list(self.warnings),
        }


def preview_import(root: Path) -> ImportPreview:
    """Compute what importing ``<root>/whygraph.toml`` would store (rule 1a).

    Reads the file only; nothing is written anywhere. A ``whygraph.toml``
    that is a symlink (e.g. to a sibling repo's gitignored file holding
    keys) is never read: the preview carries an ``error`` instead.

    Parameters
    ----------
    root : Path
        The repository root.

    Returns
    -------
    ImportPreview
        An empty preview when there is no file.
    """
    try:
        path = check_inside(root, CONFIG_FILENAME)
    except UnsafePathError as exc:
        return ImportPreview(found=True, error=f"{CONFIG_FILENAME} not imported: {exc}")
    if not path.is_file():
        return ImportPreview()
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return ImportPreview(found=True, error=f"{CONFIG_FILENAME} not imported: {exc}")

    layer, warnings = normalize_v2(raw, root)
    preview = ImportPreview(found=True, warnings=warnings)

    llm = layer.get("llm")
    if isinstance(llm, dict):
        for name in PROVIDER_TABLES:
            tag = name.replace("_", "-")
            table = llm.get(name)
            if (
                tag in LLM_KEY_PROVIDERS
                and isinstance(table, dict)
                and "api_key" in table
            ):
                value = table.pop("api_key")
                preview.secrets_moved.append(f"llm.{name}.api_key")
                if isinstance(value, str) and value.strip():
                    preview.llm_keys[tag] = value.strip()
    scan = layer.get("scan")
    if isinstance(scan, dict) and "token" in scan:
        value = scan.pop("token")
        preview.secrets_moved.append("scan.token")
        if isinstance(value, str) and value.strip():
            preview.github_token = value.strip()

    for key, default in (
        ("whygraph_db", root / ".whygraph" / "whygraph.db"),
        ("codegraph_db", root / ".codegraph" / "codegraph.db"),
    ):
        value = layer.get(key)
        if value is None or Path(value) == default:
            continue
        custom = Path(value)
        preview.custom_db_paths.append(
            {
                "key": key,
                "path": str(custom),
                "exists": custom.exists(),
                "message": (
                    f"WhyGraph data at {custom} will not be used; the portal reads "
                    f"{default.relative_to(root)}. Move the file there before "
                    "initializing to keep its descriptions, rationale cache and "
                    "chat history."
                ),
            }
        )

    kept, dropped = filter_layer(layer, IMPORT_ALLOWLIST)
    dropped += _drop_unsafe_git_names(kept)
    preview.layer = kept
    for key in sorted(dropped):
        if key.startswith("llm.") and key.rsplit(".", 1)[-1] in ("base_url", "host"):
            hint = _ENDPOINT_HINT
        elif key in ("whygraph_db", "codegraph_db"):
            hint = _DB_PATH_HINT
        elif key in _GIT_NAME_CHECKS:
            hint = _GIT_NAME_HINT
        else:
            hint = _DEFAULT_HINT
        preview.dropped.append({"key": key, "hint": hint})
    return preview


_GIT_NAME_CHECKS = {
    "scan.remote": ("remote", check_scan_remote),
    "scan.default_branch": ("default_branch", check_default_branch),
}
_GIT_NAME_HINT = "not a valid git name (it would reach git as an option)"


def _drop_unsafe_git_names(layer: dict) -> list[str]:
    """Pop ``[scan].remote`` / ``default_branch`` values ``Config`` would reject."""
    scan = layer.get("scan")
    if not isinstance(scan, dict):
        return []
    dropped: list[str] = []
    for key, (name, check) in _GIT_NAME_CHECKS.items():
        value = scan.get(name)
        if value is None:
            continue
        stripped = value.strip() if isinstance(value, str) else value
        if stripped == "":
            continue  # empty means "the default"
        try:
            check(stripped)
        except ConfigError:
            del scan[name]
            dropped.append(key)
    if not scan:
        del layer["scan"]
    return dropped


__all__ = [
    "CONNECTION_KEYS",
    "DEFAULTS_ALLOWLIST",
    "IMPORT_ALLOWLIST",
    "LINKED_PUT_ALLOWLIST",
    "ImportPreview",
    "ORG_ONLY_KEYS",
    "PROVIDER_TABLES",
    "PRODUCTION_PUT_ALLOWLIST",
    "PUT_ALLOWLIST",
    "allowed_sources",
    "filter_layer",
    "preview_import",
    "put_allowlist",
]
