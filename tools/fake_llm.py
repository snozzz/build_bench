#!/usr/bin/env python3
"""Scripted OpenAI-compatible endpoint for testing the Agent's tool loop without a model.

The script is a JSON list; each entry is the assistant message returned for the next
/v1/chat/completions request, e.g.
  [{"tool_calls": [{"id": "1", "type": "function",
                    "function": {"name": "read_file", "arguments": "{\\"path\\": \\"x\\"}"}}]},
   {"content": "done"}]
Requests are appended to <script>.requests.jsonl for inspection.

Usage: fake_llm.py SCRIPT.json [--port 18080]
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("script", type=Path)
    ap.add_argument("--port", type=int, default=18080)
    args = ap.parse_args()
    replies = json.loads(args.script.read_text())
    log = args.script.with_suffix(".requests.jsonl")
    state = {"i": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            with log.open("a") as fh:
                fh.write(body.decode("utf-8") + "\n")
            index = min(state["i"], len(replies) - 1)
            state["i"] += 1
            message = {"role": "assistant", "content": None, **replies[index]}
            data = json.dumps({
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": len(body) // 4, "completion_tokens": 50},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):  # silence
            pass

    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
