"""The agent loop.

The director (7's posture, any model via the router) proposes an action as structured intent.
The runtime routes that intent to the gate's propose(); only the HUMAN SEAT (an injected
confirm callable) can admit it. The model's output can reach propose() and nothing else —
there is no path from a model string to gate.confirm(). That is the structural wall that
makes safety model-agnostic.
"""
from __future__ import annotations

import json
import re

from .gate import Gate
from .router import Router


def _extract_intent(raw: str) -> dict | None:
    """Pull the first JSON object out of a model's reply. Prose around it is ignored."""
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _intent_instruction(offer: str) -> str:
    # Presence-first envelope: the model's job is to SPEAK to the person; the JSON is just how the
    # words are delivered. An action-proposal framing ("propose one of these, a human confirms")
    # made the model narrate the machine ("Waiting on human confirmation"); this makes it talk.
    # Actions remain available and every one still passes the gate unchanged.
    return (
        '\n\nDeliver what you say by putting it in this envelope and nothing else: '
        '{"action_type": "write_note", "params": {"text": "<exactly what you say to them>"}}. '
        f"If a real action beyond talking is genuinely needed, you may instead propose one of: {offer} "
        '(the same JSON shape). The person sees and allows anything before it happens.'
    )


def _salvage_say(raw: str) -> str | None:
    """Extract the say from a malformed JSON envelope.

    When the model emits an envelope with unescaped quotes or broken JSON
    (e.g. a drafted ad containing literal quotes), json.loads fails but the
    say content is still in there. Salvage it rather than dumping the raw
    JSON to the person.
    """
    m = re.search(r'"say"\s*:\s*"(.*?)"\s*,\s*"do"\s*:', raw, re.DOTALL)
    if m:
        return m.group(1).strip() or None
    m = re.search(r'"say"\s*:\s*"(.*)', raw, re.DOTALL)
    if m:
        text = m.group(1)
        for end in ['"do"', '", "do"', '"}']:
            idx = text.rfind(end)
            if idx > 0:
                text = text[:idx].rstrip().rstrip('"').rstrip(',').strip()
                return text or None
    return None


def _spoken(raw: str):
    """7's words when the reply wasn't our JSON envelope — talking is safe, so let them through."""
    t = (raw or "").strip()
    return t or None


def _is_envelope(text: str) -> bool:
    """Is this text nothing but our own JSON envelope? Then it is machine shape, not speech, and
    must never be shown to a person as 7's words."""
    t = (text or "").strip()
    if not (t.startswith("{") and t.endswith("}")):
        return False
    try:
        return isinstance(json.loads(t), dict)
    except ValueError:
        return False


_ENVELOPE_SCHEMA = {
    "type": "object",
    "properties": {
        "say": {"type": "string"},
        "creation": {"type": "string"},
        "do": {},
    },
    "required": ["say", "creation", "do"],
}

# -- Inflation scanners (post-model guard) ------------------------------------
# Detect when a model narrates a non-text asset as if it exists WITHOUT
# producing the actual artifact. Scene narration with real SVG is creation;
# scene narration without SVG is inflation.

_ASSET_CLAIM_RE = re.compile(
    r"\bi(?:'ve| have|)\s+(?:attached|uploaded|sent|emailed|transmitted)\b"
    r"|\b(?:file|attachment|download)\s+(?:is |has been )?(?:ready|attached|sent)\b"
    r"|\bhere(?:'s| is)\s+(?:the|your)\s+(?:file|attachment|download)\b",
    re.I,
)

_PHOTO_NARRATION_RE = re.compile(
    r"\bhere(?:'s| is)\s+(?:the|your|an?)\s+(?:photo|photograph|picture)\b"
    r"|\bi(?:'ve| have|)\s+(?:created|generated|made|produced)\s+(?:the|your|an?)\s+"
    r"(?:photo|photograph|picture)\b",
    re.I,
)

