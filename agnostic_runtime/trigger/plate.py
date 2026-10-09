"""The plate — what is on the operator's plate, as a FOLD over an append-only record.

Record: one JSONL file, ``$CATHEDRAL_BRIDGE_DIR/plate.jsonl`` (default
``~/.agnostic_runtime/cathedral/plate.jsonl``, the same bridge-dir convention as
cathedral.py). Lines are only ever appended. Nothing here rewrites or deletes a line.

Three event kinds:

  item  {"event":"item", "id", "title", "why", "kind", "choices":[str],
         "source":[str], "est":{"cost":1-3,"generativity":1-3,"pressure":1-3},
         "est_by":"cathedral", "status_hint", "owner", "ts"}
        Cathedral (or a seed script) proposes an item. A later item line with the same id
        replaces the earlier one in the fold (later events win). Estimates are Cathedral's
        guesses, carried as estimates: est_by says who guessed.

  act   {"event":"act", "id", "choice", "by", "ts"}
        A human tapped a choice. Written by the server only on an authenticated user tap
        (server.py). The latest act per item wins in the fold.

  undo  {"event":"undo", "id", "act_ts", "by", "ts"}
        A human took back a choice they made. act_ts names the act being undone (its ts).
        It counts only when, at that point in the file, the item's current act has that ts
        and was made by the same person: only the owner of an act may undo it. The item then
        reopens: its act is cleared and its choices return. The act line stays on the record
        and the fold lists its ts under the item's "undone". There is no undo of an undo: an
        undo whose act is not current (already undone, or never there) is ignored. Choosing
        again appends a new act.

        No time window. An undo is honest at any age because it rewrites nothing: the act,
        the run it started and the undo all stay on the record, and Cathedral is told in
        plain words that the choice was taken back. A window would put a clock inside a fold
        that has none, and would refuse a change of mind for arriving late. Whatever a late
        undo cannot take back (work already done outside the plate) is settled in the
        conversation the undo opens, not refused by the record.

Fold: deterministic. The same file gives the same plate and the same next pick. Malformed
lines are skipped, never repaired in place. Only an act settles an item; nothing the
machine writes (an item line, an estimate, a status_hint) can make an item settled.

Numbers: each item carries ``num``, its place in first-appearance order (1, 2, ...), shown as
"#00001". It comes from the fold of the append-only file, so it is never reassigned. A typed
"#00012 revisit" in the chat settles item 12 through the same code as a tap (see command()).

Ranking (``rank``): unsettled items that have choices, by pressure (high first), then cost
(low first), then generativity (high first), then the order they were first added.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

from agnostic_runtime.trigger.cathedral import bridge_dir

FILENAME = "plate.jsonl"
EST_KEYS = ("cost", "generativity", "pressure")
MAX_TEXT = 2000       # title / why / hint / source entry
MAX_CHOICE = 120
MAX_CHOICES = 8

# Prepared-B category.
# Prepared-B items are proposals the daemon has staged (built-on-throwaway-worktree,
# staged-config-as-patch, prepared-outbound-draft, held-in-pending) and that AWAIT the operator's
# explicit approve. Each one carries a content_hash, a byte_count, a file_list, a revert
# path and an expires_at — the UI renders them in a dedicated "awaiting your approval"
# group so legit-looking filenames cannot be shadowed by a buried diff body.
#
# Shipped on this surface: schema parsing, group routing, approve/reject events with
# hash-match discipline, the visual-safety rendering on the console, and the client-side
# 429 handling PRE-WIRED for a future daemon-emitted rate-limit (reads Retry-After /
# j.retry_after; renders a plain "try again in Ns" line and refuses nothing locally).
# Daemon-side pieces not yet built here: emission of prepared_b item events, apply-on-approve
# wrapper (`operator-apply`), encryption-at-rest for UNLOCK_ONLY patches, the hash-chained
# registry, the EnumEntry closed-enum classifier, rate-limit / cooldown enforcement (the
# 429 EMISSION — the surface already renders what the daemon returns), the external anchor
# regime. The surface renders what the daemon emits; it does not forge records.
PREPARED_B_SHAPES = (
    "B.build-on-throwaway-worktree",
    "B.stage-config-as-patch",
    "B.prepare-outbound-draft",
    "B.hold-in-pending",
)
MAX_FILES = 64             # file_list length cap on parse
MAX_PATH = 400             # per-path length cap
MAX_DIFF_BODY = 32 * 1024  # diff_body length cap at parse-time; UI additionally caps display
MAX_STDOUT_TAIL = 8 * 1024 # per-shape stdout from a prior A.record-operator-apply (sanitized)
_HASH_RE = re.compile(r"^sha256-v1:[0-9a-f]{64}$")

# One lock per process serializes "check settled, then append act" so a double tap cannot
# write two acts. (Appends from another process, e.g. the seed script, are single O_APPEND
# writes and do not need it.)
_LOCK = threading.Lock()


def default_path() -> str:
    return os.path.join(bridge_dir(), FILENAME)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_precise() -> str:
    """Act and undo timestamps carry microseconds. An act's ts is what an undo names, so two
    acts on one item (choose, undo, choose again in the same second) must not share one."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --- validation -----------------------------------------------------------------------------

