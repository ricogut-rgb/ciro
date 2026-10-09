"""The model router. The swap point.

The model is a swappable dependency behind one interface. Config chooses the backend, per
role (a strong model for the director; cheap/local ones for workers). The router unifies the
CALL, not the CAPABILITY.
"""
from __future__ import annotations

from typing import Callable, Protocol


class Model(Protocol):
    """One call, any provider."""
    name: str

    def complete(self, system: str, prompt: str, format: dict | None = None) -> str: ...


class StubModel:
    """Deterministic, no network. A scriptable model for tests — cooperative OR hostile.

    The script decides what the 'model' says. This is how we prove the gate holds even when
    the model actively tries to escape it: point the script at an escape attempt and watch
    the structure refuse it anyway.
    """

    def __init__(self, name: str = "stub", script: Callable[[str, str], str] | None = None):
        self.name = name
        self._script = script or (lambda system, prompt: "")

    def complete(self, system: str, prompt: str, format: dict | None = None) -> str:
        return self._script(system, prompt)


class OllamaModel:
    """Local backend — a real local model via Ollama's HTTP API.

    No data leaves the machine. This is the private/sovereign backend: choosing it is how the
    agnostic runtime becomes the 'under the radar' one. Runs only if Ollama is up locally.

    Supports grammar-constrained decoding via `format`: pass a JSON Schema and Ollama
    converts it to a GBNF grammar, masking invalid tokens at each decode step. The output
    is GUARANTEED to match the schema — a hard, token-level guarantee. Used for the say/do
    envelope and the model-backed classifier to prevent malformed JSON at the source.
    """

    def __init__(self, name: str = "llama3.2", host: str = "http://localhost:11434"):
        self.name = name
        self.host = host

    def complete(self, system: str, prompt: str, format: dict | None = None) -> str:
        import json
        import urllib.request

        body: dict = {
            "model": self.name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
        }
        if format is not None:
            body["format"] = format
        req = urllib.request.Request(
            f"{self.host}/api/chat", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())["message"]["content"]


class ClaudeModel:
    """Cloud backend — Anthropic Messages API via stdlib urllib.

    Data leaves the machine. The human chooses this by selecting "claude" in the
    console — never automatic. Reads ANTHROPIC_API_KEY from environment.
    No grammar-constrained decoding — format is accepted but ignored; the salvage
    path in runtime.py handles malformed output.
    """

    def __init__(self, name: str = "claude-haiku-4-5-20251001"):
        import os
        self.name = name
        self._key = os.environ.get("ANTHROPIC_API_KEY", "")

    def complete(self, system: str, prompt: str, format: dict | None = None) -> str:
        import json
        import urllib.request

        body = json.dumps({
            "model": self.name,
            "max_tokens": 1024,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self._key,
                "anthropic-version": "2023-06-01",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
        return resp["content"][0]["text"]


class CloudAdapter:
    """Provider interface — Claude / GPT plug in here.

    Pluggable, NOT exercised without a key. Wiring a cloud backend means data LEAVES the
    machine and may cost money — a config choice the human owns, never made automatically.
    Cloud models may not support constrained decoding — format is accepted but ignored;
    the salvage path in runtime.py handles malformed output.
    """

    def __init__(self, name: str, complete_fn: Callable[[str, str], str]):
        self.name = name
        self._fn = complete_fn

    def complete(self, system: str, prompt: str, format: dict | None = None) -> str:
        return self._fn(system, prompt)


class Router:
    """Holds the active backend. Swap by config; the layers above never change."""

    def __init__(self, model: Model):
        self.model = model

    @property
    def backend(self) -> str:
        return self.model.name

    def complete(self, system: str, prompt: str, format: dict | None = None) -> str:
        return self.model.complete(system, prompt, format)
