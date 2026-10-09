"""Load the operator posture (the text the model wears) from the record.

Model-independent: the posture is just text; any model wears it. By default this
reads a fallback stub; set ``AGNOSTIC_POSTURE_PATH`` to a readable file to load
your own posture document.
"""
from __future__ import annotations

import os
from pathlib import Path

_FALLBACK = (
    "You are the operator's assistant. Verify from reality, not from reports. "
    "Do not inflate — the machine, the work, or your own output. Confirm is the "
    "only admit; you may propose, never fire. Route, don't grab. Everything is "
    "for the human and for increased presence."
)


def load_posture(path: str | Path | None = None) -> str:
    """Return the posture text to seed the model with.

    If ``path`` is given and readable, return its contents. Otherwise, check
    ``$AGNOSTIC_POSTURE_PATH``. If neither is set, return the built-in fallback.
    """
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    env_path = os.environ.get("AGNOSTIC_POSTURE_PATH")
    if env_path:
        candidates.append(Path(env_path))
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate.read_text()
    return _FALLBACK