def _text(v: Any, limit: int = MAX_TEXT) -> str | None:
    if not isinstance(v, str):
        return None
    v = v.strip()
    if not v or len(v) > limit:
        return None
    return v


def _valid_prepared_b(pb: Any) -> dict | None:
    """Return a clean prepared-B envelope, or None if malformed.

    The daemon emits this inside an item event when the item is a staged B-shape
    awaiting the operator's approve. Shape-id must be one of PREPARED_B_SHAPES (closed-enum
    at the surface too — a nonsense shape value is a parse-time refuse). Content-hash
    binds the approve to one specific state (per Addendum A A.2 consume-on-admit).
    Expires_at is strict (24h in design); UI renders a grace band but does not permit
    approve past expiry — the approve path re-verifies.
    """
    if not isinstance(pb, dict):
        return None
    shape_id = pb.get("shape_id")
    if shape_id not in PREPARED_B_SHAPES:
        return None
    requester = _text(pb.get("requester_user_id"), 128)
    if requester is None:
        return None
    byte_count = pb.get("byte_count")
    if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count < 0:
        return None
    content_hash = pb.get("content_hash")
    if not isinstance(content_hash, str) or not _HASH_RE.match(content_hash):
        return None
    files = pb.get("file_list", [])
    if not isinstance(files, list) or len(files) > MAX_FILES:
        return None
    clean_files: list[str] = []
    for p in files:
        p2 = _text(p, MAX_PATH)
        if p2 is None:
            return None
        clean_files.append(p2)
    revert_path = _text(pb.get("revert_path"), MAX_TEXT)
    if revert_path is None:
        return None
    expires_at = _text(pb.get("expires_at"), 40)
    if expires_at is None:
        return None
    diff_body = pb.get("diff_body") if isinstance(pb.get("diff_body"), str) else ""
    if len(diff_body) > MAX_DIFF_BODY:
        diff_body = diff_body[:MAX_DIFF_BODY]
    stdout_tail = pb.get("stdout_tail") if isinstance(pb.get("stdout_tail"), str) else ""
    if len(stdout_tail) > MAX_STDOUT_TAIL:
        stdout_tail = stdout_tail[:MAX_STDOUT_TAIL]
    description = _text(pb.get("description"), MAX_TEXT) or ""
    unlock_only = bool(pb.get("unlock_only")) if isinstance(pb.get("unlock_only"), bool) else False
    return {
        "shape_id": shape_id,
        "requester_user_id": requester,
        "byte_count": byte_count,
        "content_hash": content_hash,
        "file_list": clean_files,
        "revert_path": revert_path,
        "expires_at": expires_at,
        "diff_body": diff_body,
        "stdout_tail": stdout_tail,
        "description": description,
        "unlock_only": unlock_only,
    }


