"""The run manager for the phone-trigger.

A triggered run is asynchronous: the model proposes, the gate holds it PENDING, and the
human seat is elsewhere — a phone that confirms later. This manager owns the in-memory
set of runs, thread-safe, so a trigger endpoint and a confirm endpoint can touch the
same run from different threads. It still never fires a model's output: confirm() here
only calls gate.confirm(move_id), which is the one and only admit.
"""
from __future__ import annotations

import html
import json
import os
import threading
import uuid

import re

from datetime import datetime, timezone

from .cathedral import CathedralBridge, normalize_canvas
from ..canvas import Creation
from ..classifier import classify_turn, _is_produced_content
from ..correction import (
    CorrectionType, detect_correction,
    execute_promote, execute_remove,
    resolve_promote_target, resolve_remove_target,
)
from ..gate import Gate
from ..harness import load_posture
from ..m3_classifier import classify_with_model
from ..router import ClaudeModel, OllamaModel, Router, StubModel
from ..runtime import Runtime, is_inflation, extract_svg, repair_svg, validate_svg_renderable
from ..distinctions.checks import (
    check_renders, check_claimed_actual, check_satisfies_intent,
    check_coherent_difference, check_subtract_repair,
)


_CREATION_CONTEXT_RE = re.compile(
    r'\b(?:tagline|headline|name|title|slogan|draft|copy|body|ad|motto|plan|idea|email|subject)\b',
    re.IGNORECASE,
)


