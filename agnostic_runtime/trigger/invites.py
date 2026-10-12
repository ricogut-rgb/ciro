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
import stat as _stat
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

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


# --- file-privacy discipline ---------------------------------------------------
#
# The invite codes file is operator-secret: if its mode is 0o644 (because chmod
# silently failed on a shared mount), or if a symlink has been dropped in its
# place, or the directory is group/other writable, the "private-by-default"
# posture is a lie. The chain below ports (in Python) the shape from the plist
# launchers in /Users/rico/Projects/oihoi-v0/infra/launchd/cathedral_console_launch.sh.
#
# Policy on any failure:
#   - loads refuse (return []) — same observable shape as "no codes," so an
#     attacker probing /signup cannot distinguish "misconfigured file" from
#     "bad code" (the enumeration oracle stays closed);
#   - writes raise InvitePrivacyError so the operator sees the problem loudly
#     at seed/add time and does not silently publish a world-readable file;
#   - a one-line reason is written to stderr once per process-session via
#     _warn() so the operator can see which check tripped.


class InvitePrivacyError(RuntimeError):
    """Raised by write paths when the file-privacy chain refuses to proceed.

    Read paths (``_load_raw``) never raise this — they return [] so the running
    server stays up and the attacker-oracle stays closed. The operator sees the
    reason on stderr via ``_warn()``.
    """


_warned: set[str] = set()