def _valid_item(ev: dict) -> dict | None:
    """Return a clean item, or None if the line is not a well-formed item."""
    iid = _text(ev.get("id"), 200)
    title = _text(ev.get("title"))
    if iid is None or title is None:
        return None
    why = ev.get("why") if isinstance(ev.get("why"), str) else ""
    choices = ev.get("choices", [])
    if not isinstance(choices, list) or len(choices) > MAX_CHOICES:
        return None
    clean_choices: list[str] = []
    for c in choices:
        c2 = _text(c, MAX_CHOICE)
        if c2 is None or c2 in clean_choices:
            return None
        clean_choices.append(c2)
    src = ev.get("source", [])
    if isinstance(src, str):
        src = [src]
    if not isinstance(src, list) or not all(isinstance(s, str) and len(s) <= MAX_TEXT for s in src):
        return None
    est = ev.get("est")
    if not isinstance(est, dict):
        return None
    clean_est = {}
    for k in EST_KEYS:
        n = est.get(k)
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 3:
            return None
        clean_est[k] = n
    owner = ev.get("owner")
    if owner is not None and not isinstance(owner, str):
        return None
    # Prepared-B items (optional). If present and malformed, the whole item line is
    # refused — a legit-looking title must never shadow a corrupt prepared-B body.
    pb_raw = ev.get("prepared_b")
    prepared_b = None
    if pb_raw is not None:
        prepared_b = _valid_prepared_b(pb_raw)
        if prepared_b is None:
            return None
    return {
        "id": iid,
        "title": title,
        "why": why[:MAX_TEXT],
        "kind": ev.get("kind") if isinstance(ev.get("kind"), str) else "",
        "choices": clean_choices,
        "source": [s for s in src if s.strip()],
        "est": clean_est,
        "est_by": ev.get("est_by") if isinstance(ev.get("est_by"), str) else "cathedral",
        "status_hint": ev.get("status_hint") if isinstance(ev.get("status_hint"), str) else "",
        "owner": owner,
        "ts": ev.get("ts") if isinstance(ev.get("ts"), str) else "",
        "prepared_b": prepared_b,
    }


# --- the record: append only ----------------------------------------------------------------

def _append(path: str, event: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    # One write on an O_APPEND descriptor: the line lands whole at the end, never mid-file.
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def append_item(path: str, item: dict) -> dict:
    """Append one item event. Raises ValueError if the item is not well-formed."""
    ev = dict(item)
    ev.setdefault("est_by", "cathedral")
    ev.setdefault("ts", _now())
    clean = _valid_item(ev)
    if clean is None:
        raise ValueError("malformed item")
    out = {"event": "item", **clean}
    _append(path, out)
    return out


def read_events(path: str) -> list[dict]:
    """Every well-formed JSON object line, in file order. A missing file is an empty record."""
    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict):
                    out.append(ev)
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError):
        return []
    return out


# --- the fold -------------------------------------------------------------------------------