_SCENE_NARRATION_RE = re.compile(
    r"\bwarm (?:and |)(?:cozy|inviting|welcoming)\s+(?:photo|image|scene|atmosphere)\b"
    r"|\bthe sound of\b.*\bfills the air\b",
    re.I,
)


def is_inflation(text: str, has_svg: bool = False) -> bool:
    """Does this text inflate — narrate a non-text asset as if it exists?

    Scene narration accompanied by real SVG is NOT inflation — the model
    produced the artifact. Narration WITHOUT SVG is inflation (describing
    something that doesn't exist). Asset claims (attached/sent) and photo
    narration are always inflation regardless of SVG presence.
    """
    if not isinstance(text, str):
        return False
    if _ASSET_CLAIM_RE.search(text) or _PHOTO_NARRATION_RE.search(text):
        return True
    if not has_svg and _SCENE_NARRATION_RE.search(text):
        return True
    return False


# -- SVG structural repair + renderability validation -------------------------
# Explore7 mapped llama3.2's reliable SVG band: text/wordmarks + crude shapes.
# Three silent-killers: missing xmlns (renders blank), unconstrained font-size
# (overflows), white-on-white fills (invisible).
#
# D7 subtract/mutate-repair: block only TRUE noise (no content / unfixable).
# A FIXABLE misfit gets REPAIRED into fit, not discarded. The flow is:
#   generate → repair-fixable → subtract-only-irreparable → render-check
# Cheap (structure is ~free per the benchmark).

_SVG_RE = re.compile(r'<svg\b[^>]*>.*?</svg>', re.DOTALL | re.I)
_XMLNS_RE = re.compile(r'\bxmlns\s*=', re.I)
_VIEWBOX_RE = re.compile(r'\bviewBox\s*=\s*["\']([^"\']+)["\']', re.I)
_FILL_ATTR_RE = re.compile(r'(\bfill\s*=\s*["\'])([^"\']*)', re.I)
_FILL_RE = re.compile(
    r'\bfill\s*=\s*["\']([^"\']+)["\']'
    r'|\bfill\s*:\s*([^;}"\']+)',
    re.I,
)

_INVISIBLE_FILLS = frozenset({
    'none', 'transparent', 'white', '#fff', '#ffffff',
})

_DEFAULT_PALETTE = '#1a3a5c'


