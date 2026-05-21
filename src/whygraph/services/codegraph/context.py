"""The graph-context bundle for a single symbol.

Exposes :class:`SymbolContext`: a target :class:`Symbol` together with the
symbols that call it (fan-in) and the symbols it calls (fan-out). Produced by
:meth:`whygraph.services.codegraph.CodeGraph.context`; consumed by the rationale
generator as a structural evidence source.
"""

from __future__ import annotations

from dataclasses import dataclass

from .relation import Relation
from .symbol import Symbol


@dataclass(frozen=True, slots=True)
class SymbolContext:
    """A symbol plus its immediate callers and callees.

    The unit of *structural* evidence about a symbol: what it is, who depends
    on it, and what it depends on. Assembled in one shot by
    :meth:`CodeGraph.context`.

    Attributes
    ----------
    target : Symbol
        The symbol the context is about.
    callers : tuple[Relation, ...]
        Relations whose :attr:`~Relation.symbol` has a ``calls`` edge *into*
        :attr:`target` — its fan-in, the blast radius of a change. Empty when
        nothing calls it.
    callees : tuple[Relation, ...]
        Relations whose :attr:`~Relation.symbol` :attr:`target` has a ``calls``
        edge *to* — its fan-out, what it depends on. Empty when it calls
        nothing.
    """

    target: Symbol
    callers: tuple[Relation, ...]
    callees: tuple[Relation, ...]