def fold(events: Iterable[dict]) -> list[dict]:
    """Current items, in first-added order, each with its latest act (or None).

    Pure: no I/O, no clock. An act names an item id and a choice; it counts only if the
    choice is one of the item's choices at the time the act was written.
    """
    items: dict[str, dict] = {}
    order: list[str] = []
    gone: dict[tuple, str] = {}     # (item id, act ts) -> choice, for acts taken back
    for ev in events:
        what = ev.get("event")
        if what == "item":
            clean = _valid_item(ev)
            if clean is None:
                continue
            prev = items.get(clean["id"])
            if prev is None:
                order.append(clean["id"])
                clean["act"] = None
                clean["undone"] = []
                clean["replaced"] = []
                clean["decision"] = None  # prepared-B approve/reject terminal
            else:
                clean["act"] = prev["act"]
                clean["undone"] = prev["undone"]
                clean["replaced"] = prev["replaced"]
                clean["decision"] = prev.get("decision")
            items[clean["id"]] = clean
        elif what == "act":
            iid, choice = ev.get("id"), ev.get("choice")
            it = items.get(iid) if isinstance(iid, str) else None
            if it is None or not isinstance(choice, str) or choice not in it["choices"]:
                continue
            it["act"] = {"choice": choice, "by": ev.get("by"), "ts": ev.get("ts")}
            # A typed re-choose: the act names the act it replaced. Shown so an Undo can say
            # plainly that taking this back reopens the card and does NOT restore that one.
            rep = ev.get("replaces")
            if isinstance(rep, str) and rep in it["replaced"] and (iid, rep) in gone:
                it["act"]["replaced"] = gone[(iid, rep)]
        elif what == "undo":
            iid, act_ts, by = ev.get("id"), ev.get("act_ts"), ev.get("by")
            it = items.get(iid) if isinstance(iid, str) else None
            cur = it["act"] if it is not None else None
            if (cur is None or not isinstance(act_ts, str) or not isinstance(by, str)
                    or cur.get("ts") != act_ts or cur.get("by") != by):
                continue
            it["undone"] = it["undone"] + [act_ts]
            if ev.get("rechoose") is True:
                it["replaced"] = it["replaced"] + [act_ts]
            gone[(iid, act_ts)] = cur.get("choice")
            it["act"] = None
        elif what in ("approve_prepared", "reject_prepared"):
            iid = ev.get("id")
            by = ev.get("by")
            it = items.get(iid) if isinstance(iid, str) else None
            if it is None or it.get("prepared_b") is None or not isinstance(by, str):
                continue
            if it.get("decision") is not None:
                continue  # terminal once decided; replay-safe
            # On approve, hash must match the item's current prepared_b.content_hash.
            # This closes "approve one state, admit another" at the fold layer — a
            # second write of the item with a changed hash moves the authority forward,
            # and any approve bound to the old hash is now stale (not applied by fold).
            if what == "approve_prepared":
                h = ev.get("approved_hash")
                if not isinstance(h, str) or h != it["prepared_b"]["content_hash"]:
                    continue
                it["decision"] = {"kind": "approved", "by": by,
                                  "ts": ev.get("ts") if isinstance(ev.get("ts"), str) else "",
                                  "approved_hash": h}
            else:
                it["decision"] = {"kind": "rejected", "by": by,
                                  "ts": ev.get("ts") if isinstance(ev.get("ts"), str) else ""}
    out = []
    for seq, iid in enumerate(order):
        it = items[iid]
        it["seq"] = seq
        it["num"] = seq + 1      # the visible "#00001": first-appearance order, never reassigned
        it["group"] = group_of(it)
        out.append(it)
    return out


def group_of(it: dict) -> str:
    pb = it.get("prepared_b")
    if pb is not None:
        d = it.get("decision")
        if d is None:
            return "awaiting_approval"
        return "settled"  # approved or rejected → settled bucket, with its own badge in UI
    if it.get("act"):
        return "settled"
    if it.get("choices"):
        return "needs_you"
    return "resting"


def rank_awaiting(items: list[dict]) -> list[dict]:
    """Prepared-B awaiting approval, soonest-expiring first — the one the clock argues
    for most strongly. Deterministic: ties fall to first-appearance order."""
    aw = [it for it in items if it["group"] == "awaiting_approval"]
    return sorted(aw, key=lambda it: (it["prepared_b"]["expires_at"], it["seq"]))


_WORD = {1: "low", 2: "medium", 3: "high"}


def _key(it: dict) -> tuple:
    e = it["est"]
    return (-e["pressure"], e["cost"], -e["generativity"], it["seq"])


def rank(items: list[dict]) -> list[dict]:
    """Unsettled items with choices, best first. Deterministic."""
    return sorted((it for it in items if it["group"] == "needs_you"), key=_key)


def reason(ranked: list[dict]) -> str:
    """One plain line saying why the first ranked item is first."""
    if not ranked:
        return ""
    top = ranked[0]
    e = top["est"]
    hint = top.get("status_hint") or ""
    parts = [_WORD[e["pressure"]] + " pressure" + (": " + hint if hint else ""),
             _WORD[e["cost"]] + " cost",
             _WORD[e["generativity"]] + " generativity"]
    line = "; ".join(parts)
    ties = sum(1 for it in ranked[1:] if _key(it)[:3] == _key(top)[:3])
    if ties:
        line += f" (tied with {ties} other{'s' if ties > 1 else ''}; the one added first comes first)"
    return line


def visible(items: list[dict], user_id: str | None) -> list[dict]:
    """The items this caller may see. Same rule as RunManager._ownership_ok: an admin
    (user_id None) sees everything; a user sees only items whose owner is that user."""
    if user_id is None:
        return list(items)
    return [it for it in items if it.get("owner") is not None and it["owner"] == user_id]


