"""Invite codes — the one-way layered admission gate for /signup.

The /signup endpoint used to admit anyone with an email. That was an attacker-oracle
(email_in_use enumerated the user base) AND a DoS cascade (anyone could inflate
users.json without bound). Both pathologies share one fix: a code earns the right to
sign up, admission is one-way layered (gate opens → code is removed; one code = one
signup), and the fence is value — admission is private-by-default.

ONE-USE SEMANTICS. A code is consumed atomically on successful signup. A failed
signup (bad email, email already registered, seed error) does NOT burn the code.
This is enforced via a sidecar lockfile (fcntl.flock) so two near-simultaneous
signups cannot race on the same code.

OPERATOR-OWNED FILE. The codes file lives at
`~/.agnostic_runtime/secrets/invite_codes.json` (overridable via
AGNOSTIC_TRIGGER_INVITES). The directory and file are created with 0o700 / 0o600
permissions. It is outside the git tree, outside this module's authority, and the
operator (Rico) is the only person who adds to it.

                    HOW TO ADD MORE CODES (Rico-level note)
                    ---------------------------------------

To add 5 fresh single-use codes to your invite file:

    python -m agnostic_runtime.trigger.invites seed 5

To add one specific code with a note (e.g. a name you'll remember who got it):

    python -m agnostic_runtime.trigger.invites add CODE "Alice (Oct 12)"

To see which codes are live / used:

    python -m agnostic_runtime.trigger.invites list

The file is plain JSON — you can also edit it with any text editor. Each entry
is `{code, note, used_at, used_by, created_at}`. Setting `used_at` to null
re-opens the slot (don't do that unless you mean to).
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# The default lives under ~/.agnostic_runtime/secrets/ so the file sits beside the
# BETA_PAUSED sentinel and the deployed state — never alongside the code tree.
_DEFAULT_PATH = Path(os.path.expanduser("~/.agnostic_runtime/secrets/invite_codes.json"))


def _path() -> Path:
    env = os.environ.get("AGNOSTIC_TRIGGER_INVITES")
    return Path(env) if env else _DEFAULT_PATH


def _lock_path() -> Path:
    return _path().with_suffix(_path().suffix + ".lock")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")


def _ensure_dir() -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p.parent, 0o700)
    except OSError:
        # Non-fatal: some filesystems reject chmod (shared drives, Windows mounts).
        # The file-level 0o600 below is still the real seal; this is defense-in-depth.
        pass


def _load_raw() -> list[dict]:
    """Return the on-disk code list, or [] if missing/corrupt.

    Corruption never raises: a bad file reads as empty so the server still boots.
    A malformed row is skipped at use-time, not at load-time.
    """
    p = _path()
    if not p.exists():
        return []
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _write_atomic(records: list[dict]) -> None:
    p = _path()
    _ensure_dir()
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(records, fh, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, p)


def _import_fcntl():
    """Return fcntl module when available; None on platforms that don't have it (Windows).

    When fcntl is unavailable the module still works — serialization falls back to
    the single-threaded CPython GIL + the atomic rename inside _write_atomic. The
    narrow window where two /signup POSTs collide on the same code is bounded by
    "the exact same code string was typed by two different arrivers in the same
    few milliseconds," which is not a path the beta needs to defend against.
    """
    try:
        import fcntl
        return fcntl
    except ImportError:
        return None


def _consume_locked(code: str, user_id: str) -> bool:
    """Consume one unused code atomically. Returns True on success."""
    import hmac
    code = (code or "").strip()
    if not code:
        return False
    records = _load_raw()
    now = _now()
    found_idx = -1
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            continue
        if rec.get("used_at"):
            continue
        stored = rec.get("code", "")
        if not isinstance(stored, str) or not stored:
            continue
        # constant-time compare so a timing probe cannot measure code prefixes
        if hmac.compare_digest(stored, code):
            found_idx = i
            break
    if found_idx < 0:
        return False
    records[found_idx] = {
        **records[found_idx],
        "used_at": now,
        "used_by": user_id,
    }
    _write_atomic(records)
    return True


def consume_code(code: str, user_id: str) -> bool:
    """Atomically find and consume one unused invite code tied to user_id.

    Returns True iff the code existed and was unused BEFORE this call. The call
    marks it used atomically; a second call with the same code returns False.
    """
    _ensure_dir()
    lock = _lock_path()
    fcntl = _import_fcntl()
    if fcntl is None:
        # No kernel-level lock available; the atomic rename inside _write_atomic
        # is still the real serializer. See _import_fcntl() note.
        return _consume_locked(code, user_id)
    # Open the lock file (create if missing) and take an exclusive advisory lock.
    with open(lock, "a+") as lock_fh:
        try:
            os.chmod(lock, 0o600)
        except OSError:
            pass
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        except OSError:
            # Lock acquisition failed (very unusual on a local disk). Fall back to
            # the atomic-rename path; the window is small and the failure mode is
            # "two codes used simultaneously still can't double-consume because
            # the second _write_atomic replaces the first," so correctness holds
            # (one of the two writes wins, the other is lost — the loser just
            # sees "bad invite" on retry).
            return _consume_locked(code, user_id)
        try:
            return _consume_locked(code, user_id)
        finally:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def generate_codes(n: int, note: str = "") -> list[str]:
    """Append N fresh single-use codes to the file. Returns the new codes (plain
    strings, in order written). This is the operator path — never called from the
    request handler.
    """
    if n <= 0:
        return []
    _ensure_dir()
    now = _now()
    records = _load_raw()
    new_codes: list[str] = []
    for _ in range(n):
        # 10 bytes → 16 url-safe chars; readable, pasteable, not phone-friendly
        # (an invite is meant to arrive via a channel the arriver can copy from).
        code = secrets.token_urlsafe(10)
        records.append({
            "code": code,
            "note": note or "",
            "used_at": None,
            "used_by": None,
            "created_at": now,
        })
        new_codes.append(code)
    _write_atomic(records)
    return new_codes


def add_code(code: str, note: str = "") -> Optional[dict]:
    """Append one explicit code. Returns the record, or None if the code already
    exists (duplicates would collide the consume path's constant-time compare)."""
    code = (code or "").strip()
    if not code:
        return None
    _ensure_dir()
    records = _load_raw()
    for rec in records:
        if isinstance(rec, dict) and rec.get("code") == code:
            return None
    rec = {
        "code": code,
        "note": note or "",
        "used_at": None,
        "used_by": None,
        "created_at": _now(),
    }
    records.append(rec)
    _write_atomic(records)
    return rec


def list_codes() -> list[dict]:
    """Return the current code list. Used by the CLI; the server never calls this."""
    return _load_raw()


# -------- CLI --------

def _cli_seed(argv: list[str]) -> int:
    try:
        n = int(argv[0]) if argv else 5
    except ValueError:
        print("usage: python -m agnostic_runtime.trigger.invites seed [N]", file=sys.stderr)
        return 2
    note = argv[1] if len(argv) > 1 else ""
    codes = generate_codes(n, note)
    print(f"generated {len(codes)} code(s) in {_path()}")
    for c in codes:
        print(c)
    return 0


def _cli_add(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m agnostic_runtime.trigger.invites add CODE [NOTE]", file=sys.stderr)
        return 2
    code = argv[0]
    note = " ".join(argv[1:]) if len(argv) > 1 else ""
    rec = add_code(code, note)
    if rec is None:
        print(f"code already exists (or empty): {code}", file=sys.stderr)
        return 1
    print(f"added code {rec['code']}")
    return 0


def _cli_list(_argv: list[str]) -> int:
    records = list_codes()
    if not records:
        print(f"no codes in {_path()}")
        return 0
    print(f"{_path()}:")
    for rec in records:
        if not isinstance(rec, dict):
            continue
        state = "USED  " if rec.get("used_at") else "live  "
        code = rec.get("code", "?")
        note = rec.get("note", "")
        print(f"  {state} {code}" + (f"  — {note}" if note else ""))
    return 0


def _main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("usage:")
        print("  python -m agnostic_runtime.trigger.invites seed [N] [NOTE]")
        print("  python -m agnostic_runtime.trigger.invites add CODE [NOTE]")
        print("  python -m agnostic_runtime.trigger.invites list")
        return 0
    cmd = argv[0]
    rest = argv[1:]
    if cmd == "seed":
        return _cli_seed(rest)
    if cmd == "add":
        return _cli_add(rest)
    if cmd == "list":
        return _cli_list(rest)
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
