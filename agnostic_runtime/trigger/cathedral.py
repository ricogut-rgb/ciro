"""The Cathedral door — a file-queue seam between the web surface and Claude Code.

The web surface is a DOOR, not a room. It never calls a model. When the human speaks to
Cathedral, this bridge appends one line to ``ask.jsonl`` and the run parks in
``awaiting_cathedral``. The real work happens in a Claude Code session that has ARMED the
door (``cathedral_wait.sh``), does the Cathedral work with its full posture/subagent/ZP
machinery, and writes one line back to ``reply.jsonl`` (``cathedral_reply.py``).

Why PULL and not push: nothing outside a Claude Code session can wake one. There is no
inbound endpoint on CC. The only primitive that actually crosses that boundary is a blocking
read inside CC itself. A durable cursor means an ask that arrived while the door was closed
is picked up on the next arm rather than silently skipped, and a heartbeat means the surface
can SAY the door is closed instead of swallowing the ask into a void.

Files (all under CATHEDRAL_BRIDGE_DIR, default ~/.agnostic_runtime/cathedral):
    ask.jsonl        server appends  {ts, run_id, conversation_id, text}
    reply.jsonl      CC appends      {ts, run_id, text, canvas?}
    cursor.txt       CC advances     count of ask lines already taken
    door.heartbeat   CC touches      mtime proves a session is listening
    door.present     waiter touches  mtime proves a PRESENT seat (cathedral_wait.sh) is armed;
                                     on delivery it holds the handed-off run_ids, one per line.
                                     The standing attendant never touches it (it only reads it,
                                     to take turns). Absent = no present seat; every reader is
                                     harmless on absence.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

DEFAULT_DIR = os.path.expanduser("~/.agnostic_runtime/cathedral")

# How stale the heartbeat may be and still count as "armed". cathedral_wait.sh touches it
# every 2s; 20s tolerates a slow loop without ever reporting a long-dead door as open.
ARMED_WINDOW = 20.0

# Audit outcomes that record the attendant NOT taking a turn (stepping aside for a present seat).
# They are legible in daemon_audit.jsonl but are not answer-path self-checks for the health line.
NON_ANSWER_OUTCOMES = frozenset({"yielded-present-seat", "lost-race"})


def bridge_dir() -> str:
    return os.environ.get("CATHEDRAL_BRIDGE_DIR") or DEFAULT_DIR


class CathedralBridge:
    """Append-only handoff. Owns no state the CC side needs; the files are the contract."""

    def __init__(self, directory: str | None = None):
        self.dir = directory or bridge_dir()
        self.ask_path = os.path.join(self.dir, "ask.jsonl")
        self.reply_path = os.path.join(self.dir, "reply.jsonl")
        self.cursor_path = os.path.join(self.dir, "cursor.txt")
        self.heartbeat_path = os.path.join(self.dir, "door.heartbeat")
        self.present_path = os.path.join(self.dir, "door.present")
        self._lock = threading.Lock()
        # Replies are tailed incrementally: we remember the byte offset already consumed and
        # the run_id -> reply map, so polling a parked run is O(new bytes), not O(file).
        self._reply_offset = 0
        self._replies: dict[str, dict] = {}
        try:
            os.makedirs(self.dir, exist_ok=True)
        except OSError:
            pass

    # --- the server side ------------------------------------------------

    def ask(self, run_id: str, conversation_id: str | None, text: str) -> bool:
        """Put one ask through the door. Returns False if the disk refused it — the caller
        must then surface a real failure rather than park a run nobody will ever answer."""
        line = json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "run_id": run_id,
            "conversation_id": conversation_id,
            "text": text,
        }, ensure_ascii=False)
        with self._lock:
            try:
                os.makedirs(self.dir, exist_ok=True)
                with open(self.ask_path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError:
                return False
        return True

    def reply_for(self, run_id: str) -> dict | None:
        """The CC session's answer to one run, or None while it is still working."""
        with self._lock:
            self._tail_replies()
            return self._replies.get(run_id)

    def _ask_run_ids(self) -> list[str]:
        out: list[str] = []
        try:
            with open(self.ask_path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    if not raw.strip():
                        continue
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        out.append("")
                        continue
                    out.append(rec.get("run_id") or "")
        except OSError:
            pass
        return out

    def _cursor(self) -> int:
        try:
            with open(self.cursor_path, "r", encoding="utf-8") as fh:
                return max(0, int((fh.read() or "0").strip() or 0))
        except (OSError, ValueError):
            return 0

    def settle_cursor(self) -> int:
        """Recompute the cursor as the run of LEADING asks that have actually been answered.

        The first version of this counted every ask on disk, so answering the newest ask in a
        batch marked the older ones taken and stranded them forever — the surface then showed
        "Cathedral is working…" for an ask nobody would ever answer again. That is the exact lie
        this door exists not to tell. An ask is taken only once its own answer is on disk.
        """
        with self._lock:
            self._tail_replies()
            answered = self._replies
            cursor = 0
            for run_id in self._ask_run_ids():
                if run_id and run_id in answered:
                    cursor += 1
                else:
                    break
            try:
                with open(self.cursor_path, "w", encoding="utf-8") as fh:
                    fh.write(str(cursor))
            except OSError:
                pass
            return cursor

    def stranded(self, run_id: str) -> bool:
        """Was this ask marked taken without ever being answered?

        settle_cursor() makes this unreachable through the normal path, so it is the
        defence-in-depth check: a hand-edited cursor, a --no-advance batch, or a future bug can
        still orphan an ask, and the surface must say so rather than pulse forever.
        """
        with self._lock:
            self._tail_replies()
            if run_id in self._replies:
                return False
            asks = self._ask_run_ids()
            try:
                idx = asks.index(run_id)
            except ValueError:
                return False
            return idx < self._cursor()

    def _tail_replies(self) -> None:
        # Called under the lock. A truncated file (fresh bridge dir) resets the offset rather
        # than reading garbage from the middle of a line.
        try:
            size = os.path.getsize(self.reply_path)
        except OSError:
            return
        if size < self._reply_offset:
            self._reply_offset = 0
            self._replies = {}
        if size == self._reply_offset:
            return
        try:
            with open(self.reply_path, "r", encoding="utf-8") as fh:
                fh.seek(self._reply_offset)
                chunk = fh.read()
                # Only consume up to the last COMPLETE line; a half-written line is left for
                # the next poll rather than parsed as truncated JSON.
                cut = chunk.rfind("\n")
                if cut < 0:
                    return
                consumed = chunk[: cut + 1]
                self._reply_offset += len(consumed.encode("utf-8"))
        except OSError:
            return
        for raw in consumed.splitlines():
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if isinstance(rec, dict) and isinstance(rec.get("run_id"), str):
                self._replies[rec["run_id"]] = rec

    def armed(self) -> bool:
        """Is a Claude Code session listening right now?"""
        try:
            return (time.time() - os.path.getmtime(self.heartbeat_path)) < ARMED_WINDOW
        except OSError:
            return False

    def heartbeat_age(self) -> float | None:
        """Seconds since the door heartbeat was last touched, or None if it never has been.

        The POSITIVE liveness signal behind the armed() boolean (invariant I / who-identity edge).
        armed() collapses to true/false at the ARMED_WINDOW cliff; the age is the raw number, so a
        heartbeat that is fresh-but-slowing, or just inside/outside the window, is VISIBLE instead
        of snapping between two states. The surface shows it so an attendant presence is legible as
        a live freshness, not a bare light that is on or off.
        """
        try:
            return max(0.0, time.time() - os.path.getmtime(self.heartbeat_path))
        except OSError:
            return None

    # --- taking turns: the present-seat signal --------------------------
    #
    # door.heartbeat cannot tell a present seat from the standing attendant: both touch it (that is
    # its job — "someone is at the door"). door.present is touched ONLY by cathedral_wait.sh, so it
    # is the distinct signal the attendant reads to yield. Both readers are harmless on absence:
    # no file means no present seat, never an error.

    def present_age(self) -> float | None:
        """Seconds since a present seat's waiter last touched door.present, or None if never."""
        try:
            return max(0.0, time.time() - os.path.getmtime(self.present_path))
        except OSError:
            return None

    def present_mtime(self) -> float | None:
        try:
            return os.path.getmtime(self.present_path)
        except OSError:
            return None

    def present_claims(self) -> set[str]:
        """run_ids the present seat's waiter handed to its session when it last delivered. Empty
        while it is merely listening (it truncates the file every loop) and on absence."""
        try:
            with open(self.present_path, "r", encoding="utf-8") as fh:
                raw = fh.read(65536)
        except OSError:
            return set()
        return {ln.strip() for ln in raw.splitlines() if ln.strip()}

    def waiting(self) -> int:
        """Asks that have been put through the door but not yet taken by a CC session."""
        return max(0, len(self._ask_run_ids()) - self._cursor())

    # --- who-identity edge: the standing-attendant audit log ------------
    #
    # The daemon (cathedral_door_daemon.py) writes one record per answer attempt to
    # daemon_audit.jsonl: {ts, run_id, answered_by, outcome, detail}. A hand-armed cathedral_wait.sh
    # session writes NO audit record (it answers through cathedral_reply.py, which never touches this
    # file). So the audit log is the UNFORGEABLE source of truth for WHO answered: a run_id with a
    # record here was answered by the STANDING ATTENDANT; a reply with no record was a present seat.
    # The console never parses the answer TEXT for a signature — it keys off this, surfaced by the
    # server. (README.cathedral_door.md, signature contract, points 1-2.)

    def _audit_path(self) -> str:
        return os.path.join(self.dir, "daemon_audit.jsonl")

    def _read_audit(self) -> list[dict]:
        out: list[dict] = []
        try:
            with open(self._audit_path(), "r", encoding="utf-8") as fh:
                for raw in fh:
                    if not raw.strip():
                        continue
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(rec, dict):
                        out.append(rec)
        except OSError:
            return []
        return out

    def audit_for(self, run_id: str) -> dict | None:
        """The attendant's most recent audit record for this run_id, or None if the attendant never
        touched it (i.e. a present seat answered, or no one has). The daemon may append more than one
        record for a run_id across retries; the LAST wins (it is the terminal outcome)."""
        if not run_id:
            return None
        latest = None
        for rec in self._read_audit():
            if rec.get("run_id") == run_id:
                latest = rec
        return latest

    # --- custody status (three-state render) ---------------------------
    #
    # door.custody_status.json is written by the daemon on every poll cycle. The dock reads it
    # together with armed() to compute the three honest states:
    #   closed  : heartbeat stale (armed() False) — the daemon isn't running.
    #   empty   : heartbeat fresh BUT custody state says "empty" (no active grant, OR an
    #             envelope-mismatch between the live launch and the bound custody).
    #   working : heartbeat fresh AND state=="working" (active custody + envelope matches).
    # (A revoked-but-alive daemon used to render as "working" when the dock only read
    # armed(); the custody_status signal is what makes that distinct.)
    def _custody_status_path(self) -> str:
        return os.path.join(self.dir, "door.custody_status.json")

    def custody_status(self) -> dict | None:
        try:
            with open(self._custody_status_path(), "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError:
            return None
        try:
            d = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(d, dict):
            return None
        return d

    def attendant_last(self) -> dict | None:
        """The attendant's most recent audit record overall — the 'last self-check' health signal.

        None when the attendant has never acted (daemon unloaded, or armed only by hand). This is
        what keeps a subtly-broken-but-heartbeating daemon from masquerading as healthy (006's
        finding): the heartbeat proves the process is alive, but THIS proves its answer PATH last
        did real work and with what outcome — a 'honest-failure'/'write-failed' here shows through
        even while the heartbeat is fresh.

        'yielded-present-seat' and 'lost-race' rows are skipped: they record the attendant stepping
        aside for, or being beaten to the write by, a present seat. That is not an answer-path
        outcome, so it must not read on the health line as "last answer did not complete".
        """
        last = None
        for rec in self._read_audit():
            if rec.get("run_id") and rec.get("outcome") not in NON_ANSWER_OUTCOMES:
                last = rec
        return last


# The console injects SVG into the canvas RAW (pre-existing: renderAtom/isSvg), so any SVG that
# reaches it executes whatever it carries. The first attempt here was a narrow pattern refusal —
# `<script`, ` on*=`, `javascript:` — and the re-inspection walked straight through it: HTML
# accepts `/` as an attribute separator, so `<svg><image/onerror="…"` never matched the
# whitespace-anchored handler pattern and fired live in the browser. A BOM before `<svg` beat the
# Python-side detection while the console's own `isSvg` still matched it.
#
# So the wall, not a better pattern: the door's writer does not put SVG on the canvas at all.
# A pattern refusal is a promise to out-think every future markup trick; a flat refusal is not.
# The underlying renderAtom sink is UNCHANGED and still open to the model backends — this closes
# --canvas, not the sink. SVG from Cathedral is deliberately NOT put on the canvas.
_SVG_ANYWHERE = re.compile(r"<\s*svg", re.IGNORECASE)

# A canvas atom is a thing a person reads on a panel, and it lands in the durable transcript
# forever. Both the per-atom and the WHOLE-REPLY caps matter: the re-inspection put 8 MB into
# threads.json as 400 atoms of 19,999 chars, each one individually under the per-atom cap.
MAX_ATOM_CHARS = 20_000
MAX_CANVAS_ATOMS = 24
MAX_CANVAS_CHARS = 100_000


def normalize_canvas(raw: object) -> list[dict]:
    """Accept only atoms the existing canvas renderer can draw. Anything else is dropped."""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    total = 0
    for item in raw:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if len(content) > MAX_ATOM_CHARS:
            continue
        if _SVG_ANYWHERE.search(content):
            continue
        if len(out) >= MAX_CANVAS_ATOMS or total + len(content) > MAX_CANVAS_CHARS:
            break
        total += len(content)
        sig = item.get("significance")
        if sig not in ("formed", "firming", "flowing"):
            sig = "firming"
        out.append({
            "content": content,
            "significance": sig,
            "kind": item.get("kind") if isinstance(item.get("kind"), str) else "draft",
            "source_turn": -1,
        })
    return out