def plate_view(path: str, user_id: str | None) -> dict:
    items = visible(fold(read_events(path)), user_id)
    ranked = rank(items)
    aw = rank_awaiting(items)
    nxt = ranked[0] if ranked else None
    return {
        "items": [_public(it) for it in items],
        "next": _public(nxt) if nxt else None,
        "reason": reason(ranked),
        "order": [it["id"] for it in ranked],
        # Prepared-B queue — items awaiting the operator's approve. Ordered by soonest expiry so
        # the one the clock argues for most strongly renders first. UI shows them in a
        # dedicated "awaiting your approval" section outside the needs_you group so the
        # register is clearly different (approve/reject on a staged diff, not a free
        # choice on an open question).
        "awaiting_approval": [it["id"] for it in aw],
    }


def _public(it: dict) -> dict:
    return {k: v for k, v in it.items() if k != "owner"}


# --- the one write a human makes ------------------------------------------------------------
#
# Every human write goes through _act_locked / _undo_locked, with _LOCK held: a tap
# (/plate/act), an Undo (/plate/undo) and a typed "#00012 revisit" command all share the same
# validation and the same append. Each helper re-reads and re-folds the record, so two calls
# in a row under one hold of the lock (a re-choose: undo, then act) each see the line the
# other wrote.

def _mine(path: str, user_id: str) -> dict[str, dict]:
    return {it["id"]: it for it in visible(fold(read_events(path)), user_id)}


def _act_locked(path: str, item_id: str, choice: str, user_id: str,
                replaces: str | None = None) -> tuple[int, dict]:
    it = _mine(path, user_id).get(item_id)
    if it is None:
        return 404, {"error": "unknown_item"}
    if choice not in it["choices"]:
        return 400, {"error": "invalid_choice"}
    if it["act"] is not None:
        return 409, {"error": "already_settled", "act": it["act"]}
    ts = _now_precise()
    while ts in it["undone"]:
        ts = _now_precise()
    ev = {"event": "act", "id": item_id, "choice": choice, "by": user_id, "ts": ts}
    if replaces is not None:
        ev["replaces"] = replaces
    _append(path, ev)
    return 200, {"act": ev, "title": it["title"], "num": it["num"]}


def _undo_locked(path: str, item_id: str, user_id: str, act_ts: str | None,
                 rechoose: bool = False) -> tuple[int, dict]:
    it = _mine(path, user_id).get(item_id)
    if it is None:
        return 404, {"error": "unknown_item"}
    cur = it["act"]
    if cur is not None and cur.get("by") != user_id:
        return 404, {"error": "unknown_item"}
    if cur is None:
        return 409, {"error": "nothing_to_undo"}
    if act_ts is not None and act_ts != cur.get("ts"):
        return 409, {"error": "stale_undo", "act": cur}
    ev = {"event": "undo", "id": item_id, "act_ts": cur["ts"], "by": user_id,
          "ts": _now_precise()}
    if rechoose:
        ev["rechoose"] = True
    _append(path, ev)
    return 200, {"undo": ev, "undid": cur, "title": it["title"], "num": it["num"]}


def act(path: str, item_id: Any, choice: Any, user_id: str) -> tuple[int, dict]:
    """Append one act for a human tap. Returns (http_status, payload).

    404 unknown_item covers both "no such item" and "not yours" (uniform, like runs.py).
    400 invalid_choice only after ownership passes. 409 already_settled: the item already
    has an act, so a double tap writes one act, not two.
    """
    if not isinstance(item_id, str) or not isinstance(choice, str):
        return 400, {"error": "bad_request"}
    with _LOCK:
        return _act_locked(path, item_id, choice, user_id)


# --- prepared-B approve / reject (surface side) ----------------------------------------------
#
# A prepared-B item is a daemon-staged proposal (worktree build, config patch, outbound draft,
# pending hold) that AWAITS the operator's explicit approve. Approve binds to the specific
# content_hash the item carried at the moment of approve — the daemon's apply path
# re-verifies at admit time. The surface's job is to make the hash-bind legible and to
# refuse approve past expiry on its side too (defense-in-depth: the daemon re-checks).
#
# Shipped here: event append with hash-match validation, expiry refusal, single-decision
# idempotency (409 already_decided on repeated approve/reject). The daemon-side admit
# (encryption-at-rest unseal, worktree merge, outbound send, consume-on-admit scan) is
# not in this layer.

