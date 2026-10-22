"""Structured output for the WhyGraph MCP tools.

FastMCP turns a ``TypedDict`` return type into an ``outputSchema`` and
``structuredContent``, but it builds the content by dumping a pydantic
model, which fills every *absent* optional key with ``null``: the
structured content would then differ from the text block and break the
schema. :func:`structured` returns the ``CallToolResult`` itself, so both
blocks carry exactly the keys the tool produced.
"""

from __future__ import annotations

import functools
import inspect
from typing import Annotated, Any, Callable

import pydantic_core
from mcp.types import CallToolResult, TextContent


def structured(schema: type) -> Callable[[Callable[..., dict]], Callable[..., Any]]:
    """Wrap a tool body so its dict result is sent as structured content.

    Parameters
    ----------
    schema : type
        The ``TypedDict`` describing the result; becomes the tool's
        ``outputSchema``.

    Returns
    -------
    callable
        A decorator. The wrapped function returns a ``CallToolResult``
        whose text block is the ``indent=2`` JSON FastMCP would have
        written for the dict, and whose ``structuredContent`` is the dict
        itself (identical keys). Its signature is the original's, apart
        from the return annotation, so the input schema is unchanged.
    """

    def decorate(fn: Callable[..., dict]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> CallToolResult:
            result = fn(*args, **kwargs)
            text = pydantic_core.to_json(result, fallback=str, indent=2).decode()
            return CallToolResult(
                content=[TextContent(type="text", text=text)],
                structuredContent=result,
            )

        wrapper.__signature__ = inspect.signature(fn, eval_str=True).replace(  # type: ignore[attr-defined]
            return_annotation=Annotated[CallToolResult, schema]
        )
        return wrapper

    return decorate