def repair_svg(svg: str) -> str:
    """Repair fixable SVG defects — mutate toward fit, don't subtract.

    1. Inject xmlns if missing (renders blank without it)
    2. Inject viewBox if missing (uncontained layout)
    3. Recolor invisible fills to palette default (white-on-white → visible)
    4. Clamp oversized font-size (overflow → illegible)
    """
    if not _XMLNS_RE.search(svg.split('>')[0]):
        svg = svg.replace('<svg', '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    if not _VIEWBOX_RE.search(svg):
        svg = svg.replace('<svg', '<svg viewBox="0 0 200 200"', 1)

    def _recolor(m):
        prefix = m.group(1)
        fill = m.group(2).strip().lower()
        if fill in _INVISIBLE_FILLS:
            return prefix + _DEFAULT_PALETTE
        return m.group(0)
    svg = _FILL_ATTR_RE.sub(_recolor, svg)

    def _clamp_font(m):
        size = float(m.group(1))
        if size > 72:
            return f'font-size="{48}"'
        if size < 8:
            return f'font-size="{16}"'
        return m.group(0)
    svg = re.sub(r'font-size="(\d+(?:\.\d+)?)"', _clamp_font, svg)

    return svg


def _has_visible_fill(svg: str) -> bool:
    """Does the SVG have at least one non-white, non-transparent fill?"""
    for m in _FILL_RE.finditer(svg):
        fill = (m.group(1) or m.group(2) or '').strip().lower()
        if fill not in _INVISIBLE_FILLS:
            return True
    if re.search(r'<text\b', svg, re.I):
        return True
    return False


def validate_svg_renderable(svg: str) -> bool:
    """Gate AFTER repair: does this SVG actually render?

    parseable XML != renderable SVG. A still-blank SVG after repair (no
    xmlns despite injection attempt, no visible content) is irreparable —
    subtract it. Everything fixable was already repaired.
    """
    if not _XMLNS_RE.search(svg.split('>')[0]):
        return False
    if not _has_visible_fill(svg):
        return False
    return True


def extract_svg(text: str) -> str | None:
    """Extract an SVG element from text, if present."""
    m = _SVG_RE.search(text)
    return m.group(0) if m else None


# -- Capability request detection ---------------------------------------------

_SVG_PRODUCIBLE_RE = re.compile(
    r'\b(?:make|create|generate|design|produce|draw|render|build|give me|show me)\b'
    r'.*\b(?:image|logo|icon|badge|banner|graphic|illustration|svg)\b',
    re.I,
)

_PHOTO_REQUEST_RE = re.compile(
    r'\b(?:make|create|generate|produce|give me|show me|take)\b'
    r'.*\b(?:photo|photograph|picture|raster)\b',
    re.I,
)

_DECLINE_ASSET_RE = re.compile(
    r'\b(?:make|create|generate|produce|build|give me|show me|record|compose|write me)\b'
    r'.*\b(?:video|animation|clip|audio|voiceover|voice[\s-]*over|sound|music|song|'
    r'pdf|file|attachment|code|script|program|html|css|javascript|app|website)\b'
    r'|'
    r'\b(?:attach|send|export|save|download)\b.*\b(?:pdf|file|attachment|email)\b',
    re.I,
)

_DIRECT_CREATION_RE = re.compile(
    r'\b(?:give me|write|draft|come up with|suggest|create|make)\b'
    r'.*\b(?:a |the |some )?(?:tagline|headline|name|slogan|title|motto|subject line|catchphrase)\b',
    re.I,
)


def _say_do_instruction(offer: str) -> str:
    # The said/done/creation seam: 7 SAYS freely, CREATES onto the canvas, and proposes DOs through
    # the gate. The creation field is the extraction point — grammar-constrained decoding forces the
    # model to separate creation from process at the token level. A weak model's chattiness doesn't
    # matter because the structure holds the shape.
    return (
        '\n\nRespond as JSON: {"say": "...", "creation": "", "do": null}\n\n'
        '"say" — your words to the person.\n'
        '"creation" — if you produce creation content this turn (a headline, tagline, draft, '
        'plan, name, copy, or SVG graphic), put ONLY the content itself here. No commentary, '
        'no framing — just the thing. Empty "" when not producing creation content.\n'
        '"do" — null unless an action that reaches the world is needed. '
        f'Available: {offer}. Nothing in "do" happens until the person allows it.\n\n'
        '{"say": "What kind of ad?", "creation": "", "do": null}\n'
        '{"say": "Here is a tagline.", "creation": "Brewing Connection", "do": null}\n'
        '{"say": "Draft ad.", "creation": "Drive Safe, Save More — affordable coverage for your family.", "do": null}\n'
    )


def _compose(task: str, history=None, canvas=None) -> str:
    """Frame the turn so the model SPEAKS to the person as a co-creator.

    Three anchors that prevent role drift (especially with weak local models):
    1. WHO you are — 7, here with a person
    2. WHAT you are doing together — building something (the creation)
    3. HOW you speak — present, direct, as yourself (not as a customer, not narrating)

    History threads in for memory; canvas threads in so 7 knows what's formed so far.
    Neither changes the router interface (it stays a stable edge).
    """
    convo = ""
    if history:
        lines = [f"{'You' if t.get('who') == 'human' else '7'}: {t.get('text', '')}" for t in history]
        convo = "Earlier in this conversation:\n" + "\n".join(lines) + "\n\n"
    canvas_context = ""
    if canvas:
        items = [a["content"] for a in canvas if a.get("significance") in ("formed", "firming")]
        if items:
            canvas_context = "What's been created so far:\n" + "\n".join(f"- {c}" for c in items) + "\n\n"
    capability_override = ""
    if _PHOTO_REQUEST_RE.search(task):
        capability_override = (
            "\n\nThey are asking for a photograph. You cannot generate photos or raster images. "
            "Say this honestly. Offer to make a vector draft (SVG) instead — a text-based logo "
            "or simple graphic using shapes and text. Or offer to draft text content."
        )
    elif _SVG_PRODUCIBLE_RE.search(task):
        capability_override = (
            "\n\nThey want a visual. You CAN make vector graphics (SVG). "
            "Produce a real SVG in the creation field. You are reliable at: "
            "text wordmarks (use legible font-size, e.g. 24-48), simple shapes with explicit fills. "
            "You are NOT reliable at: recognizable pictorial icons, detailed scenes, curved paths. "
            "STRUCTURAL RULES for valid SVG:\n"
            "- Start with <svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 200 100\">\n"
            "- Always include xmlns — without it the SVG renders blank\n"
            "- Use explicit fill colors (not white) — fill=\"#2d5016\" not fill=\"white\"\n"
            "- Keep font-size between 16 and 48\n"
            "- Label it honestly: 'vector draft' or 'wordmark', not 'logo design'\n"
            "Do NOT describe the image as a photo. Do NOT narrate what a photograph would look like. "
            "Produce the actual SVG."
        )
    elif _DECLINE_ASSET_RE.search(task):
        capability_override = (
            "\n\nIMPORTANT: They are asking for something you CANNOT make. "
            "You cannot generate video, audio, code, files, or attachments. "
            "You cannot send emails or attach files. "
            "Say this directly: tell them what you cannot do, then offer what you CAN do "
            "(draft the text, make a vector graphic, describe what it should look like). "
            "Put your honest response in 'say' and leave 'creation' empty."
        )
    creation_nudge = ""
    if not capability_override and _DIRECT_CREATION_RE.search(task):
        creation_nudge = (
            "\n\nThey are asking you to produce something specific. "
            "Produce it directly — put the actual content in 'creation'. "
            "Don't ask clarifying questions unless truly necessary."
        )
    return (
        "You are 7, here with a person. You are helping them create something together.\n"
        f"{convo}"
        f"{canvas_context}"
        f"They just said:\n{task}\n\n"
        "Help them build what they're asking for. If they want you to draft something (an ad, a plan, "
        "a name, a design, a logo), produce the draft directly — show them the thing, don't describe "
        "the process. "
        "You draft TEXT (copy, headlines, taglines, plans, emails, names, descriptions) and "
        "VECTOR GRAPHICS (simple SVG logos, wordmarks, text-based designs). "
        "You cannot generate photos, video, audio, or code — if asked, say so honestly and offer "
        "what you can do instead. Never pretend you made something you cannot make. "
        "Speak as yourself, present and direct. Never roleplay as someone else (not a customer, "
        "not a character). You are 7, their co-creator."
        f"{capability_override}"
        f"{creation_nudge}"
    )


class Runtime:
    """The agent loop — router + gate + posture + the human seat."""

    def __init__(self, router: Router, gate: Gate, posture: str, confirm):
        self.router = router      # the swappable model
        self.gate = gate          # the constitution
        self.posture = posture    # 7's instructions (text; model-independent)
        self.confirm = confirm    # the human seat: (projection:str) -> bool

    def step(self, task: str) -> dict:
        raw = self.router.complete(self.posture, task + _intent_instruction(self.gate.offer()))
        intent = _extract_intent(raw)
        if intent is None:
            return {"admitted": False, "reason": "no parseable intent", "backend": self.router.backend}

        # The model's intent reaches ONLY propose(). It can never fire.
        proposal = self.gate.propose(intent.get("action_type", ""), intent.get("params", {}) or {})
        if not proposal["ok"]:
            return {"admitted": False, "reason": proposal["kind"], "backend": self.router.backend}

        # THE HUMAN SEAT. The model's output arrives here as a projection to read, never as a fire.
        if not self.confirm(proposal["projection"]):
            return {"admitted": False, "reason": "human declined", "projection": proposal["projection"]}

        result = self.gate.confirm(proposal["move_id"])
        return {"admitted": True, "result": result, "backend": self.router.backend}

    def respond(self, task: str, history=None, canvas=None) -> dict:
        """A conversational turn, walked along the said/done/creation seam:

        - SAY flows freely (words are not a registered action, so no gate)
        - CREATION extracts onto the canvas (grammar-constrained — the model separates creation from
          process at the token level, so the weak model's chattiness doesn't matter)
        - DO is gated (confirm-is-the-only-admit, unchanged)

        Returns {say, proposal, intent, creation, backend}: `creation` is the extracted creation
        content (or None) — clean, no process framing. A surface puts this directly on the canvas.

        SAFETY NOTE: the seam is safe because text is not an action, NOT because words are harmless.
        (1) NEVER let a `say` or `creation` carry an effect. (2) Gate the VOICE by model trust.

        Leniently accepts three shapes: a {"say","creation","do"} object (the conversational model
        with extraction), a bare {"action_type","params"} (the deterministic stub), or plain prose
        (spoken words that missed the JSON envelope)."""
        raw = self.router.complete(
            self.posture,
            _compose(task, history, canvas) + _say_do_instruction(self.gate.offer()),
            format=_ENVELOPE_SCHEMA,
        )
        obj = _extract_intent(raw)
        if obj is None:
            salvaged = _salvage_say(raw)
            say = salvaged if salvaged else _spoken(raw)
            if say and _is_envelope(say):
                say = None
            return {"say": say, "proposal": None, "intent": None, "creation": None, "backend": self.router.backend}
        if "say" in obj or "do" in obj or "creation" in obj:
            say = obj.get("say")
            say = say.strip() if isinstance(say, str) else None
            do = obj.get("do")
            creation = obj.get("creation")
            if isinstance(creation, str):
                creation = creation.strip() if creation.strip() else ""
            else:
                creation = None
            proposal = None
            intent = None
            if isinstance(do, dict) and do.get("action_type"):
                intent = {"action_type": do.get("action_type", ""), "params": do.get("params", {}) or {}}
                proposal = self.gate.propose(intent["action_type"], intent["params"])
            if not say and proposal is None and not creation:
                spoken = _spoken(raw)
                say = None if spoken and _is_envelope(spoken) else spoken
            return {"say": say or None, "proposal": proposal, "intent": intent, "creation": creation, "backend": self.router.backend}
        if obj.get("action_type"):
            intent = {"action_type": obj.get("action_type", ""), "params": obj.get("params", {}) or {}}
            proposal = self.gate.propose(intent["action_type"], intent["params"])
            return {"say": None, "proposal": proposal, "intent": intent, "creation": None, "backend": self.router.backend}
        return {"say": _spoken(raw), "proposal": None, "intent": None, "creation": None, "backend": self.router.backend}

    def propose(self, task: str, history=None) -> dict:
        """Model proposes; the gate holds it PENDING (no confirm). For triggered/async runs where
        the human seat is elsewhere (e.g., a phone that confirms later). Returns the proposal
        (ok, move_id, projection) or a refusal — and never fires. Confirm is done separately by
        calling gate.confirm(move_id) when the human (on the phone) approves.

        `history` (prior turns) lets a triggered run continue a THREAD: 7 answers the current
        message in the light of what was already said. It changes only what the model reads;
        the gate still admits nothing without a human confirm."""
        raw = self.router.complete(self.posture, _compose(task, history) + _intent_instruction(self.gate.offer()))
        intent = _extract_intent(raw)
        if intent is None:
            return {"ok": False, "kind": "no_parseable_intent", "backend": self.router.backend}
        proposal = self.gate.propose(intent.get("action_type", ""), intent.get("params", {}) or {})
        return {**proposal, "backend": self.router.backend}