def _parse_rfc3339(ts: str) -> datetime | None:
    try:
        # Accept "...Z" or offset form; both land in UTC-aware datetime.
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _decide_locked(path: str, item_id: str, kind: str, user_id: str,
                   approved_hash: str | None) -> tuple[int, dict]:
    """kind is 'approved' or 'rejected'. Hash match required on approve only."""
    it = _mine(path, user_id).get(item_id)
    if it is None or it.get("prepared_b") is None:
        return 404, {"error": "unknown_item"}
    pb = it["prepared_b"]
    if it.get("decision") is not None:
        return 409, {"error": "already_decided", "decision": it["decision"]}
    if kind == "approved":
        if not isinstance(approved_hash, str) or approved_hash != pb["content_hash"]:
            # One of two cases: the browser is holding a stale view (the item was re-staged
            # with a new hash), or the request is forged. Either way, don't record approve.
            return 409, {"error": "hash_mismatch", "expected": pb["content_hash"]}
        # Expiry: strict refusal past expires_at. The design's grace band is a UX render on
        # the console (shows "needs re-approve"); it is NOT a widened approve window on the
        # server. If the clock is past, the daemon would refuse anyway (24h strict).
        exp = _parse_rfc3339(pb["expires_at"])
        if exp is not None:
            now = datetime.now(timezone.utc)
            if now > exp:
                return 409, {"error": "expired", "expires_at": pb["expires_at"]}
    ev = {"event": "approve_prepared" if kind == "approved" else "reject_prepared",
          "id": item_id, "by": user_id, "ts": _now_precise()}
    if kind == "approved":
        ev["approved_hash"] = approved_hash
    _append(path, ev)
    return 200, {"decision": {"kind": kind, "by": user_id, "ts": ev["ts"],
                              **({"approved_hash": approved_hash} if kind == "approved" else {})},
                 "title": it["title"], "num": it["num"], "shape_id": pb["shape_id"]}


def approve_prepared(path: str, item_id: Any, approved_hash: Any,
                     user_id: str) -> tuple[int, dict]:
    """Append one approve_prepared event. Hash must match the item's current content_hash."""
    if not isinstance(item_id, str) or not isinstance(approved_hash, str):
        return 400, {"error": "bad_request"}
    with _LOCK:
        return _decide_locked(path, item_id, "approved", user_id, approved_hash)


def reject_prepared(path: str, item_id: Any, user_id: str) -> tuple[int, dict]:
    """Append one reject_prepared event. No hash bind — reject is a no-op on the daemon
    side (the staged artifact is discarded via its own revert path)."""
    if not isinstance(item_id, str):
        return 400, {"error": "bad_request"}
    with _LOCK:
        return _decide_locked(path, item_id, "rejected", user_id, None)


def undo(path: str, item_id: Any, user_id: str, act_ts: Any = None) -> tuple[int, dict]:
    """Append one undo of the item's current act. Returns (http_status, payload).

    Same lock as act(), so simultaneous undos (or an undo racing a tap) record exactly one.
    404 unknown_item covers "no such item", "not yours" and "the current act is someone
    else's" (uniform). 409 nothing_to_undo: the item is not settled. When act_ts is given
    (the console sends the act its Undo button was drawn for) and it is not the current act,
    409 stale_undo: an old button never undoes a newer choice.
    """
    if not isinstance(item_id, str) or (act_ts is not None and not isinstance(act_ts, str)):
        return 400, {"error": "bad_request"}
    with _LOCK:
        return _undo_locked(path, item_id, user_id, act_ts)


# --- visible numbers and typed commands -----------------------------------------------------
#
# NUMBER RULE: an item's number is its place in the order its id FIRST appears as a
# well-formed item line in plate.jsonl, counting from 1 across the whole file (every owner).
# The record is append-only, so a number is never reassigned: a later item line for the same
# id keeps its number, and a new id only ever gets the next number. Shown as "#00001".
#
# COMMAND GRAMMAR (a human's typed chat message, parsed server-side on an authenticated /run):
#   "#<number> <choice>"   e.g. "#00012 revisit", "#12 Revisit", "#3 A"
# The whole message, trimmed, must be one line matching ^#0*([0-9]{1,5})[ \t]+(\S.*)$, with
# ASCII digits only ("#٣" and "#３" are ordinary text). Anything else (a bare "#00012", the
# command inside a longer message, two lines) is an ordinary message.

