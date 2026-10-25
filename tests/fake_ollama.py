"""A fake Ollama server for tests: real HTTP on 127.0.0.1, so ChatOllama, OllamaEmbeddings and the doctor's urllib client
run their real code. No model, no GPU, no download.

    def test_something(fake_ollama):                    # the fixture sets OLLAMA_BASE_URL and LLM_PROVIDER=ollama
        fake_ollama.reply("Hello")                      # the next /api/chat answers with this text
        fake_ollama.reply_tool_call("ping", {"x": 1})   # ... or with a tool call
        fake_ollama.reply_json({"category": "Fuel"})    # ... or JSON (what a call with `format` expects)
        fake_ollama.fail_next(500, "boom")              # ... or an HTTP error
        fake_ollama.delay = 2.0                         # every answer waits this many seconds (timeout tests)
        fake_ollama.add_model("tiny:1b", ["completion"])   # installed, but without tool support
        fake_ollama.calls                               # [(method, path, json_body), ...] everything it was asked

Defaults: models qwen2.5:7b, qwen2.5:3b, qwen2.5-coder:7b (completion + tools) and nomic-embed-text (embedding).
An /api/chat with `tools` for a model without the `tools` capability answers 400 "does not support tools", as Ollama does.
An unknown model answers 404 `model "x" not found, try pulling it first`. With nothing queued, /api/chat answers "OK".
/api/embed returns bag-of-words vectors (tests/support/fakes.bow_vector), so similar texts are close.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from support.fakes import bow_vector

DEFAULT_MODELS = {
    "qwen2.5:7b": ["completion", "tools"],
    "qwen2.5:3b": ["completion", "tools"],
    "qwen2.5-coder:7b": ["completion", "tools"],
    "nomic-embed-text:latest": ["embedding"],
}
VERSION = "0.12.0"


class FakeOllama:
    def __init__(self) -> None:
        self.models: dict[str, list[str]] = dict(DEFAULT_MODELS)
        self.loaded: list[str] = []                     # what /api/ps reports as in memory
        self.queue: list[dict] = []                     # scripted answers for /api/chat
        self.calls: list[tuple[str, str, dict | None]] = []
        self.delay: float = 0.0
        self._fail: list[tuple[int, str]] = []
        self._lock = threading.Lock()
        self.server: ThreadingHTTPServer | None = None
        self.base_url = ""

    # ── scripting ──
    def reply(self, text: str) -> None:
        self.queue.append({"kind": "text", "content": text})

    def reply_tool_call(self, name: str, arguments: dict, content: str = "") -> None:
        self.queue.append({"kind": "tool", "content": content, "tool_calls": [{"function": {"name": name, "arguments": arguments}}]})

    def reply_json(self, obj) -> None:
        self.queue.append({"kind": "text", "content": json.dumps(obj)})

    def fail_next(self, status: int, message: str) -> None:
        self._fail.append((status, message))

    def add_model(self, name: str, capabilities: list[str]) -> None:
        self.models[name] = list(capabilities)

    def remove_model(self, name: str) -> None:
        self.models.pop(name, None)

    def paths(self) -> list[str]:
        return [path for _method, path, _body in self.calls]

    def bodies(self, path: str) -> list[dict]:
        return [body or {} for _m, p, body in self.calls if p == path]

    # ── model lookup: "qwen2.5:7b", or a bare name that matches "<name>:latest" ──
    def find(self, name: str) -> str | None:
        if name in self.models:
            return name
        if ":" not in name and f"{name}:latest" in self.models:
            return f"{name}:latest"
        return None

    # ── server ──
    def start(self) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):       # keep the test output quiet
                pass

            def _body(self) -> dict | None:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return None
                try:
                    return json.loads(self.rfile.read(length))
                except ValueError:
                    return None

            def _send(self, status: int, payload: dict) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _stream(self, chunks: list[dict]) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for chunk in chunks:
                    line = json.dumps(chunk).encode() + b"\n"
                    self.wfile.write(f"{len(line):x}\r\n".encode() + line + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")

            def do_GET(self):
                self._route("GET", None)

            def do_POST(self):
                self._route("POST", self._body())

            def do_DELETE(self):
                self._route("DELETE", self._body())

            def _route(self, method: str, body: dict | None) -> None:
                path = self.path.split("?")[0]
                with fake._lock:
                    fake.calls.append((method, path, body))
                if fake.delay:
                    time.sleep(fake.delay)
                if fake._fail and path in ("/api/chat", "/api/embed", "/api/show", "/api/tags", "/api/version", "/api/ps"):
                    with fake._lock:
                        status, message = fake._fail.pop(0)
                    return self._send(status, {"error": message})
                if path == "/api/version" and method == "GET":
                    return self._send(200, {"version": VERSION})
                if path == "/api/tags" and method == "GET":
                    return self._send(200, {"models": [
                        {"name": n, "model": n, "modified_at": "2026-01-01T00:00:00Z", "size": 1_000_000, "digest": "0" * 64,
                         "details": {"family": "fake", "parameter_size": "7B", "quantization_level": "Q4_K_M"}}
                        for n in fake.models]})
                if path == "/api/ps" and method == "GET":
                    return self._send(200, {"models": [{"name": n, "model": n, "size": 1_000_000, "size_vram": 0} for n in fake.loaded]})
                if path == "/api/show" and method == "POST":
                    return self._show(body or {})
                if path == "/api/chat" and method == "POST":
                    return self._chat(body or {})
                if path == "/api/embed" and method == "POST":
                    return self._embed(body or {})
                self._send(404, {"error": f"fake ollama has no {method} {path}"})

            def _missing(self, name: str) -> None:
                self._send(404, {"error": f'model "{name}" not found, try pulling it first'})

            def _show(self, body: dict) -> None:
                name = body.get("model") or body.get("name") or ""
                found = fake.find(name)
                if found is None:
                    return self._missing(name)
                self._send(200, {"modelfile": f"FROM {found}", "parameters": "", "template": "",
                                 "details": {"family": "fake"}, "model_info": {}, "capabilities": fake.models[found]})

            def _embed(self, body: dict) -> None:
                name = body.get("model") or ""
                found = fake.find(name)
                if found is None:
                    return self._missing(name)
                if "embedding" not in fake.models[found]:
                    return self._send(400, {"error": f'"{name}" does not support embeddings'})
                texts = body.get("input", [])
                texts = [texts] if isinstance(texts, str) else list(texts)
                self._send(200, {"model": found, "embeddings": [bow_vector(t) for t in texts],
                                 "total_duration": 1, "load_duration": 1, "prompt_eval_count": len(texts)})

            def _chat(self, body: dict) -> None:
                name = body.get("model") or ""
                found = fake.find(name)
                if found is None:
                    return self._missing(name)
                if body.get("tools") and "tools" not in fake.models[found]:
                    return self._send(400, {"error": f"registry.ollama.ai/library/{found}: does not support tools"})
                scripted = fake.queue.pop(0) if fake.queue else {"kind": "text", "content": "OK"}
                message = {"role": "assistant", "content": scripted.get("content", "")}
                if scripted.get("tool_calls"):
                    message["tool_calls"] = scripted["tool_calls"]
                stats = {"model": found, "created_at": "2026-01-01T00:00:00Z", "total_duration": 1, "load_duration": 1,
                         "prompt_eval_count": 10, "prompt_eval_duration": 1, "eval_count": 5, "eval_duration": 1}
                if body.get("stream", True):
                    pieces = message["content"].split(" ")
                    chunks = [{"model": found, "created_at": stats["created_at"], "done": False,
                               "message": {"role": "assistant", "content": piece + (" " if i < len(pieces) - 1 else "")}}
                              for i, piece in enumerate(pieces) if piece]
                    if message.get("tool_calls"):
                        chunks.append({"model": found, "created_at": stats["created_at"], "done": False,
                                       "message": {"role": "assistant", "content": "", "tool_calls": message["tool_calls"]}})
                    chunks.append({**stats, "done": True, "done_reason": "stop", "message": {"role": "assistant", "content": ""}})
                    return self._stream(chunks)
                self._send(200, {**stats, "done": True, "done_reason": "stop", "message": message})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


@pytest.fixture
def fake_ollama(monkeypatch):
    """A running FakeOllama. Points OLLAMA_BASE_URL at it, selects the ollama provider, and keeps proxies out of the way."""
    fake = FakeOllama()
    fake.start()
    monkeypatch.setenv("OLLAMA_BASE_URL", fake.base_url)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    try:
        yield fake
    finally:
        fake.stop()