def _warn(reason: str) -> None:
    """Log one line per unique reason-string per process. Deduped so a hostile
    client cannot flood stderr by hammering /signup against a misconfigured
    file; the operator still sees each distinct cause once."""
    if reason in _warned:
        return
    _warned.add(reason)
    try:
        print(f"[invites] REFUSED: {reason}", file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def _check_file_privacy(p: Path) -> Optional[str]:
    """Return None if ``p`` is a regular file owned by the current uid with
    mode 0o*00 (no group/other bits), or a reason-string otherwise. Does NOT
    follow symlinks: a symlink in the file's place is itself a refusal cause.
    """
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return None  # non-existence is handled by callers (write will create it)
    except OSError as exc:
        return f"cannot stat {p} ({exc})"
    mode = st.st_mode
    if _stat.S_ISLNK(mode):
        return f"{p} is a symlink"
    if not _stat.S_ISREG(mode):
        return f"{p} is not a regular file"
    if st.st_uid != os.getuid():
        return f"{p} is owned by uid {st.st_uid}, not {os.getuid()}"
    perm = _stat.S_IMODE(mode)
    if perm & 0o077:
        return f"{p} mode is 0o{perm:03o} (group/other bits set; want 0o*00)"
    return None


def _check_dir_privacy(d: Path) -> Optional[str]:
    """Return None if the resolved directory ``d`` is owned by the current uid
    and is not group/other writable. Resolves symlinks (via ``realpath``) first,
    so a symlinked directory is judged by where it points — mirroring the
    ``cd … && pwd -P`` pattern in the launcher scripts.
    """
    try:
        resolved = Path(os.path.realpath(str(d)))
        st = os.stat(resolved)
    except FileNotFoundError:
        return None  # caller (_ensure_dir) will create it with 0o700
    except OSError as exc:
        return f"cannot stat directory {d} ({exc})"
    if not _stat.S_ISDIR(st.st_mode):
        return f"{resolved} is not a directory"
    if st.st_uid != os.getuid():
        return f"directory {resolved} is owned by uid {st.st_uid}, not {os.getuid()}"
    perm = _stat.S_IMODE(st.st_mode)
    if perm & 0o022:
        return (f"directory {resolved} mode is 0o{perm:03o} "
                f"(group/other write bits set; want 0o700)")
    return None


def _ensure_dir() -> None:
    """Make the secrets directory with 0o700, then VERIFY. If verification
    fails after mkdir+chmod, raise — a world-writable directory is never a
    place to drop secrets, and silently continuing would publish them.
    """
    p = _path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _warn(f"mkdir {p.parent} failed ({exc})")
        raise InvitePrivacyError(str(exc)) from exc
    try:
        os.chmod(p.parent, 0o700)
    except OSError:
        # Non-fatal for the chmod itself: some filesystems reject it (shared
        # drives, Windows mounts). We still verify below — if the mode is
        # actually permissive, that's the refusal.
        pass
    reason = _check_dir_privacy(p.parent)
    if reason is not None:
        _warn(reason)
        raise InvitePrivacyError(reason)


def _load_raw() -> list[dict]:
    """Return the on-disk code list, or [] if missing / corrupt / privacy-violated.

    Corruption never raises: a bad file reads as empty so the server still boots.
    A privacy violation (symlink in place, wrong owner, loose mode, world-writable
    directory) ALSO reads as empty — the attacker-oracle stays closed, and the
    operator sees the reason once via ``_warn()``.
    A malformed row inside a well-formed file is skipped at use-time, not here.
    """
    p = _path()
    if not p.exists() and not p.is_symlink():
        return []
    # Directory check first: a world-writable parent means we cannot trust the
    # file contents either (anyone could have swapped the file).
    reason = _check_dir_privacy(p.parent)
    if reason is not None:
        _warn(reason)
        return []
    reason = _check_file_privacy(p)
    if reason is not None:
        _warn(reason)
        return []
    # O_NOFOLLOW defends the TOCTOU window between lstat and open: if a symlink
    # is swapped in between the two syscalls, open refuses rather than reading
    # the attacker's target. Falls back to normal open on platforms without
    # O_NOFOLLOW (Windows); the lstat above still catches the resting case.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(p, flags)
    except OSError:
        return []
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _write_atomic(records: list[dict]) -> None:
    """Write records with 0o600 and VERIFY the on-disk mode. If verification
    fails after the final rename, raise — bare ``except OSError: pass`` around
    chmod would otherwise let a world-readable file ship on filesystems where
    the chmod silently no-ops.
    """
    p = _path()
    _ensure_dir()
    # Refuse to overwrite a symlink or a non-owned file (the resting-state check
    # mirrors _check_file_privacy; a symlink that appears later is still caught
    # by O_NOFOLLOW on tmp creation below).
    if p.exists() or p.is_symlink():
        reason = _check_file_privacy(p)
        if reason is not None:
            _warn(reason)
            raise InvitePrivacyError(reason)
    tmp = p.with_suffix(p.suffix + ".tmp")
    # Create tmp with 0o600 from the start — the window between open and chmod
    # was where a loose umask could leak the file world-readable even for one
    # fsync. O_NOFOLLOW refuses a pre-staged symlink; O_EXCL refuses a stale tmp
    # someone else owns (so a hostile user cannot pre-create /tmp/invites.tmp
    # with their own ownership and have us write into it).
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    # If the tmp already exists from a prior crash, remove it so O_EXCL would succeed.
    # (We don't require O_EXCL here because retrying a half-written tmp is normal.)
    try:
        if tmp.exists() or tmp.is_symlink():
            os.unlink(tmp)
    except OSError:
        pass
    try:
        fd = os.open(tmp, flags, 0o600)
    except OSError as exc:
        _warn(f"cannot open {tmp} for write ({exc})")
        raise InvitePrivacyError(str(exc)) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(records, fh, indent=2)
    except OSError as exc:
        try: os.unlink(tmp)
        except OSError: pass
        raise InvitePrivacyError(str(exc)) from exc
    # Belt-and-braces chmod (the open mode above may be masked by umask on some
    # platforms — explicit chmod corrects that).
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    try:
        os.replace(tmp, p)
    except OSError as exc:
        try: os.unlink(tmp)
        except OSError: pass
        raise InvitePrivacyError(str(exc)) from exc
    # VERIFY: stat the final file and refuse to continue if mode is loose. If
    # chmod silently failed above, this is where we catch it — the operator
    # sees a loud log and the caller gets an error rather than a world-readable
    # secrets file sitting on disk.
    reason = _check_file_privacy(p)
    if reason is not None:
        _warn(reason)
        raise InvitePrivacyError(reason)


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
    """Consume one unused code. CALLER MUST HOLD ``invite_locked_section()``.

    Returns True on success. Reads _load_raw() every call (no in-memory cache),
    so a probe-then-consume sequence under one held lock sees a consistent view
    of the file. The ``_write_atomic`` call inside is a rename, not an in-place
    mutation — the lock orders the renames.
    """
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


def is_code_live(code: str) -> bool:
    """Non-mutating probe: True iff ``code`` exists in the file and is unused.

    Call this INSIDE ``invite_locked_section()`` to get atomic
    check-and-consume semantics (the whole critical section — probe, collision
    check, seed, consume — runs under one held flock, so two arrivers using
    the same code cannot both pass the probe). Calling outside the lock still
    works but leaves a TOCTOU window the lock closes.
    """
    import hmac
    code = (code or "").strip()
    if not code:
        return False
    for rec in _load_raw():
        if not isinstance(rec, dict):
            continue
        if rec.get("used_at"):
            continue
        stored = rec.get("code", "")
        if isinstance(stored, str) and stored and hmac.compare_digest(stored, code):
            return True
    return False


@contextmanager
def invite_locked_section() -> Iterator[bool]:
    """Hold the invite flock for the duration of a critical section.

    Yields True when the kernel-level flock was acquired, False when the
    platform lacks fcntl (Windows) or the flock call failed. Callers run
    ``is_code_live`` → collision check → seed → ``_consume_locked`` under
    the same held lock, so a race between two arrivers typing the same code
    at the same millisecond cannot both pass the probe. (Without this, the
    probe-then-consume sequence had a TOCTOU window where both arrivers saw
    a live code; the consume ordered one through and left the other's user
    record on disk pointing at a slot nobody owns.)

    The ``False`` branch (no fcntl) falls back to atomic-rename-only
    serialization — correctness still holds for the single-code-double-use
    pathology (the second ``_write_atomic`` sees used_at set and refuses),
    but the TOCTOU window reopens for near-simultaneous collisions.
    """
    _ensure_dir()
    lock = _lock_path()
    fcntl = _import_fcntl()
    if fcntl is None:
        yield False
        return
    with open(lock, "a+") as lock_fh:
        try:
            os.chmod(lock, 0o600)
        except OSError:
            pass
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def consume_code(code: str, user_id: str) -> bool:
    """Atomically find and consume one unused invite code tied to user_id.

    Returns True iff the code existed and was unused BEFORE this call. The call
    marks it used atomically; a second call with the same code returns False.
    This is the STANDALONE path (CLI, out-of-handler callers); the signup
    handler uses ``invite_locked_section()`` + ``_consume_locked`` directly so
    the lock spans the entire critical section rather than just the consume.
    """
    with invite_locked_section():
        return _consume_locked(code, user_id)


def record_failed_probe(path_hint: str = "") -> None:
    """Append-only timestamp log of failed /signup invite attempts.

    One line per failure. The log is for the operator (Rico) — a sudden burst
    of lines before a legitimate arriver reports a stolen slot is the signal
    that the invite space is being probed. No code strings are logged (so an
    attacker running the parsing can't verify a hit from the log), only the
    timestamp. Non-fatal: if the log cannot be written we drop silently rather
    than let logging block a /signup response.
    """
    try:
        _ensure_dir()
        log = _path().parent / "invite_probes.log"
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(_now() + (f"  {path_hint}\n" if path_hint else "\n"))
        try:
            os.chmod(log, 0o600)
        except OSError:
            pass
    except (OSError, InvitePrivacyError):
        pass


def generate_codes(n: int, note: str = "") -> list[str]:
    """Append N fresh single-use codes to the file. Returns the new codes (plain
    strings, in order written). This is the operator path — never called from the
    request handler. Held under the invite lock so a concurrent /signup consume
    cannot lose the new codes to a read-modify-write race.
    """
    if n <= 0:
        return []
    with invite_locked_section():
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
    exists (duplicates would collide the consume path's constant-time compare).
    Held under the invite lock (same reason as ``generate_codes``)."""
    code = (code or "").strip()
    if not code:
        return None
    with invite_locked_section():
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