COMMAND_RE = re.compile(r"#0*([0-9]{1,5})[ \t]+(\S.*)")
_TRAIL = ".!,;:"
# Common short words that must never pick a choice on their own (the whole-word and prefix rules).
STOPWORDS = frozenset("the and on of to for with now it is a an in at or be this that yes no ok".split())


def label(num: int) -> str:
    return "#%05d" % num


def parse_command(text: Any) -> tuple[int, str] | None:
    """(number, choice text) for a typed plate command, or None for an ordinary message."""
    if not isinstance(text, str):
        return None
    t = text.strip()
    if "\n" in t or "\r" in t:
        return None
    m = COMMAND_RE.fullmatch(t)
    if m is None:
        return None
    return int(m.group(1)), m.group(2).strip()


def _words(s: str) -> list[str]:
    return [w for w in re.split(r"[^0-9a-z]+", s.casefold()) if w]


def _phrases(choice: str, n: int) -> set[str]:
    w = _words(choice)
    return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}


def is_question(query: str) -> bool:
    """A "?" in the typed choice means the person is asking, not choosing."""
    return "?" in query


def match_choice(query: str, choices: list[str]) -> tuple[str | None, list[str]]:
    """Resolve typed text to one of an item's choices. Returns (choice, candidates).

    When in doubt, nothing: (None, []) or, if it could be several, (None, candidates). Rules,
    in order; the first that finds anything decides:
      1. the exact text of a choice;
      2. a question (any "?"): nothing;
      3. the same text ignoring case (and a trailing . ! , ; :);
      4. one letter: only a choice that IS that letter or is labelled "<letter> · ..."
         ("a" -> "A · ship ..."); any other single letter picks nothing;
      5. one word of 2+ characters, not a stopword, that starts a choice's FIRST word
         ("rev" -> "revisit", "inst" -> "install", "keep" -> "keep parked");
      6. whole words inside a choice, every word 3+ letters and not a stopword
         ("honest" -> "A · ship the honest ...", "injury limits" -> "C · injury limits ...").
    One candidate is a match; more than one is ambiguous (no choice; the candidates return).
    """
    q = query.strip()
    if q in choices:
        return q, [q]
    if is_question(q):
        return None, []
    q2 = q.rstrip(_TRAIL).strip().casefold()
    if not q2:
        return None, []

    def decide(hits: list[str]) -> tuple[str | None, list[str]] | None:
        if len(hits) == 1:
            return hits[0], hits
        if hits:
            return None, hits
        return None

    got = decide([c for c in choices if c.casefold() == q2])
    if got:
        return got
    if len(q2) == 1:
        return decide([c for c in choices
                       if c.casefold() == q2 or c.casefold().startswith(q2 + " ·")]) or (None, [])
    if re.fullmatch(r"[0-9a-z]{2,}", q2) and q2 not in STOPWORDS:
        got = decide([c for c in choices if _words(c) and _words(c)[0].startswith(q2)])
        if got:
            return got
    qw = _words(q2)
    if qw and " ".join(qw) == re.sub(r"\s+", " ", q2) and all(
            len(w) >= 3 and w not in STOPWORDS for w in qw):
        got = decide([c for c in choices if " ".join(qw) in _phrases(c, len(qw))])
        if got:
            return got
    return None, []


