"""A scripted OpenAI-compatible LLM for the screenshot audit (``e2e/audit/``).

Like ``tests/llm_fake.py`` (loopback only, standard library only) but it can
also **call tools**, so a chat in the audit shows tool cards and a chart. The
script is picked by a marker in the latest user message:

* ``[[chart]]`` - three model calls: ``search_symbols`` + ``run_graph_stats``
  in parallel, then ``render_chart`` on the ``chart_ref`` the stats result
  carried, then a Markdown answer.
* ``[[error]]`` - ``500`` (the chat's provider-error state).
* ``[[slow]]`` - the reply is streamed slowly (about 20 s), so a screenshot can
  catch a turn in flight.
* anything else - a short Markdown reply.

Every streamed answer reports ``usage`` when ``stream_options.include_usage``
is set, so each call is one usage-ledger row. Never imported by ``src/``.

Run: ``python3 e2e/audit/llm_script.py --host 127.0.0.1 --port 18769``.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "e2e-audit-model"

GRAPH_SQL = (
    "SELECT name, SUM(end_line - start_line + 1) AS lines "
    "FROM nodes GROUP BY name ORDER BY lines DESC"
)

PLAIN = (
    "This repository is small: one module with an **entry point** and a helper it "
    "calls. Ask me about a symbol, a file or how something changed and I will look "
    "it up."
)

FINAL = (
    "Here is what the code graph says:\n\n"
    "- The largest symbol is the module file itself.\n"
    "- `main` calls `helper`; nothing else calls either.\n\n"
    "The chart above shows lines per symbol."
)


class State:
    """A call counter, so usage varies a little from call to call."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls = 0

    def usage(self) -> dict:
        with self.lock:
            self.calls += 1
            n = self.calls
        prompt = 9_000 + (n % 5) * 1_500
        completion = 400 + (n % 3) * 350
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "prompt_tokens_details": {"cached_tokens": 2_000 if n % 2 else 0},
        }


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def plan(messages: list[dict]) -> tuple[str, object]:
    """Return ``("text", str)``, ``("tools", [calls])`` or ``("error", None)``."""
    last_user = max(
        (i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1
    )
    user = _text(messages[last_user].get("content")) if last_user >= 0 else ""
    after = messages[last_user + 1 :]
    if "[[error]]" in user:
        return "error", None
    if "[[chart]]" not in user:
        return ("slow" if "[[slow]]" in user else "text"), PLAIN
    called = [
        c.get("function", {}).get("name")
        for m in after
        if m.get("role") == "assistant"
        for c in (m.get("tool_calls") or [])
    ]
    if not called:
        return "tools", [
            ("search_symbols", {"query": "main"}),
            ("run_graph_stats", {"sql": GRAPH_SQL}),
        ]
    if "render_chart" not in called:
        ref = None
        for m in after:
            if m.get("role") == "tool":
                found = re.search(
                    r'"chart_ref"\s*:\s*"([^"]+)"', _text(m.get("content"))
                )
                if found:
                    ref = found.group(1)
        if ref:
            return "tools", [
                (
                    "render_chart",
                    {
                        "chart_ref": ref,
                        "kind": "bar",
                        "title": "Lines per symbol",
                        "x": "name",
                        "y": "lines",
                        "y_label": "lines",
                    },
                )
            ]
    return "text", FINAL


def chunk(cid: str, created: int, choices: list, usage: dict | None = None) -> dict:
    out = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": MODEL,
        "choices": choices,
    }
    if usage is not None:
        out["usage"] = usage
    return out


def make_handler(state: State) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:  # noqa: A002
            pass

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
                        "data": [{"id": MODEL, "object": "model", "owned_by": "audit"}],
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
            kind, payload = plan(request.get("messages") or [])
            if kind == "error":
                self._json(
                    500,
                    {
                        "error": {
                            "message": "The audit model is down (scripted).",
                            "type": "server_error",
                        }
                    },
                )
                return
            usage = state.usage()
            cid = f"chatcmpl-audit-{state.calls}"
            created = int(time.time())
            if not request.get("stream"):
                message = {
                    "role": "assistant",
                    "content": payload if isinstance(payload, str) else "",
                }
                self._json(
                    200,
                    {
                        "id": cid,
                        "object": "chat.completion",
                        "created": created,
                        "model": MODEL,
                        "choices": [
                            {"index": 0, "message": message, "finish_reason": "stop"}
                        ],
                        "usage": usage,
                    },
                )
                return
            frames = [
                chunk(
                    cid,
                    created,
                    [{"index": 0, "delta": {"role": "assistant", "content": ""}}],
                )
            ]
            if kind == "tools":
                for i, (name, args) in enumerate(payload):
                    frames.append(
                        chunk(
                            cid,
                            created,
                            [
                                {
                                    "index": 0,
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": i,
                                                "id": f"call_{state.calls}_{i}",
                                                "type": "function",
                                                "function": {
                                                    "name": name,
                                                    "arguments": json.dumps(args),
                                                },
                                            }
                                        ]
                                    },
                                }
                            ],
                        )
                    )
                finish = "tool_calls"
            else:
                for i, word in enumerate(payload.split(" ")):
                    frames.append(
                        chunk(
                            cid,
                            created,
                            [
                                {
                                    "index": 0,
                                    "delta": {
                                        "content": word if i == 0 else " " + word
                                    },
                                }
                            ],
                        )
                    )
                finish = "stop"
            frames.append(
                chunk(
                    cid, created, [{"index": 0, "delta": {}, "finish_reason": finish}]
                )
            )
            if (request.get("stream_options") or {}).get("include_usage"):
                frames.append(chunk(cid, created, [], usage))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            pause = 20.0 / max(len(frames), 1) if kind == "slow" else 0.0
            try:
                for f in frames:
                    self.wfile.write(f"data: {json.dumps(f)}\n\n".encode())
                    self.wfile.flush()
                    if pause:
                        time.sleep(pause)
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18769)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(State()))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
