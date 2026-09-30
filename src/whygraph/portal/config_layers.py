"""The ``project_config`` rows: config v2 layers and the policy applied on write.

Two kinds of layer exist: the **global defaults** (``project_id`` NULL,
one row) and one row per project. Both hold a config v2 dict (see
:func:`whygraph.core.config.normalize_v2`) with **no secrets**; secrets
live in :mod:`whygraph.portal.secrets`. :mod:`whygraph.portal.context`
merges the two layers when it builds a project's context.

Policy applied here (plan section 4.2.1)
----------------------------------------
* Rule 4: an ``api_key`` or ``token`` key anywhere in the dict is
  rejected (:class:`ConfigPolicyError`), so a secret can never reach
  ``WHYGRAPH_CONFIG_JSON`` or the database in the clear.
* Rule 3 (write half): changing or removing a provider's endpoint
  (``[llm.<provider>].base_url`` / ``host``) **clears that scope's key**
  for the provider, so a saved key never follows an edited endpoint.
  The context half (no global key into a project that overrides the
  endpoint) is in :mod:`whygraph.portal.context`.

The allowlists (rules 1a, 1b, 6) belong to the HTTP endpoints, which know
whether a dict came from an import or from the UI.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping

from sqlmodel import Session, select

from .models import ProjectConfig, _now
from .secrets import LLM_API_KEY, LLM_KEY_PROVIDERS, delete_secret

ENDPOINT_KEYS: tuple[str, ...] = ("base_url", "host")
"""``[llm.<provider>]`` keys that point somewhere (an API or server URL)."""

SECRET_KEYS: tuple[str, ...] = ("api_key", "token")
"""Keys that must never appear in a stored config dict."""


class ConfigPolicyError(ValueError):
    """A config dict broke the portal config policy (HTTP 422 in the endpoints)."""


def find_secret_paths(config: Mapping[str, Any], _prefix: str = "") -> list[str]:
    """Return the dotted path of every secret key in ``config``.

    Parameters
    ----------
    config : Mapping
        A config dict, possibly nested.

    Returns
    -------
    list of str
        E.g. ``["llm.anthropic.api_key", "scan.token"]``; empty when clean.
        Values are never included.
    """
    found: list[str] = []
    for key, value in config.items():
        path = f"{_prefix}{key}"
        if key in SECRET_KEYS:
            found.append(path)
        elif isinstance(value, Mapping):
            found.extend(find_secret_paths(value, f"{path}."))
    return found


def endpoint_of(config: Mapping[str, Any], provider_attr: str) -> str | None:
    """Return the endpoint ``config`` sets for one ``[llm.<provider>]`` table.

    Parameters
    ----------
    config : Mapping
        A (normalized or raw) layer.
    provider_attr : str
        The table name, e.g. ``"openai"`` or ``"claude_cli"``.

    Returns
    -------
    str or None
        The ``base_url`` / ``host`` value, or ``None`` when unset.
    """
    llm = config.get("llm")
    table = llm.get(provider_attr) if isinstance(llm, Mapping) else None
    if not isinstance(table, Mapping):
        return None
    for key in ENDPOINT_KEYS:
        if table.get(key) is not None:
            return str(table[key])
    return None


def get_layer_row(session: Session, project_id: int | None) -> ProjectConfig | None:
    """Return the ``project_config`` row of a scope, or ``None``."""
    stmt = select(ProjectConfig)
    stmt = stmt.where(
        ProjectConfig.project_id.is_(None)  # type: ignore[union-attr]
        if project_id is None
        else ProjectConfig.project_id == project_id
    )
    return session.exec(stmt).first()


def load_layer(session: Session, project_id: int | None) -> dict[str, Any]:
    """Return a scope's stored config dict (``{}`` when there is no row).

    Parameters
    ----------
    session : Session
        A portal DB session.
    project_id : int or None
        ``None`` for the global defaults.

    Returns
    -------
    dict
        A copy of the stored dict; mutating it changes nothing.
    """
    row = get_layer_row(session, project_id)
    return {} if row is None else copy.deepcopy(row.config)


def save_layer(
    session: Session, project_id: int | None, config: Mapping[str, Any]
) -> None:
    """Store a scope's config dict, replacing the previous one.

    Saving the global defaults twice leaves one row. The caller commits
    and, if it holds a :class:`whygraph.portal.context.ContextCache`,
    invalidates it (everything for the global scope, else that project).

    Parameters
    ----------
    session : Session
        A portal DB session.
    project_id : int or None
        ``None`` for the global defaults.
    config : Mapping
        The whole new dict (a JSON ``null`` inside it resets a key at
        merge time).

    Raises
    ------
    ConfigPolicyError
        If the dict contains ``api_key`` / ``token`` (rule 4). Nothing is
        written and no key is cleared.
    """
    secrets_found = find_secret_paths(config)
    if secrets_found:
        raise ConfigPolicyError(
            "secrets are stored separately, not in config: " + ", ".join(secrets_found)
        )
    new = copy.deepcopy(dict(config))
    row = get_layer_row(session, project_id)
    old = row.config if row is not None else {}

    for tag in LLM_KEY_PROVIDERS:
        attr = tag.replace("-", "_")
        if endpoint_of(old, attr) != endpoint_of(new, attr):
            delete_secret(
                session, kind=LLM_API_KEY, provider=tag, project_id=project_id
            )

    if row is None:
        session.add(ProjectConfig(project_id=project_id, config=new))
    else:
        row.config = new
        row.updated_at = _now()
    session.flush()


__all__ = [
    "ConfigPolicyError",
    "ENDPOINT_KEYS",
    "SECRET_KEYS",
    "endpoint_of",
    "find_secret_paths",
    "get_layer_row",
    "load_layer",
    "save_layer",
]