def _extract_quoted_creation(text: str) -> str | None:
    """Extract a quoted creation span from a chatty say.

    The weak model wraps creation in chatter: "A tagline that feels right is
    'Brewing Connection'. It's short and suggests warmth." Extract the quoted
    span. Only fires when the say has creation-context words (tagline, headline,
    name, etc.) to avoid false-extracting conversational quotes.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    if not _CREATION_CONTEXT_RE.search(text):
        return None
    matches = re.findall(r"'([^']{3,})'", text)
    if not matches:
        matches = re.findall(r'"([^"]{3,})"', text)
    if matches:
        return max(matches, key=len)
    return None


def _audit_age(ts: object) -> float | None:
    """Seconds since a daemon-audit timestamp (strftime ``%Y-%m-%dT%H:%M:%S%z``), or None if it is
    absent or unparseable. Best-effort: the surface still renders the outcome + raw ts without it."""
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        dt = datetime.strptime(ts.strip(), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())


def _neutralize_markup(text: str) -> str:
    """Defense-in-depth for the STORE side: neutralize HTML markup in model-authored text
    BEFORE it enters the durable transcript that GET /thread serves.

    The browser console already re-escapes model text at render time, so no payload executes
    there. But that leaves the whole XSS defense resting on one client render path — any future
    or alternate consumer of /thread that dumps the text into innerHTML would turn stored markup
    into live stored-XSS. Escaping ``& < >`` to entities makes a tag impossible to form, while
    round-tripping cleanly through the console's own decode() so nothing changes for the human.
    """
    if not isinstance(text, str):
        return text
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# The ONE definition of "nothing to read", shared by every layer.
#
# The first attempt at this fix defined "empty" three times — Python's str.strip(), JavaScript's
# String.trim(), and a bare `if say:` — and the three disagreed: str.strip() drops U+00A0 but not
# U+200B; trim() drops U+00A0 and U+FEFF but not U+200B. One zero-width character walked through
# every guard and put an unreadable offer in front of a person. So emptiness is defined ONCE, here,
# and the console mirrors this exact set (see `INVISIBLE` in server.py's console — keep them equal).
#
# Beyond whitespace, these are the characters that occupy no visual space: zero-width spaces and
# joiners, the word joiner, the soft hyphen, the BOM, and the bidi/format marks.
INVISIBLE = (
    "\u200b\u200c\u200d\u2060\u2061\u2062\u2063\u2064"   # zero-width + invisible operators
    "\ufeff\u00ad\u180e\u00a0"                               # BOM, soft hyphen, Mongolian vowel sep, nbsp
    "\u200e\u200f\u061c"                                      # bidi marks
    "\u202a\u202b\u202c\u202d\u202e"                        # bidi embedding/override
    "\u2066\u2067\u2068\u2069"                               # bidi isolates
    "\u206a\u206b\u206c\u206d\u206e\u206f"                 # deprecated formatting (match JS side)
    "\u2800"                                                   # Braille Pattern Blank (renders as space)
    "\u3164\uffa0"                                             # Hangul Filler, Halfwidth Hangul Filler
)


_EARNED_WITHHOLDING = (
    "I can help with text and simple graphics — "
    "what would you like to create?"
)


def _plate_meta(p: object) -> dict | None:
    """The plate marker on a human turn, or None. Only {op: act|react|undo, id, act_ts}, all
    strings. "react" is a typed re-choose ("#00012 keep" on a settled item): act_ts is the
    new act."""
    if not isinstance(p, dict):
        return None
    op, iid, ts = p.get("op"), p.get("id"), p.get("act_ts")
    if op not in ("act", "react", "undo") or not isinstance(iid, str) or not isinstance(ts, str):
        return None
    return {"op": op, "id": iid[:200], "act_ts": ts[:64]}


def is_blank(text: object) -> bool:
    """Is there nothing here for a person to read? The single source of truth for "empty".

    Total by construction: anything that is not a string has no words in it, so a non-string
    (None, an int, a dict — all ordinary output from a weak model) is blank rather than an error.
    An earlier version called .strip() on whatever it was handed and raised AttributeError, which
    killed the request thread and showed the person a raw `TypeError: Failed to fetch`.

    Entities are decoded first, to a fixed point, exactly as the console's decode() does. Judging
    the undecoded string let `&nbsp;` through here while the console rendered it as blank — the
    store-side hardening and the render-side decoder composing into a text-in / blank-out
    transform. Both ends must answer "is there anything here?" about the SAME string.
    """
    if not isinstance(text, str):
        return True
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return not text.strip(INVISIBLE + " \t\n\r\v\f\x00").strip()


def _has_content(action_type: str, params: dict, projection: str = "") -> bool:
    """Is there anything IN this move for the person to read?

    A gate is only a gate if the human can see what they are allowing. A write_note whose text is
    blank asks them to allow something unreadable — an empty offer — so it is not a real move.

    For every other action type the PROJECTION is what the person reads, so that is what must be
    non-blank. This is checked, not assumed: the previous version returned True for anything that
    was not a write_note, which was true only because today's registry happens to hold nothing whose
    describe() can come back empty. The first action added that could would have reopened the hole
    silently.
    """
    if action_type == "write_note":
        return not is_blank((params or {}).get("text", ""))
    return not is_blank(projection)


def _admit_to_canvas(content: str, say_text: str = "") -> tuple[bool, str]:
    """The selector spine — every atom passes through here before canvas entry.

    ALL paths to canvas call this. No append without it. Returns (admitted,
    content_to_use). When admitted=False the atom must not enter the canvas.
    When admitted=True content_to_use is the (possibly repaired) content.

    Checks run:
      claimed/actual    — on say_text (the claim context)
      satisfies-intent  — on the content itself
      subtract/repair   — on SVG content (repair fixable, subtract irreparable)
      renders           — on SVG after repair
      coherent-diff     — on SVG after repair
    """
    if is_blank(content):
        return False, ""

    if say_text and not check_claimed_actual(say_text).passed:
        return False, ""

    if not check_satisfies_intent(content).passed:
        return False, ""

    svg = extract_svg(content)
    if svg:
        sr = check_subtract_repair(svg)
        if not sr.passed:
            return False, ""
        checked_svg = sr.repaired_value if sr.repaired else svg
        if not check_renders(checked_svg).passed:
            return False, ""
        if not check_coherent_difference(checked_svg).passed:
            return False, ""
        return True, checked_svg

    return True, content


def _earns_canvas(atom, turn_type: str) -> bool:
    """A 2nd instance of content must earn its place on the canvas.

    The earning protocol — what qualifies content for a canvas presence beyond the dock:

    1. confirmed_do → always earns (consent gate is the earning)
    2. say classified as "draft" (firming) → earns (structural detection is the earning:
       the classifier proved this is produced content, not process chatter, so the canvas
       atom carries a genuine distinction from the dock bubble)
    3. say classified as "process" (flowing) → does NOT earn (verbatim copy, no transformation)

    Evolution path: when the model-backed classifier can EXTRACT and CONDENSE — turning
    a full draft into named atoms — that transformation becomes stronger earning. For now,
    detection-as-earning is the honest floor: it identifies what IS the creation without
    claiming to understand it.
    """
    if turn_type == "confirmed_do":
        return True
    if hasattr(atom, "kind") and atom.kind in ("draft", "promoted") and hasattr(atom, "significance") and atom.significance == "firming":
        return True
    return False


def _demo_script(system: str, prompt: str) -> str:
    """Deterministic three-beat demo: welcome → text creation → SVG creation.

    Zero compute, watchable, repeatable. Each beat shows a different capability:
      Beat 1 (general): pure say, no creation
      Beat 2 (tagline/copy): text creation firms on canvas
      Beat 3 (logo/visual): SVG creation firms on canvas, rendered inline
    """
    m = re.search(r'They just said:\n(.+?)(?:\n\n)', prompt, re.DOTALL)
    task = m.group(1).strip().lower() if m else prompt.lower()

    if any(w in task for w in ('logo', 'graphic', 'visual', 'icon', 'image', 'svg', 'design')):
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 380 80">'
            '<rect width="380" height="80" rx="12" fill="#2d5016"/>'
            '<text x="190" y="52" fill="#e6edf3" font-size="28" '
            'font-family="sans-serif" text-anchor="middle" font-weight="bold">'
            'BREWING CONNECTION</text></svg>'
        )
        return json.dumps({
            "say": "Here is a vector wordmark for Brewing Connection. "
                   "Simple shapes, honest label — a starting point you can shape.",
            "creation": svg,
            "do": None,
        })

    if any(w in task for w in ('tagline', 'headline', 'name', 'slogan', 'copy',
                                'write', 'draft', 'ad', 'motto')):
        return json.dumps({
            "say": "Here is a tagline for your tea shop.",
            "creation": "Brewing Connection — where every cup tells a story",
            "do": None,
        })

    return json.dumps({
        "say": "Let’s build this together. What would you like to start with "
               "— a name, a tagline, a draft of the ad copy, or a simple logo?",
        "creation": "",
        "do": None,
    })


class RunManager:
    """Owns triggered runs. Start proposes (never fires); confirm/decline is the phone's seat."""

    def __init__(self, state_path: str | None = None):
        self._lock = threading.Lock()
        self._runs: dict[str, dict] = {}
        # A conversation threads runs together so 7 remembers. Each holds the human's chosen
        # model and the transcript of turns that were actually SAID (human messages + the 7
        # replies the human confirmed). Nothing 7 proposes enters the transcript until admitted.
        self._threads: dict[str, dict] = {}
        # Persistence is OPT-IN. No path (arg or env) -> in-memory only, disk is never touched;
        # this keeps the default constructor exactly as it was. A path means transcripts survive
        # a restart: only _threads is durable — _runs and Gate objects are ephemeral by design.
        self._state_path = state_path or os.environ.get("AGNOSTIC_TRIGGER_STATE") or None
        # The Cathedral door — a file-queue bridge to a Claude Code session, built lazily so the
        # default constructor touches no disk until a cathedral run actually needs it. Tests inject
        # an isolated bridge by assigning self._bridge before the first start().
        self._bridge: CathedralBridge | None = None
        if self._state_path:
            self._load_threads()

    def _door(self) -> CathedralBridge:
        if self._bridge is None:
            self._bridge = CathedralBridge()
        return self._bridge

    def _load_threads(self) -> None:
        # Rehydrate _threads from disk. A missing or corrupt file must never raise — a fresh
        # server simply starts with an empty transcript rather than refusing to boot. "Corrupt"
        # is not only invalid JSON: a valid-JSON file with a wrong INNER shape (a thread that is a
        # string, or one missing/mis-typing "model"/"turns") would load clean here and then crash
        # every later request that touches that conversation. So each entry is validated and
        # sanitized on load — repaired where it can be, dropped where it cannot — and only sane
        # entries are admitted. Nothing that survives this pass can raise on later use.
        try:
            with open(self._state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        clean: dict[str, dict] = {}
        for cid, thread in data.items():
            sane = self._sanitize_thread(thread)
            if sane is not None:
                clean[str(cid)] = sane
        self._threads = clean

    @staticmethod
    def _sanitize_thread(thread: object) -> dict | None:
        """Coerce one on-disk conversation entry into the guaranteed shape, or reject it.

        Contract: a thread is a dict with a string ``model`` and a list ``turns`` of
        ``{"who": str, "text": str}``. A non-dict entry, or one without a usable string model,
        is unsalvageable and dropped (returns None). A bad/missing ``turns`` is repaired to an
        empty list, and individual malformed turns are dropped, so what is admitted can never
        raise on later access.
        """
        if not isinstance(thread, dict):
            return None
        model = thread.get("model")
        if not isinstance(model, str):
            return None
        raw_turns = thread.get("turns")
        turns: list[dict] = []
        if isinstance(raw_turns, list):
            for t in raw_turns:
                if not isinstance(t, dict):
                    continue
                who = t.get("who")
                text = t.get("text")
                if isinstance(who, str) and isinstance(text, str):
                    turn = {"who": who, "text": text}
                    # Carry the standing-attendant identity through a restart. Without this, a
                    # reply the autonomous attendant authored would rehydrate as a present seat
                    # ("7") on reload — exactly the who-identity lie this edge exists not to tell.
                    answered_by = t.get("answered_by")
                    if isinstance(answered_by, str):
                        turn["answered_by"] = answered_by
                    outcome = t.get("answer_outcome")
                    if isinstance(outcome, str):
                        turn["answer_outcome"] = outcome
                    # Carry the ask-time role snapshot through a restart. RECORDED, not loaded —
                    # the field is a historical fact about what the asker declared, never a
                    # posture input for this seat.
                    turn_role = t.get("role")
                    if isinstance(turn_role, str):
                        turn["role"] = turn_role
                    # Carry the plate marker (this human turn was a plate act or undo) through
                    # a restart, so a reloaded console can still offer Undo on it.
                    pm = _plate_meta(t.get("plate"))
                    if pm is not None and who == "human":
                        turn["plate"] = pm
                    turns.append(turn)
        canvas_data = thread.get("canvas")
        canvas = []
        if isinstance(canvas_data, list):
            for item in canvas_data:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, str):
                    continue
                sig = item.get("significance")
                if sig not in ("formed", "firming", "flowing"):
                    continue
                canvas.append(item)
        # Preserve the thread's owner across restart. Threads born before this field existed
        # read with user_id=None (admin-owned by default), which cannot be re-claimed by a
        # user token later — that is the right shape; ownership is set at thread birth.
        owner = thread.get("user_id")
        out: dict = {"model": model, "turns": turns, "canvas": canvas}
        if isinstance(owner, str):
            out["user_id"] = owner
        else:
            out["user_id"] = None
        return out

    def _persist_threads(self) -> None:
        # Best-effort, atomic, and called UNDER the lock right after turns change. A disk problem
        # can NEVER break a live conversation, so every OSError is swallowed; the write goes to a
        # .tmp sibling then os.replace() swaps it in as one atomic step.
        if not self._state_path:
            return
        try:
            parent = os.path.dirname(self._state_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._threads, fh)
            os.replace(tmp, self._state_path)
        except OSError:
            pass

    def _model_factory(self, name: str):
        if name == "stub":
            def script(system: str, prompt: str) -> str:
                # Cooperative: propose a write_note summarizing the task. Deterministic for tests.
                summary = prompt.strip().splitlines()[0][:120] if prompt.strip() else "run"
                text = summary.replace('"', "'")
                return '{"action_type": "write_note", "params": {"text": "%s"}}' % text
            return StubModel("stub", script=script)
        if name == "demo":
            return StubModel("demo", script=_demo_script)
        if name == "ollama":
            return OllamaModel()
        if name == "claude":
            return ClaudeModel()
        raise ValueError(f"unknown model {name}")

    def start(self, task: str, model: str = "stub", conversation_id: str | None = None,
              user_id: str | None = None, role: str | None = None,
              plate: dict | None = None) -> str:
        # plate: set only by the server (/plate/act, /plate/undo, and a typed "#00012 revisit"
        # it parsed and recorded on /run), never copied from a request body.
        # It rides on the human turn ({"op": "act"|"undo", "id", "act_ts"}) so the console knows
        # a bubble is a plate act from the record, not from its text. Cathedral path only.
        # A thread may not straddle the door. The cathedral path builds NO model and the model
        # paths call NO bridge; a conversation that began on one side cannot be continued on the
        # other without a model being run where none should be (or a correction mutating a thread
        # across the seam). So before anything else — before correction detection, before any model
        # work — resolve the door side and FORK into a fresh conversation on mismatch.
        #
        # OWNERSHIP: a user-scoped request (user_id is a string) cannot continue a thread that
        # belongs to another user; it forks into a fresh conversation. An admin request
        # (user_id is None, env-token path) can continue any thread. The ownership stamp is set
        # on the thread at creation and NEVER mutated.
        with self._lock:
            thread = self._threads.get(conversation_id) if conversation_id else None
            if thread is not None and user_id is not None:
                owner = thread.get("user_id")
                if owner is not None and owner != user_id:
                    thread = None
                    conversation_id = None
            if thread is not None and (thread["model"] == "cathedral") != (model == "cathedral"):
                thread = None
                conversation_id = None
            cathedral = (thread["model"] if thread is not None else model) == "cathedral"
            if cathedral:
                if thread is None:
                    conversation_id = uuid.uuid4().hex
                    self._threads[conversation_id] = {
                        "model": "cathedral", "turns": [], "canvas": [],
                        "user_id": user_id,
                    }
        if cathedral:
            return self._start_cathedral(task, conversation_id, user_id=user_id, role=role,
                                         plate=plate)

        # Check for a canvas correction BEFORE the model call.
        correction = detect_correction(task)
        if correction and conversation_id:
            result = self._handle_correction(task, correction, conversation_id, model,
                                             user_id=user_id, role=role)
            if result is not None:
                return result

        # Resolve the thread and snapshot its prior turns UNDER the lock; then release it before
        # the model call (never hold the lock across the network). A thread keeps its original
        # model so a conversation doesn't switch backends mid-stream.
        with self._lock:
            thread = self._threads.get(conversation_id) if conversation_id else None
            if thread is None:
                conversation_id = uuid.uuid4().hex
                thread = {"model": model, "turns": [], "canvas": [], "user_id": user_id}
                self._threads[conversation_id] = thread
            model = thread["model"]
            history = list(thread["turns"])
            canvas_snapshot = list(thread.get("canvas") or [])

        gate = Gate()
        rt = Runtime(Router(self._model_factory(model)), gate, load_posture(), confirm=lambda proj: False)
        r = rt.respond(task, history=history, canvas=canvas_snapshot)
        say = r.get("say")
        proposal = r.get("proposal")
        intent = r.get("intent")
        creation = r.get("creation")

        # A proposal with nothing in it is not an offer. Never ask the person to allow a move they
        # cannot read — drop the hollow proposal (it stays un-confirmable in the gate, so nothing can
        # fire) and fall through to 7's words, or to the merciful "I did not catch that".
        if proposal and proposal.get("ok") and intent and not _has_content(
            intent.get("action_type", ""), intent.get("params") or {}, proposal.get("projection") or ""
        ):
            proposal = None
            intent = None

        if is_blank(say):
            say = None

        if proposal and proposal.get("ok"):
            state = "awaiting_confirm"
            error = None
        elif proposal and not proposal.get("ok"):
            state = "refused"
            error = proposal.get("kind")
        elif say:
            state = "said"
            error = None
        else:
            state = "no_intent"
            error = "no_parseable_intent"

        run = {
            "gate": gate,
            "proposal": proposal,
            "intent": intent,
            "say": say,
            "backend": r.get("backend"),
            "state": state,
            "result": None,
            "error": error,
            "conversation_id": conversation_id,
            "user_id": user_id,
        }
        run_id = uuid.uuid4().hex
        with self._lock:
            self._runs[run_id] = run
            turn_idx = len(thread["turns"])
            human_turn: dict = {"who": "human", "text": task}
            # Role is RECORDED on each ask (ask-time snapshot of what the user declared),
            # NEVER loaded as posture. The field exists so a future surface or auditor can
            # see what the asker declared without this layer folding it into any posture
            # decision.
            if role is not None:
                human_turn["role"] = role
            thread["turns"].append(human_turn)
            if say:
                safe_say = _neutralize_markup(say)

                # Dock canary: detect inflation BEFORE transcript entry.
                # An inflating say must not reach the person in the dock.
                from ..canvas import Atom
                svg_in_creation = extract_svg(creation or "")
                svg_in_say = extract_svg(say or "")
                has_svg = bool(svg_in_creation or svg_in_say)
                inflating = (
                    is_inflation(say, has_svg=has_svg)
                    or is_inflation(creation or "", has_svg=has_svg)
                    or not check_claimed_actual(say or "").passed
                )

                if not inflating:
                    dock_text = safe_say
                else:
                    dock_text = _EARNED_WITHHOLDING
                thread["turns"].append({"who": "7", "text": dock_text})
                say_turn_idx = len(thread["turns"]) - 1

                # Canvas extraction — all paths through _admit_to_canvas.
                if creation and not is_blank(creation) and not inflating:
                    safe_creation = _neutralize_markup(creation) if not extract_svg(creation) else creation
                    admitted, content = _admit_to_canvas(safe_creation, say_text=say)
                    if admitted:
                        atom = Atom(content=content, significance="firming",
                                    kind="draft", source_turn=say_turn_idx)
                        thread.setdefault("canvas", []).append(atom.to_dict())
                elif creation is not None and not inflating:
                    extracted = _extract_quoted_creation(say)
                    if extracted:
                        safe_ex = _neutralize_markup(extracted)
                        admitted, content = _admit_to_canvas(safe_ex, say_text=say)
                        if admitted:
                            atom = Atom(content=content, significance="firming",
                                        kind="draft", source_turn=say_turn_idx)
                            thread.setdefault("canvas", []).append(atom.to_dict())
                    elif _is_produced_content(safe_say):
                        for atom in classify_turn("say", safe_say, turn_index=say_turn_idx):
                            if _earns_canvas(atom, "say"):
                                admitted, content = _admit_to_canvas(atom.content, say_text=say)
                                if admitted:
                                    atom = Atom(content=content, significance=atom.significance,
                                                kind=atom.kind, source_turn=say_turn_idx)
                                    thread.setdefault("canvas", []).append(atom.to_dict())
                else:
                    for atom in classify_turn("say", safe_say, turn_index=say_turn_idx):
                        if _earns_canvas(atom, "say"):
                            admitted, content = _admit_to_canvas(atom.content, say_text=say)
                            if admitted:
                                atom = Atom(content=content, significance=atom.significance,
                                            kind=atom.kind, source_turn=say_turn_idx)
                                thread.setdefault("canvas", []).append(atom.to_dict())
                    if model != "stub" and not inflating:
                        threading.Thread(
                            target=self._classify_say_with_model,
                            args=(safe_say, conversation_id, say_turn_idx, model),
                            daemon=True,
                        ).start()
            self._persist_threads()
        return run_id

    def _start_cathedral(self, task: str, conversation_id: str,
                         user_id: str | None = None, role: str | None = None,
                         plate: dict | None = None) -> str:
        """Put one ask through the Cathedral door and park the run. NO model is constructed here.

        The whole point of this path is the wall: the surface is a door, not an engine. It appends
        one line to the bridge's ask.jsonl and parks the run in ``awaiting_cathedral`` until a
        Claude Code session writes an answer back. If the disk refuses the ask, the run fails
        LOUDLY (``said`` with a real message) rather than parking forever behind a spinner.
        """
        bridge = self._door()
        run_id = uuid.uuid4().hex
        delivered = bridge.ask(run_id, conversation_id, task)
        with self._lock:
            thread = self._threads.get(conversation_id)
            if thread is not None:
                human_turn: dict = {"who": "human", "text": task}
                if role is not None:
                    human_turn["role"] = role
                pm = _plate_meta(plate)
                if pm is not None:
                    human_turn["plate"] = pm
                thread["turns"].append(human_turn)
                self._persist_threads()
            if delivered:
                state, say, error = "awaiting_cathedral", None, None
            else:
                state = "said"
                say = ("I could not reach Cathedral just now — the door did not take the ask. "
                       "Nothing was parked; try again.")
                error = "undeliverable"
            self._runs[run_id] = {
                "gate": Gate(),
                "proposal": None,
                "intent": None,
                "say": say,
                "backend": "cathedral",
                "state": state,
                "result": None,
                "error": error,
                "conversation_id": conversation_id,
                "user_id": user_id,
            }
        return run_id

    def _admit_cathedral_reply(self, run_id: str, run: dict, reply: dict) -> None:
        """Admit one answer from the CC side into the run and the durable transcript.

        Called UNDER the lock. The reply text is neutralized before it enters the transcript that
        GET /thread serves (store-side XSS defense, same as the model paths). Canvas atoms pass
        ``normalize_canvas`` (drops SVG, oversize, and bloat) and are then neutralized too — a
        refused atom never reaches the transcript at all, an admitted one never reaches it as
        live markup.

        WHO ANSWERED is resolved HERE, from the daemon audit log (keyed by run_id), NEVER from the
        answer text — the daemon writes no signature into its reply (README signature contract #1).
        A run_id with an audit record was answered by the STANDING ATTENDANT; its absence means a
        present seat answered. The identity is stamped on the run (for the live poll) and on the
        durable turn (so it survives a restart), so the console can always tell the reader WHO is
        on the other side once "home" can be a machine.
        """
        text = reply.get("text")
        audit = self._door().audit_for(run_id)
        answered_by = audit.get("answered_by") if isinstance(audit, dict) else None
        answer_outcome = audit.get("outcome") if isinstance(audit, dict) else None
        run["answered_by"] = answered_by if isinstance(answered_by, str) else None
        run["answer_outcome"] = answer_outcome if isinstance(answer_outcome, str) else None
        if is_blank(text):
            run["say"] = "Cathedral sent nothing to read."
            run["state"] = "said"
            return
        safe = _neutralize_markup(text)
        run["say"] = safe
        run["state"] = "said"
        cid = run.get("conversation_id")
        th = self._threads.get(cid) if cid else None
        if th is None:
            return
        turn = {"who": "7", "text": safe}
        if run["answered_by"]:
            turn["answered_by"] = run["answered_by"]
            if run["answer_outcome"]:
                turn["answer_outcome"] = run["answer_outcome"]
        th["turns"].append(turn)
        turn_idx = len(th["turns"]) - 1
        for atom in normalize_canvas(reply.get("canvas")):
            atom = dict(atom)
            atom["content"] = _neutralize_markup(atom["content"])
            atom["source_turn"] = turn_idx
            th.setdefault("canvas", []).append(atom)
        self._persist_threads()

    def _classify_say_with_model(self, say: str, conversation_id: str, turn_idx: int, model_name: str):
        """Async model-backed classification.  Can only UPGRADE (flowing → firming).

        The weak model proposes; the structure selects.  A bare ``creation:true`` is NOT
        enough to override the heuristic classifier — the model is the mutation engine,
        not the authority.  An upgrade requires corroboration:

        - If the heuristic already firmed it: the model agrees, dedup catches it (no-op).
        - If the heuristic said flowing AND the content has structural evidence of produced
          content (labeled sections, substantial multi-line, bulleted plans): upgrade.
        - If the heuristic said flowing and NO structural evidence: suppress.  The model's
          word alone on a process question is not worth a canvas slot.

        On any failure, the heuristic answer stands.
        """
        try:
            model = self._model_factory(model_name)
            router = Router(model)
            atoms = classify_with_model(router, say, "say", turn_idx)
            if atoms is None:
                return
            heuristic_result = classify_turn("say", say, turn_index=turn_idx)
            heuristic_earns = any(_earns_canvas(a, "say") for a in heuristic_result)
            with self._lock:
                th = self._threads.get(conversation_id)
                if th is None:
                    return
                canvas = th.setdefault("canvas", [])
                existing = {a.get("content", "").strip() for a in canvas}
                added = False
                for atom in atoms:
                    if not _earns_canvas(atom, "say"):
                        continue
                    if atom.content.strip() in existing:
                        continue
                    if not heuristic_earns and not _is_produced_content(atom.content):
                        continue
                    admitted, content = _admit_to_canvas(atom.content, say_text=say)
                    if not admitted:
                        continue
                    from ..canvas import Atom as A
                    atom = A(content=content, significance=atom.significance,
                             kind=atom.kind, source_turn=atom.source_turn)
                    canvas.append(atom.to_dict())
                    added = True
                if added:
                    self._persist_threads()
        except Exception:
            pass

    def _handle_correction(self, task: str, correction: CorrectionType,
                           conversation_id: str, model: str,
                           user_id: str | None = None,
                           role: str | None = None) -> str | None:
        """Execute a canvas correction.  No model call — deterministic response."""
        with self._lock:
            thread = self._threads.get(conversation_id)
            if thread is None:
                return None
            # Ownership gate — a user-scoped correction cannot land on another user's thread.
            if user_id is not None:
                owner = thread.get("user_id")
                if owner is not None and owner != user_id:
                    return None
            model = thread["model"]
            canvas = thread.setdefault("canvas", [])
            turns = thread["turns"]

            if correction == CorrectionType.PROMOTE:
                target = resolve_promote_target(turns, canvas)
                if target:
                    execute_promote(target, canvas, turn_index=len(turns))
                    say = "Done."
                else:
                    say = "I don't see anything to add."
            elif correction == CorrectionType.REMOVE:
                target = resolve_remove_target(canvas)
                if target:
                    execute_remove(target.get("atom_id", ""), canvas)
                    say = "Removed."
                else:
                    say = "Nothing to remove."
            else:
                return None

            human_turn: dict = {"who": "human", "text": task}
            if role is not None:
                human_turn["role"] = role
            turns.append(human_turn)
            turns.append({"who": "7", "text": say})
            self._persist_threads()

        run_id = uuid.uuid4().hex
        run = {
            "gate": Gate(),
            "proposal": None,
            "intent": None,
            "say": say,
            "backend": model,
            "state": "said",
            "result": None,
            "error": None,
            "conversation_id": conversation_id,
            "user_id": user_id,
        }
        with self._lock:
            self._runs[run_id] = run
        return run_id

    @staticmethod
    def _ownership_ok(record_owner: str | None, request_user_id: str | None) -> bool:
        """One ownership rule, applied at every user-scoped read/write.

        - request_user_id is None  → admin / env-token caller; bypass (back-compat).
        - record_owner is None     → legacy or admin-created record; only admin may touch it.
        - both set                 → must match exactly.

        Shield-steward (ownership gate grammar): the Bearer token has already been resolved
        upstream (that is the re-ask-the-empty). The comparison here is what makes it an
        admit-atom instead of a bare auth check.
        """
        if request_user_id is None:
            return True
        if record_owner is None:
            return False
        return record_owner == request_user_id

    def status(self, run_id: str, user_id: str | None = None) -> dict:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return {"error": "unknown_run"}
            if not self._ownership_ok(run.get("user_id"), user_id):
                # Deliberate shape: same error code as unknown_run so a probe cannot learn
                # whether a run exists under another owner. The 404 surface is uniform.
                return {"error": "unknown_run"}
            if run.get("backend") == "cathedral":
                return self._cathedral_status(run_id, run)
            pending = None
            if run["state"] == "awaiting_confirm":
                proposal = run["proposal"]
                intent = run.get("intent") or {}
                # Carry the TYPED move, not only its prose projection. A surface that has to scrape
                # the projection with a regex to find out what it is asking the person to allow will
                # get it wrong — that is how an offer arrived with an empty body. Give it the data.
                pending = {
                    "move_id": proposal.get("move_id"),
                    "projection": proposal.get("projection"),
                    "action_type": intent.get("action_type"),
                    "params": intent.get("params") or {},
                }
            cid = run.get("conversation_id")
            th = self._threads.get(cid) if cid else None
            canvas = list(th.get("canvas") or []) if th else []
            return {
                "state": run["state"],
                "backend": run["backend"],
                "pending": pending,
                "say": run.get("say"),
                "result": run["result"],
                "error": run["error"],
                "conversation_id": cid,
                "canvas": canvas,
            }

    def _cathedral_status(self, run_id: str, run: dict) -> dict:
        """Status for a run parked at the Cathedral door. Called UNDER the lock.

        While parked, poll the bridge for the CC session's answer; the moment one is on disk, admit
        it (transition to ``said``, thread the reply, place canvas atoms). The door's own state is
        reported honestly so the surface can SAY what is true instead of pulsing a spinner:
        ``armed`` means a session is listening, ``waiting`` is how many asks are outstanding, and
        ``stranded`` means this ask was marked taken without ever being answered — the one lie this
        whole seam exists not to tell.
        """
        bridge = self._door()
        if run["state"] == "awaiting_cathedral":
            reply = bridge.reply_for(run_id)
            if reply is not None:
                self._admit_cathedral_reply(run_id, run, reply)
        cid = run.get("conversation_id")
        th = self._threads.get(cid) if cid else None
        canvas = list(th.get("canvas") or []) if th else []
        return {
            "state": run["state"],
            "backend": run["backend"],
            "pending": None,
            "say": run.get("say"),
            "result": run["result"],
            "error": run["error"],
            "conversation_id": cid,
            "canvas": canvas,
            # WHO answered this run (None until an answer lands; None thereafter means a present
            # seat, a non-None string means the standing attendant). The console renders the
            # sender from THIS, never from the answer text.
            "answered_by": run.get("answered_by"),
            "answer_outcome": run.get("answer_outcome"),
            "door": {
                "armed": bridge.armed(),
                "waiting": bridge.waiting(),
                "stranded": bridge.stranded(run_id),
                # Positive liveness + health (who-identity edge): the raw heartbeat age behind the
                # armed() boolean, and the attendant's last real answer-path outcome, so a
                # subtly-broken-but-beating daemon shows through instead of masquerading as healthy.
                "heartbeat_age": bridge.heartbeat_age(),
                "attendant_last": self._shape_attendant_last(bridge.attendant_last()),
                # Custody-status signal (010 three-state render). None if the daemon isn't writing
                # the file (older build, or very first poll not landed yet) — the dock falls back
                # to the armed/empty collapse. When present, state ∈ {empty, working}.
                "custody_status": self._shape_custody_status(bridge.custody_status()),
            },
        }

    @staticmethod
    def _shape_custody_status(d: dict | None) -> dict | None:
        """Trim the daemon's door.custody_status.json to what the dock renders. We DO expose
        the steward_role (dock-header label) and the build_id (useful on hover/tap); the
        envelope_profile_hash is also exposed to let the console render a shortened hash for
        operator verification. The register block stays NOT-exposed on the primary dock —
        it's one-tap-away under the existing det() fold alongside attendant_last."""
        if not isinstance(d, dict):
            return None
        state = d.get("state")
        if state not in ("empty", "working"):
            return None
        cust = d.get("custody") if isinstance(d.get("custody"), dict) else None
        return {
            "state":    state,
            "reason":   d.get("reason"),
            "build_id": d.get("build_id"),
            "steward_role": cust.get("steward_role") if cust else None,
            "grant_enact_id": cust.get("grant_enact_id") if cust else None,
            "envelope_match": (d.get("envelope") or {}).get("match"),
        }

    @staticmethod
    def _shape_attendant_last(rec: dict | None) -> dict | None:
        """Trim a daemon audit record to what the surface reads: the last outcome, its timestamp,
        its age, and the attendant's self-name. None when the attendant has never acted."""
        if not isinstance(rec, dict):
            return None
        out = {
            "ts": rec.get("ts"),
            "outcome": rec.get("outcome"),
            "answered_by": rec.get("answered_by"),
        }
        age = _audit_age(rec.get("ts"))
        if age is not None:
            out["age_s"] = age
        return out

    def thread(self, conversation_id: str, user_id: str | None = None) -> dict:
        """The transcript so far — everything actually said (human turns + confirmed 7 replies).
        Lets the console rehydrate the whole conversation on reload."""
        with self._lock:
            th = self._threads.get(conversation_id)
            if th is None:
                return {"error": "unknown_conversation"}
            if not self._ownership_ok(th.get("user_id"), user_id):
                # Uniform-404 shape — see RunManager.status.
                return {"error": "unknown_conversation"}
            return {
                "conversation_id": conversation_id,
                "model": th["model"],
                "turns": list(th["turns"]),
                "canvas": list(th.get("canvas") or []),
            }

    def confirm(self, run_id: str, move_id: str, user_id: str | None = None) -> dict:
        # Confirm re-verifies ownership at fire-time against the Bearer-resolved user, not
        # only move_id. The two gates compose: the token resolution upstream re-asks who's
        # asking; the owner-match here is what makes it an admit-atom. move_id alone could
        # not catch "same console, different human" — the user-id must also match the run's
        # owner.
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return {"ok": False, "error": "unknown_run"}
            if not self._ownership_ok(run.get("user_id"), user_id):
                return {"ok": False, "error": "unknown_run"}
            if run["state"] != "awaiting_confirm":
                return {"ok": False, "error": "not_awaiting"}
            if move_id != run["proposal"].get("move_id"):
                return {"ok": False, "error": "move_id_mismatch"}
            res = run["gate"].confirm(move_id)
            run["state"] = "done"
            run["result"] = res
            # Only NOW does 7's reply enter the transcript — admitted by the human, never before.
            th = self._threads.get(run.get("conversation_id"))
            if th is not None:
                text = res.get("params", {}).get("text") if isinstance(res, dict) else None
                if is_blank(text):
                    text = (run.get("proposal") or {}).get("projection")
                if not is_blank(text):
                    safe_text = _neutralize_markup(text)
                    th["turns"].append({"who": "7", "text": safe_text})
                    turn_idx = len(th["turns"]) - 1
                    for atom in classify_turn("confirmed_do", safe_text, turn_index=turn_idx):
                        if _earns_canvas(atom, "confirmed_do"):
                            admitted, content = _admit_to_canvas(atom.content)
                            if admitted:
                                from ..canvas import Atom as A
                                atom = A(content=content, significance=atom.significance,
                                         kind=atom.kind, source_turn=turn_idx)
                                th.setdefault("canvas", []).append(atom.to_dict())
            self._persist_threads()
            return {"ok": True, "result": res}

    def decline(self, run_id: str, user_id: str | None = None) -> dict:
        # Decline re-verifies ownership too. A declining stranger must not be able to kill
        # another user's awaiting_confirm run.
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return {"ok": False, "error": "unknown_run"}
            if not self._ownership_ok(run.get("user_id"), user_id):
                return {"ok": False, "error": "unknown_run"}
            if run["state"] == "awaiting_confirm":
                run["state"] = "declined"
            return {"ok": True, "state": "declined"}
