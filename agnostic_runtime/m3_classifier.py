"""Model-backed classifier.

The weak model (ollama) is the mutation engine; the structure is the selector.
The model READS each turn and drafts a binary classification: creation or process.
The structural selector validates the model's answer — a say is never formed, content
must be grounded, garbage falls back to the heuristic classifier. The human corrects
errors the structure can't catch (see correction.py).

Binary classification for this checkpoint: whole say → one atom. Extraction (pulling
named pieces) is the evolution path.
"""
from __future__ import annotations

import json
import re

from .canvas import Atom

_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "creation": {"type": "boolean"},
    },
    "required": ["creation"],
}

_CLASSIFY_SYSTEM = "You classify conversation turns. Respond ONLY with JSON, nothing else."

_CLASSIFY_PROMPT = '''\
A person is creating something with help. Read this turn and decide:
is it CREATION CONTENT (a draft, name, plan, tagline, slogan, decision, or anything that IS the thing being built)
or PROCESS (conversation flow, questions, thinking aloud, greetings, acknowledgments)?

Examples:
Turn: "Here's a draft headline: Drive Safe, Save More"
Answer: {"creation": true}

Turn: "That sounds interesting, tell me more"
Answer: {"creation": false}

Turn: "How about we call it SafeGuard Insurance?"
Answer: {"creation": true}

Turn: "I'm still thinking about the best approach"
Answer: {"creation": false}

Turn: "Protect Your Family on the Road — affordable coverage starts here."
Answer: {"creation": true}

Now classify this turn:
Turn: "__TURN_TEXT__"
Answer: '''


def _parse_classification(raw: str) -> bool | None:
    """Parse the model's binary answer.  True = creation, False = process, None = unparseable."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    m = re.search(r"\{[^}]*\}", raw)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict) and "creation" in obj:
                return bool(obj["creation"])
        except (json.JSONDecodeError, ValueError):
            pass
    lower = raw.lower()
    if '"creation": true' in lower or '"creation":true' in lower:
        return True
    if '"creation": false' in lower or '"creation":false' in lower:
        return False
    if "true" in lower and "false" not in lower:
        return True
    if "false" in lower and "true" not in lower:
        return False
    return None


def classify_with_model(router, text: str, turn_type: str, turn_index: int = 0) -> list[Atom] | None:
    """Model-backed classification.  Returns atoms, or None on failure (caller falls back to the heuristic classifier)."""
    if not isinstance(text, str) or not text.strip():
        return []

    if turn_type == "confirmed_do":
        return [Atom(content=text.strip(), significance="formed", kind="action", source_turn=turn_index)]

    if turn_type not in ("say", "human"):
        return []

    prompt = _CLASSIFY_PROMPT.replace("__TURN_TEXT__", text.strip().replace('"', '\\"'))

    try:
        raw = router.complete(_CLASSIFY_SYSTEM, prompt, format=_CLASSIFY_SCHEMA)
    except Exception:
        return None

    is_creation = _parse_classification(raw)
    if is_creation is None:
        return None

    content = text.strip()
    if is_creation:
        kind = "draft" if turn_type == "say" else "intent"
        return [Atom(content=content, significance="firming", kind=kind, source_turn=turn_index)]
    else:
        kind = "process" if turn_type == "say" else "input"
        return [Atom(content=content, significance="flowing", kind=kind, source_turn=turn_index)]
