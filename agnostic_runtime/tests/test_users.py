"""Tests for the honest-wait (MVP2) users module.

Covers the shape change: email becomes nullable, and `chip` / `priority`
join the record. Legacy rows that predate these fields must still read
cleanly through `user_for_token` / `user_for_email`.

Run: python -m agnostic_runtime.tests.test_users
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


# Each test redirects AGNOSTIC_TRIGGER_USERS to a fresh temp file so writes
# don't touch the committed users.json. We import after setting the env so
# `_path()` reads it, but the module caches nothing path-related — it reads
# the env on every call — so a late env change works too.

class UsersMVP2Shape(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "users.json"
        self._prev = os.environ.get("AGNOSTIC_TRIGGER_USERS")
        os.environ["AGNOSTIC_TRIGGER_USERS"] = str(self.path)

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("AGNOSTIC_TRIGGER_USERS", None)
        else:
            os.environ["AGNOSTIC_TRIGGER_USERS"] = self._prev

    def test_seed_user_with_none_email_collapses_to_none(self) -> None:
        from agnostic_runtime.trigger.users import seed_user
        rec = seed_user(None, "stranger")
        self.assertIsNone(rec["email"])
        self.assertEqual(rec["declared_role"], "self:stranger")
        self.assertIsNone(rec["chip"])
        self.assertFalse(rec["priority"])
        self.assertTrue(rec["token"])
        self.assertTrue(rec["id"])

    def test_seed_user_with_empty_string_email_collapses_to_none(self) -> None:
        from agnostic_runtime.trigger.users import seed_user
        rec = seed_user("", "stranger")
        self.assertIsNone(rec["email"])

    def test_seed_user_with_whitespace_only_email_collapses_to_none(self) -> None:
        from agnostic_runtime.trigger.users import seed_user
        rec = seed_user("   ", "stranger")
        self.assertIsNone(rec["email"])

    def test_seed_user_with_full_shape(self) -> None:
        from agnostic_runtime.trigger.users import seed_user
        rec = seed_user("x@y.z", "stranger", chip="Time", priority=True)
        self.assertEqual(rec["email"], "x@y.z")
        self.assertEqual(rec["chip"], "Time")
        self.assertTrue(rec["priority"])

    def test_seed_user_default_chip_and_priority(self) -> None:
        from agnostic_runtime.trigger.users import seed_user
        rec = seed_user("x@y.z", "stranger")
        self.assertIsNone(rec["chip"])
        self.assertFalse(rec["priority"])

    def test_seed_user_whitespace_chip_collapses_to_none(self) -> None:
        from agnostic_runtime.trigger.users import seed_user
        rec = seed_user("x@y.z", "stranger", chip="   ")
        self.assertIsNone(rec["chip"])

    def test_legacy_row_without_chip_priority_still_reads(self) -> None:
        # Write a legacy-shape row directly to disk (no chip, no priority)
        legacy = [{
            "id": "deadbeefcafef00d",
            "email": "legacy@example.com",
            "declared_role": "self:stranger",
            "token": "legacy-token-abc",
            "created_at": "2026-09-01T00:00:00+0000",
        }]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(legacy), encoding="utf-8")
        from agnostic_runtime.trigger.users import (
            user_for_token, user_for_email,
        )
        got = user_for_token("legacy-token-abc")
        self.assertIsNotNone(got)
        assert got is not None  # for type-checkers
        self.assertEqual(got["id"], "deadbeefcafef00d")
        # The record simply doesn't carry chip/priority; callers get None/False
        # via .get() at their altitude, which is the honest contract.
        self.assertIsNone(got.get("chip"))
        self.assertFalse(got.get("priority", False))
        # user_for_email also tolerates the shape
        self.assertIsNotNone(user_for_email("legacy@example.com"))

    def test_update_declared_role_carries_chip_and_priority_forward(self) -> None:
        from agnostic_runtime.trigger.users import (
            seed_user, update_declared_role,
        )
        rec = seed_user("x@y.z", "stranger", chip="Time", priority=True)
        updated = update_declared_role(rec["id"], "attorney")
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated["declared_role"], "self:attorney")
        self.assertEqual(updated["chip"], "Time")
        self.assertTrue(updated["priority"])
        self.assertEqual(updated["token"], rec["token"])
        self.assertEqual(updated["email"], "x@y.z")

    def test_update_declared_role_from_legacy_row_defaults_fields(self) -> None:
        legacy = [{
            "id": "legacyid00000001",
            "email": None,
            "declared_role": "self:stranger",
            "token": "legacy-tok-2",
            "created_at": "2026-09-01T00:00:00+0000",
        }]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(legacy), encoding="utf-8")
        from agnostic_runtime.trigger.users import update_declared_role
        updated = update_declared_role("legacyid00000001", "attorney")
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertIsNone(updated["chip"])
        self.assertFalse(updated["priority"])
        self.assertIsNone(updated["email"])

    def test_user_for_email_does_not_match_none_record_email(self) -> None:
        from agnostic_runtime.trigger.users import seed_user, user_for_email
        seed_user(None, "stranger")
        # No email on the record → no search string finds it. That's fine.
        self.assertIsNone(user_for_email("x@y.z"))


class UsersCLI(unittest.TestCase):
    """The operator CLI still requires an email — public signup is not this path."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "users.json"
        self.env = os.environ.copy()
        self.env["AGNOSTIC_TRIGGER_USERS"] = str(self.path)
        self.repo_root = Path(__file__).resolve().parents[2]

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "agnostic_runtime.trigger.users", *args],
            cwd=self.repo_root,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_cli_seed_without_email_arg_fails(self) -> None:
        r = self._run("seed")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage:", r.stderr)

    def test_cli_seed_with_empty_email_arg_fails(self) -> None:
        r = self._run("seed", "")
        self.assertEqual(r.returncode, 2)
        self.assertIn("email is required", r.stderr)

    def test_cli_seed_with_email_succeeds(self) -> None:
        r = self._run("seed", "op@example.com")
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("seeded user", r.stdout)
        self.assertIn("token:", r.stdout)


if __name__ == "__main__":
    unittest.main()
