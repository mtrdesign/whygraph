"""Waking a reader blocked on a provider stream, from another thread.

Shared by both chat adapters' :meth:`~whygraph.services.llm.chat.ChatClient.abort`.
Closing an ``httpx`` response from another thread does **not** wake a
reader blocked in ``iter_bytes()`` on a silent stream (a long think); it
stays blocked until the read timeout. Shutting the socket down does, at
once, with ``httpx.RemoteProtocolError`` in the reader.
"""

from __future__ import annotations

import socket
from typing import Any


def shutdown_stream(stream: Any) -> None:
    """Shut down the socket under an SDK stream so its reader wakes.

    Parameters
    ----------
    stream : Any
        An ``anthropic`` ``MessageStream`` or an ``openai`` ``Stream`` (both
        expose the ``httpx.Response`` as ``.response``), or ``None``.

    Notes
    -----
    Never closes the stream: the reader's own ``with`` / ``finally`` does
    that on its thread. ``socket.socket.shutdown`` is called as the base
    class method because ``SSLSocket.shutdown`` would tear the TLS object
    down under the reader. Anything that is not a live socket - a fake
    client, an abort before the response headers arrived, a socket already
    closed - is a silent no-op; the harness's per-event cancel check covers
    those cases.
    """
    response = getattr(stream, "response", None)
    try:
        sock = response.extensions["network_stream"].get_extra_info("socket")
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except (AttributeError, KeyError, TypeError, OSError):
        pass
