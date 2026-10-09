"""Natural-language correction path — the human half of the selector.

Every model-backed classification is a READING the person can correct — never a verdict.
Corrections are detected by pattern, resolved to a target atom, and executed on the canvas.

Covers two operations:
  PROMOTE  — a flowing say that should be on the canvas  ("put that on the canvas")
  REMOVE   — a firming atom that shouldn't be there      ("forget that")
"""
from __future__ import annotations

import html
import re
from enum import Enum

from .canvas import Atom


class CorrectionType(Enum):
    PROMOTE = "promote"
    REMOVE = "remove"


_REF = r"(?:that|it|this|the last (?:one|thing))"

_PROMOTE_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\b(?:put|add|move) " + _REF + r" (?:on|to|in) (?:the )?canvas\b",
        r"\b" + _REF + r" (?:belongs?|should be) (?:on|in) (?:the )?canvas\b",
        r"\bcanvas " + _REF + r"\b",
        r"\b(?:save|keep) " + _REF + r" (?:on|to|in) (?:the )?canvas\b",
        r"\b" + _REF + r"(?:'s| is) (?:part of )?(?:the )?creation\b",
        r"\bput " + _REF + r" (?:on|in) (?:the )?canvas\b",
    ]
]

_REMOVE_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\b(?:remove|delete|discard|drop|clear) " + _REF + r" (?:from (?:the )?canvas\b|$)",
        r"^\s*(?:remove|delete|discard|drop|clear) " + _REF + r"\s*[.!?]?\s*$",
        r"^\s*forget " + _REF + r"\s*[.!?]?\s*$",
        r"\btake " + _REF + r" off (?:the )?canvas\b",
        r"\b" + _REF + r" (?:doesn'?t|does not|shouldn'?t|should not) belong (?:on |in )?(?:the )?canvas\b",
        r"\b" + _REF + r" (?:shouldn'?t|should not) be (?:on |in )?(?:the )?canvas\b",
        r"^\s*undo " + _REF + r"\s*[.!?]?\s*$",
        r"\b(?:remove|delete|drop|clear) (?:the )?last (?:one|thing|atom|item) (?:from (?:the )?canvas\b|$)",
        r"^\s*(?:remove|delete|drop|clear) (?:the )?last (?:one|thing|atom|item)\s*[.!?]?\s*$",
    ]
]


def detect_correction(text: str) -> CorrectionType | None:
    """Detect if a message is a canvas correction.  Returns the type, or None."""
    if not isinstance(text, str) or not text.strip():
        return None
    for p in _PROMOTE_PATTERNS:
        if p.search(text):
            return CorrectionType.PROMOTE
    for p in _REMOVE_PATTERNS:
        if p.search(text):
            return CorrectionType.REMOVE
    return None


_CORRECTION_RESPONSES = frozenset({
    "Done.", "Removed.", "Nothing to remove.", "I don't see anything to add.",
})


def resolve_promote_target(turns: list[dict], canvas: list[dict]) -> str | None:
    """Find the most recent 7-say that isn't already on the canvas."""
    canvas_contents: set[str] = set()
    for a in canvas:
        c = a.get("content", "")
        if c:
            canvas_contents.add(c.strip())

    for turn in reversed(turns):
        if turn.get("who") == "7":
            raw = turn.get("text", "")
            text = html.unescape(raw).strip()
            if text and text not in canvas_contents and text not in _CORRECTION_RESPONSES:
                return text
    return None


def resolve_remove_target(canvas: list[dict]) -> dict | None:
    """Find the most recent firming atom on the canvas (the one the person likely means)."""
    firming = [a for a in canvas if a.get("significance") == "firming"]
    return firming[-1] if firming else None


def execute_promote(content: str, canvas: list[dict], turn_index: int = 0) -> dict:
    """Add a promoted atom to the canvas as firming."""
    atom = Atom(content=content, significance="firming", kind="promoted", source_turn=turn_index)
    d = atom.to_dict()
    canvas.append(d)
    return d


def execute_remove(atom_id: str, canvas: list[dict]) -> bool:
    """Remove an atom from the canvas by atom_id."""
    for i, a in enumerate(canvas):
        if a.get("atom_id") == atom_id:
            canvas.pop(i)
            return True
    return False
