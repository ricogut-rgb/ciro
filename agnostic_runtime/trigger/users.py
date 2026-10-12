"""Append-only user records — minimum-ship for the user-reachable surface.

PROJECTION, NOT AUTHORITY. This file is a materialized projection of user-birth events.
Once the substrate Record arrives, user births move there and this file becomes
rebuildable-from-Record. Nothing downstream should treat users.json as source-of-truth
AT THAT POINT — minimum-ship uses it only because the Record doesn't yet exist. If
something starts reading users.json as authority after the Record lands, that breaks
the projection/substrate invariant.

The honest floor: a user is `{id, email, declared_role, token, created_at}` and
nothing more. Records are APPENDED to a JSON array on disk; nothing mutates in
place. A role change is another APPEND whose id matches an earlier record;
latest-row-per-id wins on read (see `_latest_by_id`).

FIELD NAME — `declared_role`, not `role`. Role on a persistent user record culturally
promises posture-loading, and the primary operator's standing is explicit: "recorded,
NOT loaded as posture — the first moment stays bare." The name tells future readers
that this field is a historical self-label, not a live posture input. The per-turn
stamp uses the shorter name `role` because at that altitude it IS the operative
value for that turn.

VALUE SHAPE — `self:<text>`. The attestation travels with the value. Writers here
(``seed_user``, ``update_declared_role``) normalize every incoming declared_role to
the `self:` prefix at write-time; the raw read-back carries the prefix so any future
handler that does ``if role == 'attorney'`` fails against ``self:attorney`` and must
EXPLICITLY opt into reading a self-attested label. That is the right texture: this
field is self-attestation, and the shape says so. The wall sits at the value's
shape (a class-wall), not an enumeration of roles that would need maintaining as
new ones appear. Legacy bare values that pre-exist on disk are tolerated by readers
but are not written anymore — the display helper strips the prefix either way, and
code gating on exact values will route the prefixed form through its own opt-in.

DISPLAY — surfaces that render the self-label to a reader route the value through
``display_role()``, which strips the ``self:`` prefix (and tolerates legacy bare
values and None). Metadata hand-offs (``manager.start(role=...)``, structured logs)
should pass the RAW value, not the stripped one — the prefix is a feature at that
altitude.

No signup form yet — the encounter should be bare. Users are seeded manually with:

    python -m agnostic_runtime.trigger.users seed EMAIL [ROLE]

which prints the token once. The operator pastes it into the console's token field.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# The default lives next to the module so a fresh checkout works with no env
# config. AGNOSTIC_TRIGGER_USERS points elsewhere when the server runs out of a
# deployed state dir (same shape as AGNOSTIC_TRIGGER_STATE).
_DEFAULT_PATH = Path(__file__).resolve().parent / "users.json"


def _path() -> Path:
    env = os.environ.get("AGNOSTIC_TRIGGER_USERS")
    return Path(env) if env else _DEFAULT_PATH


def _load_raw() -> list[dict]:
    """Return the on-disk event list, or [] if missing/corrupt.

    Corruption never raises here; a bad file reads as empty so the server still
    boots. Individual-record validation happens in the readers below — a
    malformed row is skipped, never loaded.
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
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(records, fh, indent=2)
    os.replace(tmp, p)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")


_SELF_PREFIX = "self:"


def _normalize_declared_role(value: Optional[str]) -> str:
    """Normalize a declared_role into its on-disk value shape.

    The attestation travels with the value: every write becomes ``self:<text>``.
    If the caller already passed a ``self:``-prefixed value (e.g. an existing
    row being re-written through this path), we keep it as-is rather than
    double-prefixing to ``self:self:attorney``. Empty / None collapses to
    ``self:stranger`` — the honest floor for "no label given".
    """
    trimmed = (value or "").strip()
    if not trimmed:
        return _SELF_PREFIX + "stranger"
    if trimmed.startswith(_SELF_PREFIX):
        return trimmed
    return _SELF_PREFIX + trimmed


def display_role(declared_role: Optional[str]) -> str:
    """Strip the ``self:`` prefix for arriver-visible rendering.

    Tolerates both the new prefixed form AND legacy bare values that may
    pre-exist on disk from before the prefix was introduced; returns
    ``"stranger"`` for None / empty so a surface never has to None-check.
    Metadata hand-offs (``manager.start(role=...)``, structured logs) should
    pass the RAW value — the prefix is a feature at that altitude. This helper
    exists for display paths only.
    """
    if not declared_role:
        return "stranger"
    value = declared_role.strip()
    if not value:
        return "stranger"
    if value.startswith(_SELF_PREFIX):
        stripped = value[len(_SELF_PREFIX):].strip()
        return stripped or "stranger"
    return value


