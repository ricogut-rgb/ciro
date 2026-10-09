"""The agnostic runtime — thinnest real slice.

The model is swappable behind a router, and the constitution's gate holds regardless of
which model runs above it — including a hostile one. Structure > model, as running code.

Not the full runtime: no trigger service, minimal agent loop, one small action registry.
It exists to prove the swap and the floor end-to-end before anything larger is built
(prove on one cycle before you stamp).
"""
from .router import Router, StubModel, OllamaModel, CloudAdapter
from .gate import Gate, ACTION_REGISTRY
from .runtime import Runtime
from .harness import load_posture

__all__ = [
    "Router", "StubModel", "OllamaModel", "CloudAdapter",
    "Gate", "ACTION_REGISTRY", "Runtime", "load_posture",
]
