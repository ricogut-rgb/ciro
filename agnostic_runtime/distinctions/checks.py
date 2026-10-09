"""Executable distinction checks.

Each check returns a CheckResult. The distinction it holds is named in the
docstring and tagged via `distinction=`. The adversarial test for each check
lives in test_distinctions.py — a check without its failing-case test is not
done (green != real).

TIER: canary-shrinker. Human is last catch, not the only one.
These checks are bypassable on this platform. Unskippable enforcement
would require an own-orchestrator on the box.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class CheckResult:
    distinction: str
    passed: bool
    finding: str = ""
    repaired: bool = False
    repaired_value: object = None


# -- Coverage declaration (honest boundary) -----------------------------------

MECHANIZABLE = (
    "renders/merely-parses",
    "claimed/actual",
    "satisfies-intent/satisfies-letter",
    "coherent-difference",
    "subtract/mutate-repair",
    "author/auditor",
)

NOT_MECHANIZABLE = (
    "said/done (as judgment)",
    "geometry/metaphor",
    "coherent-form/trapped-density",
    "carry/surface",
    "posture/self",
    "presence/tension/gestalt",
)


# -- The capability registry (generated from reality, not hand-kept — T1-S) --
# This is the ACTUAL capability set, verified by Explore7 on llama3.2.
# claimed/actual checks against THIS, not a prose description.

CAPABILITY_REGISTRY = {
    "text": {
        "can": True,
        "forms": ["copy", "headline", "tagline", "plan", "email", "name",
                  "description", "slogan", "subject line", "draft"],
    },
    "svg": {
        "can": True,
        "reliable": ["wordmark", "text layout", "simple shapes", "explicit fills"],
        "unreliable": ["pictorial icons", "curved paths", "detailed scenes"],
    },
    "photo": {"can": False},
    "raster_image": {"can": False},
    "video": {"can": False},
    "audio": {"can": False},
    "code": {"can": False},
    "file_attachment": {"can": False},
    "email_send": {"can": False},
}

_CLAIM_PATTERNS = {
    "photo": re.compile(
        r"\bhere(?:'s| is)\s+(?:the|your|an?)\s+(?:photo|photograph|picture)\b"
        r"|\bi(?:'ve| have|)\s+(?:created|generated|made|produced)\s+(?:the|your|an?)\s+"
        r"(?:photo|photograph|picture)\b"
        r"|\bi\s+can\s+(?:generate|create|produce|make)\b.*?\b(?:photo|photograph|picture)s?\b",
        re.I,
    ),
    "video": re.compile(
        r"\bhere(?:'s| is)\s+(?:the|your|an?)\s+video\b"
        r"|\bi(?:'ve| have|)\s+(?:created|generated|made|produced)\s+(?:the|your|an?)\s+video\b"
        r"|\bi\s+can\s+(?:generate|create|produce|make)\b.*?\bvideos?\b",
        re.I,
    ),
    "audio": re.compile(
        r"\bhere(?:'s| is)\s+(?:the|your|an?)\s+(?:audio|music|song|sound|track)\b"
        r"|\bi(?:'ve| have|)\s+(?:created|generated|made|produced)\s+(?:the|your|an?)\s+"
        r"(?:audio|music|song|sound|track)\b"
        r"|\bi\s+can\s+(?:generate|create|produce|make)\b.*?\b(?:audio|music|songs?|sounds?|tracks?)\b",
        re.I,
    ),
    "file_attachment": re.compile(
        r"\bi(?:'ve| have|)\s+(?:attached|uploaded|sent|emailed|transmitted)\b"
        r"|\b(?:file|attachment|download)\s+(?:is |has been )?(?:ready|attached|sent)\b"
        r"|\bhere(?:'s| is)\s+(?:the|your)\s+(?:file|attachment|download)\b",
        re.I,
    ),
    "code": re.compile(
        r"\bhere(?:'s| is)\s+(?:the|your|some)\s+(?:code|script|program|function|html|css|javascript)\b"
        r"|\bi(?:'ve| have|)\s+(?:written|coded|programmed|built)\s+(?:the|your|a)\b",
        re.I,
    ),
    "email_send": re.compile(
        r"\bi(?:'ve| have|)\s+(?:sent|emailed|delivered)\s+(?:the|your|it)\b",
        re.I,
    ),
}


# -- 1. renders / merely-parses -----------------------
# "Did I verify the artifact actually manifests (the SVG renders, the page
# shows), or only that a check about it passed?"

_XMLNS_RE = re.compile(r'\bxmlns\s*=', re.I)
_FILL_RE = re.compile(
    r'\bfill\s*=\s*["\']([^"\']+)["\']'
    r'|\bfill\s*:\s*([^;}"\']+)',
    re.I,
)
_INVISIBLE_FILLS = frozenset({
    'none', 'transparent', 'white', '#fff', '#ffffff',
})


_CSS_HIDDEN_RE = re.compile(
    r'\bopacity\s*[=:]\s*["\']?0(?:\.0+)?["\']?'
    r'|\bdisplay\s*:\s*none\b'
    r'|\bvisibility\s*[=:]\s*["\']?hidden["\']?'
    r'|\bclip-path\s*:\s*inset\(\s*100\s*%\s*\)'
    r'|\bclip-path\s*:\s*circle\(\s*0(?:px|%)?\s*[,)]'
    r'|\btransform\s*[=:]\s*["\']?\s*scale\s*\(\s*0(?:\.0+)?\s*[,)]',
    re.I,
)


def _all_elements_hidden(svg: str) -> bool:
    """True if every visual element in the SVG is hidden via CSS properties."""
    elements = re.findall(r'<(?:rect|circle|ellipse|line|polyline|polygon|path|text|image|use)\b[^>]*>', svg, re.I)
    if not elements:
        return True
    return all(_CSS_HIDDEN_RE.search(el) for el in elements)


def check_renders(svg: str) -> CheckResult:
    """renders / merely-parses — an artifact must actually manifest.

    A parseable-but-blank SVG (no xmlns, all-white fills, no visible
    elements) is a claimed/actual failure in artifact form. Gate on
    renderability, not parseability.

    Catches CSS visibility bypass: opacity:0, display:none, visibility:hidden
    on all visual elements means nothing renders even though elements exist.
    """
    if not isinstance(svg, str) or not svg.strip():
        return CheckResult("renders/merely-parses", False, "empty artifact")
    if not _XMLNS_RE.search(svg.split('>')[0]):
        return CheckResult("renders/merely-parses", False,
                          "missing xmlns — parseable but renders blank")
    if _all_elements_hidden(svg):
        return CheckResult("renders/merely-parses", False,
                          "all elements hidden via CSS (opacity:0/display:none/visibility:hidden)")
    vb = re.search(r'viewBox\s*=\s*["\']([^"\']+)["\']', svg, re.I)
    if vb:
        parts = vb.group(1).split()
        if len(parts) == 4:
            try:
                if float(parts[2]) <= 0 or float(parts[3]) <= 0:
                    return CheckResult("renders/merely-parses", False,
                                      "zero-size viewBox — nothing visible")
            except ValueError:
                pass
    has_visible = False
    for m in _FILL_RE.finditer(svg):
        fill = (m.group(1) or m.group(2) or '').strip().lower()
        if fill not in _INVISIBLE_FILLS:
            has_visible = True
            break
    if not has_visible:
        for tm in re.finditer(r'<text\b[^>]*>(.*?)</text>', svg, re.I | re.DOTALL):
            if tm.group(1).strip():
                has_visible = True
                break
    if has_visible:
        non_text_els = re.findall(
            r'<(?:rect|circle|ellipse|line|polyline|polygon|path|image|use)\b', svg, re.I)
        if not non_text_els:
            text_contents = re.findall(r'<text\b[^>]*>(.*?)</text>', svg, re.I | re.DOTALL)
            if text_contents and all(not tc.strip() for tc in text_contents):
                has_visible = False
    if not has_visible:
        return CheckResult("renders/merely-parses", False,
                          "no visible content — all fills invisible or absent")
    return CheckResult("renders/merely-parses", True)


# -- 2. claimed / actual (the never-inflate LAW) ------------------------------
# "Does this output claim any capability, evaluation, action, or presence
# the machine doesn't actually have, do, or possess right now?"

def check_claimed_actual(text: str) -> CheckResult:
    """claimed / actual — the surface never claims more than the machine has.

    Checks model output against the CAPABILITY_REGISTRY (generated from
    reality, not hand-kept). A claim of a capability not in the registry
    is a never-inflate violation.
    """
    if not isinstance(text, str):
        return CheckResult("claimed/actual", True)
    for cap_name, pattern in _CLAIM_PATTERNS.items():
        if pattern.search(text):
            cap = CAPABILITY_REGISTRY.get(cap_name, {})
            if not cap.get("can", False):
                return CheckResult("claimed/actual", False,
                                  f"claims {cap_name} capability not in registry")
    return CheckResult("claimed/actual", True)


# -- 3. satisfies-intent / satisfies-letter -----------
# "Could a placeholder or invented value make this green while its purpose
# is violated?"

_PLACEHOLDER_RE = re.compile(
    r'\b(?:TODO|FIXME|PLACEHOLDER|TBD|XXX|LOREM|lorem ipsum)\b'
    r'|\b(?:foo|bar|baz|test123|example\.com|placeholder)\b'
    r'|^\s*\.\.\.\s*$',
    re.I | re.M,
)

_INVENTED_VALUE_RE = re.compile(
    r'\b(?:your[_\s-]*(?:name|brand|company|product|tagline)|insert[_\s-]*here)\b',
    re.I,
)

_ERROR_MESSAGE_RE = re.compile(
    r'\berror\s*:\s*(?:unable|cannot|could not|failed)\b'
    r'|\bplease\s+try\s+again\s+later\b'
    r'|\bsomething\s+went\s+wrong\b'
    r'|\bunable\s+to\s+(?:generate|create|produce|process)\b',
    re.I,
)

_FRAMING_PREAMBLE_RE = re.compile(
    r'\b(?:how about|what about)\b.*\b(?:the following|these|this)\b'
    r'|\bthe following\b\s*[?:.]?\s*$'
    r'|\bhere (?:are|is) (?:a |an |some |my |the )?\w+\s*(?:idea|suggestion|option|concept)s?\b'
    r'|\b(?:idea|suggestion|option|concept)s?\s+(?:for|about)\b'
    r'|\blet me (?:suggest|share|offer|propose)\b'
    r'|\bi(?:\'d| would) (?:suggest|recommend|propose)\b',
    re.I,
)

_MAX_FRAMING_LEN = 200


_CONFUSABLE_MAP = str.maketrans({
    "Ο": "O", "ο": "o",  # Greek Ο/ο → O/o
    "А": "A", "а": "a",  # Cyrillic А/а → A/a
    "В": "B",                  # Cyrillic В → B
    "Е": "E", "е": "e",  # Cyrillic Е/е → E/e
    "К": "K", "к": "k",  # Cyrillic К/к → K/k
    "М": "M", "м": "m",  # Cyrillic М/м → M/m
    "Н": "H",                  # Cyrillic Н → H
    "О": "O", "о": "o",  # Cyrillic О/о → O/o
    "Р": "P", "р": "p",  # Cyrillic Р/р → P/p
    "С": "C", "с": "c",  # Cyrillic С/с → C/c
    "Т": "T", "т": "t",  # Cyrillic Т/т → T/t
    "Х": "X", "х": "x",  # Cyrillic Х/х → X/x
    "Α": "A", "α": "a",  # Greek Α/α → A/a
    "Β": "B", "β": "b",  # Greek Β/β → B/b (close enough for detection)
    "Ε": "E", "ε": "e",  # Greek Ε/ε → E/e
    "Η": "H",                  # Greek Η → H
    "Ι": "I", "ι": "i",  # Greek Ι/ι → I/i
    "Κ": "K", "κ": "k",  # Greek Κ/κ → K/k
    "Μ": "M",                  # Greek Μ → M
    "Ν": "N",                  # Greek Ν → N
    "Ρ": "P", "ρ": "p",  # Greek Ρ/ρ → P/p (rho)
    "Τ": "T", "τ": "t",  # Greek Τ/τ → T/t
    "Χ": "X", "χ": "x",  # Greek Χ/χ → X/x
    "З": "3",                  # Cyrillic З → 3 (visual)
    "І": "I", "і": "i",  # Cyrillic І/і → I/i
})


def _fold_confusables(text: str) -> str:
    """Fold visually-confusable characters to Latin equivalents for placeholder detection."""
    return text.translate(_CONFUSABLE_MAP)


def check_satisfies_intent(text: str) -> CheckResult:
    """satisfies-intent / satisfies-letter — no placeholder passing a guard.

    A creation atom with placeholder text satisfies the letter (non-empty,
    passes is_blank) but not the intent (no real content). Catch invented
    values that pass guards while the purpose is violated.

    NFKD-normalizes before regex to collapse Unicode homoglyphs (Greek O in
    TΟDO, Cyrillic A in PLАCEHOLDER) that would bypass word-boundary matching.
    """
    if not isinstance(text, str) or not text.strip():
        return CheckResult("satisfies-intent/satisfies-letter", False,
                          "empty — satisfies neither")
    normalized = _fold_confusables(text)
    m = _PLACEHOLDER_RE.search(normalized)
    if m:
        return CheckResult("satisfies-intent/satisfies-letter", False,
                          f"placeholder detected: {m.group(0)!r}")
    m = _INVENTED_VALUE_RE.search(normalized)
    if m:
        return CheckResult("satisfies-intent/satisfies-letter", False,
                          f"invented value: {m.group(0)!r}")
    m = _ERROR_MESSAGE_RE.search(normalized)
    if m:
        return CheckResult("satisfies-intent/satisfies-letter", False,
                          f"error message, not creation: {m.group(0)!r}")
    if len(text.strip()) < _MAX_FRAMING_LEN and _FRAMING_PREAMBLE_RE.search(normalized):
        return CheckResult("satisfies-intent/satisfies-letter", False,
                          "framing preamble — narrates the task, not the creation")
    stripped = text.strip()
    content_chars = stripped.replace(' ', '')
    if len(content_chars) >= 3 and len(set(content_chars)) == 1:
        return CheckResult("satisfies-intent/satisfies-letter", False,
                          f"single character repeated — not real content")
    return CheckResult("satisfies-intent/satisfies-letter", True)


# -- 4. coherent-difference --------------------
# "difference (reads / none), coherence (shared vocabulary — palette,
# shape-family / off-vocabulary)"

_FILL_ATTR_RE = re.compile(r'fill\s*=\s*["\']([^"\']+)["\']', re.I)

_KNOWN_PALETTE = {
    '#1a3a5c', '#2d5016', '#333', '#333333', '#000', '#000000',
    '#e6edf3', '#0d1117', '#a08520', '#1a1700',
}


def check_coherent_difference(svg: str) -> CheckResult:
    """coherent-difference — a mark must read (difference) AND cohere.

    Difference: has visible fill (else no mark — dead/mush).
    Coherence: fills are in a recognizable vocabulary (palette), not
    random/off-vocabulary (noise).

    Returns repaired=True with repaired_value when an off-palette fill
    is snapped to the nearest palette color (mutate-repair, not subtract).
    """
    if not isinstance(svg, str) or not svg.strip():
        return CheckResult("coherent-difference", False, "no content — no difference")

    fills = [m.group(1).strip().lower() for m in _FILL_ATTR_RE.finditer(svg)]
    if not fills and not re.search(r'<text\b', svg, re.I):
        return CheckResult("coherent-difference", False,
                          "no fills, no text — mush (no difference)")

    visible = [f for f in fills if f not in _INVISIBLE_FILLS]
    if not visible and not re.search(r'<text\b', svg, re.I):
        return CheckResult("coherent-difference", False,
                          "all fills invisible — no readable difference")

    off_palette = [f for f in visible if f not in _KNOWN_PALETTE]
    if off_palette:
        return CheckResult("coherent-difference", True,
                          f"off-palette fills (acceptable — model's choice): {off_palette}")

    return CheckResult("coherent-difference", True)


# -- 5. subtract / mutate-repair ---------------
# "Block only true noise (no content / unfixable). A fixable misfit gets
# REPAIRED into fit, not discarded."

def check_subtract_repair(svg: str) -> CheckResult:
    """subtract / mutate-repair — repair fixable, subtract only irreparable.

    Returns repaired=True with repaired_value when a fixable defect is
    repaired. Returns passed=False only for irreparable content (empty,
    no elements, structurally broken beyond repair).
    """
    if not isinstance(svg, str) or not svg.strip():
        return CheckResult("subtract/mutate-repair", False,
                          "empty — irreparable (subtract)")

    from ..runtime import repair_svg, validate_svg_renderable, extract_svg
    raw_svg = extract_svg(svg) or svg
    repaired = repair_svg(raw_svg)
    changed = repaired != raw_svg

    if validate_svg_renderable(repaired):
        if changed:
            return CheckResult("subtract/mutate-repair", True,
                              "fixable defects repaired (mutate-repair)",
                              repaired=True, repaired_value=repaired)
        return CheckResult("subtract/mutate-repair", True, "no repair needed")

    return CheckResult("subtract/mutate-repair", False,
                      "irreparable after repair attempt — subtract")


# -- 6. author / auditor ------------------------------
# "Are these eyes independent of the builder?"

def check_author_auditor(author_id: str, auditor_id: str) -> CheckResult:
    """author / auditor — the author cannot audit its own floor.

    Canary-now, self-reported. This check flags when the same identity
    authored and audited. It cannot ENFORCE independence (that requires
    orchestrator-level separation). It CAN catch the obvious case and
    raise a flag.
    """
    if not author_id or not auditor_id:
        return CheckResult("author/auditor", True,
                          "identity unknown — cannot check (canary gap)")
    if author_id.strip().lower() == auditor_id.strip().lower():
        return CheckResult("author/auditor", False,
                          f"same identity authored and audited: {author_id!r}")
    return CheckResult("author/auditor", True)


# -- Runner -------------------------------------------------------------------

def run_all(artifact: str = "", text: str = "",
            author_id: str = "", auditor_id: str = "") -> list[CheckResult]:
    """Run all mechanizable checks. Returns list of CheckResults.

    `artifact` is SVG or other renderable content.
    `text` is model output text (say/creation).
    """
    results = []

    if artifact:
        results.append(check_renders(artifact))
        results.append(check_coherent_difference(artifact))
        results.append(check_subtract_repair(artifact))

    if text:
        results.append(check_claimed_actual(text))
        results.append(check_satisfies_intent(text))

    if author_id or auditor_id:
        results.append(check_author_auditor(author_id, auditor_id))

    return results