def seed_user(email: str, declared_role: str = "stranger",
              token: Optional[str] = None) -> dict:
    """Append one user-birth event. Returns the full record so the caller can
    print the token (it is only emitted in full here — at birth).

    Idempotent on EMAIL is NOT guaranteed and intentionally so: a second seed of
    the same email creates a second user (new id, new token). Use `user_for_email`
    for upsert semantics.
    """
    email = (email or "").strip()
    if not email:
        raise ValueError("email is required")
    uid = secrets.token_hex(8)
    tok = token or secrets.token_urlsafe(32)
    rec = {
        "id": uid,
        "email": email,
        "declared_role": _normalize_declared_role(declared_role),
        "token": tok,
        "created_at": _now(),
    }
    records = _load_raw()
    records.append(rec)
    _write_atomic(records)
    return rec


def update_declared_role(user_id: str, new_declared_role: str) -> Optional[dict]:
    """Append a role-change event for an existing user (never mutates prior rows).

    This is the explicit form of what `_latest_by_id` already does implicitly —
    making the append-only semantics first-class so a future reader does not have
    to infer them. Returns the new row, or None if no user with that id exists.

    The new row carries the CURRENT token and email (read from the latest fold
    at write-time) so a single row still describes the whole user state. Only
    `declared_role` and `created_at` change.
    """
    folded = _latest_by_id(_load_raw())
    prior = folded.get(user_id)
    if prior is None:
        return None
    new_row = {
        "id": user_id,
        "email": prior.get("email", ""),
        "declared_role": _normalize_declared_role(new_declared_role),
        "token": prior.get("token", ""),
        "created_at": _now(),
    }
    records = _load_raw()
    records.append(new_row)
    _write_atomic(records)
    return new_row


def _latest_by_id(records: list[dict]) -> dict[str, dict]:
    """Fold the append-only series: latest row per id wins.

    An update to a role (or any field, future-shape) is another APPEND whose id
    matches an earlier record; this fold reads the current truth from the whole
    series without mutating anything.
    """
    latest: dict[str, dict] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        uid = rec.get("id")
        if not isinstance(uid, str) or not uid:
            continue
        latest[uid] = rec
    return latest


def user_for_token(token: str) -> Optional[dict]:
    """Resolve a bearer token to the current user record, or None.

    Reads the FOLD (latest row per id). A token that still appears in an older
    row but was rotated in a later row returns the newest record; if the latest
    row carries a different token (rotation), the old token no longer resolves
    — that is the right shape.
    """
    if not isinstance(token, str) or not token:
        return None
    for rec in _latest_by_id(_load_raw()).values():
        if rec.get("token") == token:
            return dict(rec)
    return None


def user_id_for_token(token: str) -> Optional[str]:
    rec = user_for_token(token)
    return rec.get("id") if rec else None


def declared_role_for_token(token: str) -> Optional[str]:
    rec = user_for_token(token)
    return rec.get("declared_role") if rec else None


def user_for_email(email: str) -> Optional[dict]:
    if not isinstance(email, str) or not email.strip():
        return None
    for rec in _latest_by_id(_load_raw()).values():
        if rec.get("email") == email.strip():
            return dict(rec)
    return None


# -------- CLI --------

def _cli_seed(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m agnostic_runtime.trigger.users seed EMAIL [ROLE]",
              file=sys.stderr)
        return 2
    email = argv[0]
    declared_role = argv[1] if len(argv) > 1 else "stranger"
    existing = user_for_email(email)
    if existing is not None:
        print(f"user already seeded: {email} id={existing['id']}",
              file=sys.stderr)
        print(f"token: {existing['token']}")
        return 0
    rec = seed_user(email, declared_role)
    print(f"seeded user {rec['id']} email={rec['email']} "
          f"declared_role={rec['declared_role']}")
    print(f"token: {rec['token']}")
    print(f"records: {_path()}")
    return 0


def _main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: python -m agnostic_runtime.trigger.users seed EMAIL [ROLE]")
        return 0
    if argv[0] == "seed":
        return _cli_seed(argv[1:])
    print(f"unknown command: {argv[0]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
