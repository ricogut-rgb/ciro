"""Distinctions checker — verified, executable checks.

Each check is tagged to its distinction and ships with an adversarial test
that proves it catches the miss it names. A check without its failing-case
test is not done.

COVERAGE DECLARATION (honest boundary):

  Mechanizable (checked here):
    - renders / merely-parses
    - claimed / actual (capability-honesty)
    - satisfies-intent / satisfies-letter
    - coherent-difference (difference + coherence)
    - subtract / mutate-repair
    - author / auditor (canary — self-reported, not enforced)

  NOT mechanizable (human/eye — never claimed here):
    - said / done (as judgment — the gate IS the structural enforcement)
    - geometry / metaphor
    - coherent-form / trapped-density
    - carry / surface
    - posture / self
    - presence / tension / gestalt

  A checker that claimed to evaluate the judgment distinctions would itself
  be a never-inflate violation. This module does not pretend full coverage.

TIER BOUNDARY:
  These checks are tested + wired but BYPASSABLE on this platform (a
  canary-shrinker — human is last catch, not the only one). Unskippable
  enforcement would require an own-orchestrator on the box. Don't label it
  gate-hard.
"""

from .checks import (
    check_renders,
    check_claimed_actual,
    check_satisfies_intent,
    check_coherent_difference,
    check_subtract_repair,
    check_author_auditor,
    run_all,
    MECHANIZABLE,
    NOT_MECHANIZABLE,
    CheckResult,
)
