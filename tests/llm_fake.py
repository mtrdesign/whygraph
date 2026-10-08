"""A fake OpenAI-compatible LLM server for the Playwright suite (M2f-2 plan section 6.5).

Loopback only, standard library only. Run as a script::

    uv run --no-sync python tests/llm_fake.py --host 127.0.0.1 --port 18768

and point a portal's ``[llm.openai].base_url`` at ``http://127.0.0.1:18768/v1``.
Routes:

* ``GET /v1/models`` - one model, :data:`MODEL`.
* ``POST /v1/chat/completions`` - a fixed reply (:data:`REPLY`), streamed as
  server-sent events when ``stream`` is true (the shape the chat harness reads:
  a role chunk, content chunks, a ``finish_reason`` chunk and, when
  ``stream_options.include_usage`` is set, a trailing chunk with an empty
  ``choices`` list carrying ``usage``) and as one JSON body otherwise. Every
  chunk names the served ``model``.

The usage is a fixed :data:`PROMPT_TOKENS` / :data:`COMPLETION_TOKENS`, large
enough that a call costs at least $0.01 at any price override of a dollar per
million tokens. Calls alternate: the first (and every odd one) reports
``prompt_tokens_details.cached_tokens``, the second (and every even one) omits
``prompt_tokens_details`` altogether, so both shapes reach the portal's
normaliser.

Never imported by ``src/`` and never copied into the image.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "e2e-stub-model"
"""The one model the fake lists and serves."""

REPLY = "The e2e stub model says hello."
"""The fixed assistant reply."""

PROMPT_TOKENS = 20_000
COMPLETION_TOKENS = 4_000
CACHED_TOKENS = 5_000


class FakeLlm:
    """The fake's state: a call counter that alternates the usage shape."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0

    def next_usage(self) -> dict:
        """Return the usage block of the next call.

        Returns
        -------
        dict
            ``usage`` with ``prompt_tokens_details`` on odd calls only.
        """
        with self._lock:
            self.calls += 1
            odd = self.calls % 2 == 1
        usage: dict = {
            "prompt_tokens": PROMPT_TOKENS,
            "completion_tokens": COMPLETION_TOKENS,
            "total_tokens": PROMPT_TOKENS + COMPLETION_TOKENS,
        }
        if odd:
            usage["prompt_tokens_details"] = {"cached_tokens": CACHED_TOKENS}
        return usage


def _chunk(
    cid: str, created: int, choices: list[dict], usage: dict | None = None
) -> dict:
    body = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": MODEL,
        "choices": choices,
    }
    if usage is not None:
        body["usage"] = usage
    return body


def _make_handler(fake: FakeLlm) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:  # noqa: A002
            print(f"{self.address_string()} {fmt % args}", flush=True)

        def _json(self, status: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?")[0] == "/v1/models":
                self._json(
                    200,
                    {
                        "object": "list",
                        "data": [
                            {
                                "id": MODEL,
                                "object": "model",
                                "created": 0,
                                "owned_by": "e2e",
                            }
                        ],
                    },
                )
            else:
                self._json(404, {"error": {"message": "not found"}})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?")[0] != "/v1/chat/completions":
                self._json(404, {"error": {"message": "not found"}})
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                request = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json(400, {"error": {"message": "bad json"}})
                return
            usage = fake.next_usage()
            cid = f"chatcmpl-e2e-{fake.calls}"
            created = int(time.time())
            if not request.get("stream"):
                self._json(
                    200,
                    {
                        "id": cid,
                        "object": "chat.completion",
                        "created": created,
                        "model": MODEL,
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": REPLY},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": usage,
                    },
                )
                return
            include_usage = bool(
                (request.get("stream_options") or {}).get("include_usage")
            )
            frames = [
                _chunk(
                    cid,
                    created,
                    [{"index": 0, "delta": {"role": "assistant", "content": ""}}],
                ),
            ]
            words = REPLY.split(" ")
            for i, word in enumerate(words):
                text = word if i == 0 else " " + word
                frames.append(
                    _chunk(cid, created, [{"index": 0, "delta": {"content": text}}])
                )
            frames.append(
                _chunk(
                    cid, created, [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                )
            )
            if include_usage:
                frames.append(_chunk(cid, created, [], usage))
            payload = (
                "".join(f"data: {json.dumps(f)}\n\n" for f in frames)
                + "data: [DONE]\n\n"
            )
            raw = payload.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return Handler


def main(argv: list[str] | None = None) -> None:
    """Serve the fake until interrupted.

    Parameters
    ----------
    argv : list of str, optional
        Command-line arguments (default ``sys.argv[1:]``).
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18768)
    args = parser.parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        parser.error("the fake LLM listens on loopback only")
    server = ThreadingHTTPServer((args.host, args.port), _make_handler(FakeLlm()))
    print(f"fake LLM on http://{args.host}:{args.port}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