def command(path: str, num: Any, query: Any, user_id: str) -> tuple[int, dict]:
    """A typed "#<num> <choice>" from a human. Returns (http_status, payload).

    Resolves num to an item visible to user_id; another user's number reads exactly like an
    unknown one (404 unknown_item). Matches the choice (match_choice); a question ("?"), no
    match or an ambiguous one appends nothing (400 question / no_match / ambiguous_choice,
    each with the item's choices).
    Then, all under ONE hold of _LOCK and through the same helpers as /plate/act and
    /plate/undo:
      unsettled                  -> one act                                 op "act"
      settled, the same choice   -> nothing                                 op "already"
      settled, another choice    -> undo of the current act, then the act   op "react"
    Both lines of a re-choose are by user_id, so the fold reads: chose X, took it back, chose Y.
    The undo carries rechoose=true and the act replaces=<X's ts>, so the fold can say plainly
    that undoing Y reopens the card and does not restore X (act["replaced"] = X's choice).
    """
    if not isinstance(num, int) or isinstance(num, bool) or not isinstance(query, str):
        return 400, {"error": "bad_request"}
    with _LOCK:
        it = next((x for x in _mine(path, user_id).values() if x["num"] == num), None)
        if it is None or (it["act"] is not None and it["act"].get("by") != user_id):
            # Unknown, not yours, or settled by someone else (only an act's maker may take it
            # back, the rule /plate/undo keeps): one uniform answer.
            return 404, {"error": "unknown_item"}
        base = {"num": num, "id": it["id"], "title": it["title"], "choices": list(it["choices"])}
        if query.strip() not in it["choices"] and is_question(query):
            return 400, {"error": "question", **base}
        choice, hits = match_choice(query, it["choices"])
        if choice is None:
            if not hits:
                return 400, {"error": "no_match", **base}
            return 400, {"error": "ambiguous_choice", "candidates": hits, **base}
        cur = it["act"]
        if cur is not None and cur.get("choice") == choice:
            return 200, {"op": "already", "act": cur, **base}
        if cur is None:
            code, r = _act_locked(path, it["id"], choice, user_id)
            if code != 200:
                return code, {**r, **base}
            return 200, {"op": "act", "act": r["act"], **base}
        code, u = _undo_locked(path, it["id"], user_id, cur.get("ts"), rechoose=True)
        if code != 200:
            return code, {**u, **base}
        code, r = _act_locked(path, it["id"], choice, user_id, replaces=cur.get("ts"))
        if code != 200:
            # Only reachable if another process rewrote the item's choices between the two
            # appends. Report what is on record (the undo); never claim the act landed.
            return code, {**r, "undo": u["undo"], "undid": cur, **base}
        return 200, {"op": "react", "act": r["act"], "undo": u["undo"], "undid": cur, **base}


# --- seed: a minimal generic example showing the plate-item shape ---------------------------
#
# This seeds a new plate with one item per kind so a fresh operator can see the shape and the
# surface wiring without a live record. Overwrite seed_items() in a deployment to seed the plate
# with your own open items. (The server does not depend on this; it reads from the append-only
# record. seed() is a CLI convenience: `python -m agnostic_runtime.trigger.plate seed PATH`.)

_FOLDER_CHOICES = ["done", "keep", "revisit"]


def seed_items() -> list[dict]:
    return [
        {
            "id": "example-decision",
            "title": "Example decision card",
            "why": ("A card with choices. The operator picks one and the record folds forward. "
                    "Replace this seed in your deployment with real open items."),
            "kind": "decision",
            "choices": ["A · one option", "B · another option"],
            "source": [],
            "est": {"pressure": 1, "cost": 1, "generativity": 1},
            "status_hint": "example",
        },
        {
            "id": "example-repo",
            "title": "Example repo card",
            "why": "A card that shows a repo's standing. No choice to make here.",
            "kind": "repo",
            "choices": [],
            "source": [],
            "est": {"pressure": 1, "cost": 1, "generativity": 1},
            "status_hint": "active",
        },
        {
            "id": "example-parked-folder",
            "title": "Example parked folder",
            "why": "A folder quiet for a while. Only the operator knows whether it is finished.",
            "kind": "parked_folder",
            "choices": list(_FOLDER_CHOICES),
            "source": [],
            "est": {"pressure": 1, "cost": 1, "generativity": 1},
            "status_hint": "",
        },
    ]


def seed(path: str, owner: str | None) -> int:
    n = 0
    for it in seed_items():
        it = dict(it, owner=owner)
        append_item(path, it)
        n += 1
    return n


def _main(argv: list[str]) -> int:
    usage = "usage: python -m agnostic_runtime.trigger.plate seed PATH [--owner USER_ID]"
    if len(argv) < 2 or argv[0] != "seed":
        print(usage, file=sys.stderr)
        return 2
    path = argv[1]
    owner = None
    if "--owner" in argv:
        i = argv.index("--owner")
        if i + 1 >= len(argv):
            print(usage, file=sys.stderr)
            return 2
        owner = argv[i + 1]
    n = seed(path, owner)
    print(f"appended {n} item events to {path}" + (f" (owner {owner})" if owner else " (no owner: admin-only)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
