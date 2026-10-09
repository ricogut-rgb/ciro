"""The seam-grounded heuristic classifier (scaffold).

The said/done seam is the structural floor:
  - confirmed do  → formed  (something real happened, admitted by the person)
  - say from 7    → flowing (ephemeral conversation)
  - human message → flowing (the trigger, not the creation)

This classifier ADDS to that floor: it looks inside says to detect creation-relevant content
and promotes those from flowing → firming. The classification problem reduces to one question
per say: is this part of the creation, or just process?

Constraint: under-firm rather than over-firm. A false negative (missing a creation-say) is
fine — the human can promote later. A false positive (over-firming a process-say) puts noise
into the firm zone, which is worse.
"""
from __future__ import annotations

import re

from .canvas import Atom, Significance

_STRONG_DECISION_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\blet'?s\s+(?:call|name|go with|use|do|make|set|pick|choose)\b",
        r"\bwe'?ll\s+(?:use|call|name|go with|do|make|set|build|create|pick|choose)\b",
        r"\bthe (?:name|title|plan|goal|approach) (?:is|will be)\b",
    ]
]

_WEAK_DECISION_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bdecided?\b",
        r"\bconfirmed?\b",
        r"\bagreed?\b",
    ]
]

_PROCESS_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bmaybe\b",
        r"\bwhat if\b",
        r"\bI(?:'m| am) (?:thinking|wondering|not sure)\b",
        r"\b(?:keep |still )?thinking\b",
        r"\bhmm\b",
        r"\blet me (?:think|consider)\b",
        r"^\s*(?:ok|okay|sure|right|got it|I see)\s*[.!]?\s*$",
    ]
]

_DRAFT_LABEL_RE = re.compile(
    r"^\s*(?:headline|subheadline|tagline|title|body|copy|subject|opening|closing|name|slogan"
    r"|description|summary|overview|outline|intro|conclusion|cta|call to action"
    r"|section|part|step|item|option|version|draft|proposal|concept|idea)\s*:",
    re.IGNORECASE | re.MULTILINE,
)

_MIN_DRAFT_LENGTH = 120
_MIN_DRAFT_LINES = 3


def _is_produced_content(text: str) -> bool:
    """Detect structured/produced content — a draft, plan, ad, email, etc.

    Creation content ≠ decision content. A drafted ad has no "let's call" or "decided"
    but IS creation — it's the thing being built. Structural signals:
    - Labeled sections (Headline:, Body:, Subject:, etc.)
    - Substantial multi-line output (>120 chars, >3 lines)
    - Bulleted/numbered lists within substantial text
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if _DRAFT_LABEL_RE.search(stripped):
        return True
    lines = [l for l in stripped.split("\n") if l.strip()]
    if len(lines) < _MIN_DRAFT_LINES:
        return False
    if len(stripped) < _MIN_DRAFT_LENGTH:
        return False
    bullet_lines = sum(1 for l in lines if re.match(r"^\s*(?:[-*•]|\d+[.)]\s)", l))
    if bullet_lines >= 2 and len(lines) >= 4:
        return True
    if len(lines) >= 5 and len(stripped) >= 200:
        return True
    return False


def _is_creation_content(text: str) -> bool:
    """Does this text contain creation-relevant content?

    Two independent detectors:
    1. Decision content — "let's call it X", "we'll use Y" (original)
    2. Produced content — a draft, plan, ad, email (structural detection)

    Under-firm by design: process signal suppresses weak decisions
    but NOT produced content (a draft with "maybe" in it is still a draft).
    """
    if not isinstance(text, str) or not text.strip():
        return False
    if _is_produced_content(text):
        return True
    has_strong = any(p.search(text) for p in _STRONG_DECISION_PATTERNS)
    has_weak = any(p.search(text) for p in _WEAK_DECISION_PATTERNS)
    is_process = any(p.search(text) for p in _PROCESS_PATTERNS)
    if is_process:
        return has_strong
    return has_strong or has_weak


def classify_turn(
    turn_type: str,
    text: str,
    turn_index: int = 0,
) -> list[Atom]:
    """Classify a conversation turn into canvas atoms.

    turn_type: 'confirmed_do', 'say', or 'human'
    text: the turn's content
    turn_index: which turn in the conversation (for source_turn)

    Returns a list of Atom objects to add to the creation.
    """
    if not isinstance(text, str) or not text.strip():
        return []

    content = text.strip()

    if turn_type == "confirmed_do":
        return [Atom(
            content=content,
            significance="formed",
            kind="action",
            source_turn=turn_index,
        )]

    if turn_type == "say":
        if _is_creation_content(content):
            return [Atom(
                content=content,
                significance="firming",
                kind="draft",
                source_turn=turn_index,
            )]
        return [Atom(
            content=content,
            significance="flowing",
            kind="process",
            source_turn=turn_index,
        )]

    if turn_type == "human":
        if _is_creation_content(content):
            return [Atom(
                content=content,
                significance="firming",
                kind="intent",
                source_turn=turn_index,
            )]
        return [Atom(
            content=content,
            significance="flowing",
            kind="input",
            source_turn=turn_index,
        )]

    return []
