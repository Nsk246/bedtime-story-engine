"""
checks.py - the measurable half of quality.

WHAT THIS FILE IS
    Pure functions over a string. Given the text of a story, measure the things
    that have a right answer: how long it is, how many times the refrain
    actually appears, whether any sentence is too long to read in one breath,
    whether the ending states a moral out loud.

WHAT IT IS NOT
    A judge. Nothing here has an opinion. "Is this story charming" is not a
    question this file can answer, and it doesn't try.

WHY IT EXISTS
    The first version of this project asked the LLM judge for these numbers and
    it got them wrong most of the time. It would report three refrains in a
    draft that used two, and the reviser would act on that. Anything countable
    is now counted here, and the results are handed to the judge as fact.

    The rule this file exists to enforce: count in code, judge with the model.

    It imports nothing from the rest of the project on purpose. You can read it
    in two minutes and test it without an API key.

READ NEXT
    pipeline.py, StoryEngine.evaluate - where these results get used.
"""

from __future__ import annotations

import re
from typing import Any

# A 6-year-old loses the thread somewhere past this many words in one breath.
MAX_SENTENCE_WORDS = 28

# Length is a guide, not a rule.
#
# A good 350-word bedtime story beats a padded 500-word one, and that isn't a
# guess: when length was a hard gate, the reviser satisfied it by bolting extra
# scenes onto the end. One story came back at 688 words with three endings and
# was plainly worse than the 382-word version of itself. Optimising the number
# cost us the thing the number was standing in for.
#
# So the tolerated band is wide, and only genuinely malformed lengths - a story
# less than half its target, or half again over - count as a defect. Everything
# in between is reported and left alone.
TOO_LONG = 1.50
TOO_SHORT = 0.60
BADLY_SHORT = 0.45

# The refrain should recur and shouldn't be hammered. "Exactly three" was my
# own invention, and across three rounds of real runs the model landed it about
# half the time - it kept producing 2 or 4, which read perfectly well, and 7,
# which did not. So the ideal stays 3 and the acceptable range is 2 to 4. The
# check now enforces what actually matters (it recurs, it isn't overused)
# instead of a number I picked.
REFRAIN_IDEAL = 3
REFRAIN_MIN = 2
REFRAIN_MAX = 4

# gpt-3.5's favourite way to ruin an ending. The style bible bans this, and it
# still slips through every few drafts, so we also grep for it.
PREACHY = re.compile(
    r"\b("
    r"the (moral|lesson) of"
    r"|\w+ (learned|realised|realized|understood) that"          # "Lily learned that..."
    r"|the (real|true|greatest) (treasure|lesson|magic|gift|adventure) (is|was|are|were)"
    r"|which (just )?goes to show"
    r"|remember(,| that)? (always|the )"
    r"|knew that .{0,50}(could come true|would always)"
    r")\b",
    re.I,
)

_PUNCT = re.compile(r"[^\w\s]")
_ARTICLE = re.compile(r"\b(the|a|an) ", re.I)
_SENTENCE_END = re.compile(r"[.!?]+[\s\"']*")


def _flatten(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    So that 'Slow paws, brave heart.' and 'slow paws brave heart' count as the
    same line. Without this the refrain count is wrong whenever the writer ends
    a repetition with an exclamation mark instead of a full stop.
    """
    return " ".join(_PUNCT.sub("", text.lower()).split())


def measure(
    body: str,
    target_words: int,
    refrain: str,
    beat_names: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Measure one draft. Keys starting with '_' are for building fix messages
    and are stripped before the judge sees them (see facts_for_judge)."""
    words = len(body.split())

    flat_refrain = _flatten(refrain)
    refrain_hits = _flatten(body).count(flat_refrain) if flat_refrain else 0

    sentences = [s for s in _SENTENCE_END.split(body) if s.strip()]
    over_limit = [s.strip() for s in sentences if len(s.split()) > MAX_SENTENCE_WORDS]

    paragraphs = [p for p in body.strip().split("\n\n") if p.strip()]
    preachy = PREACHY.search(body)

    # Beat labels are scaffolding. "The ridiculous problem presented itself"
    # appeared verbatim in a real run, so check for it rather than trusting the
    # instruction not to.
    flat_body = _flatten(body)
    # Match with the leading article stripped from both sides. A real run wrote
    # "a ridiculous problem arose" against the label "The ridiculous problem",
    # and an exact match sailed straight past it.
    leaked = ""
    for name in beat_names:
        stem = _ARTICLE.sub("", _flatten(name)).strip()
        if len(stem.split()) >= 2 and stem in _ARTICLE.sub("", flat_body):
            leaked = name
            break

    # A story should land on its closing image, not on a chant. Two real runs
    # ended with the refrain as the final line.
    ends_on_refrain = bool(
        flat_refrain and paragraphs and _flatten(paragraphs[-1]) == flat_refrain
    )

    # The refrain is *meant* to repeat, so it can never be an accidental
    # duplicate. Without this, a refrain of eight words or more standing as its
    # own paragraph got flagged as a defect for doing its job.
    seen: dict[str, int] = {}
    duplicated = ""
    for para in paragraphs:
        key = _flatten(para)
        if key == flat_refrain:
            continue
        if len(key.split()) >= 8:
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1 and not duplicated:
                duplicated = para.strip()[:120]

    # The refrain is meant to recur at rising moments, spread through the story.
    # Told to "add exactly 2 more", the reviser twice satisfied the count by
    # stacking three identical lines at the end - the right number, in the
    # wrong place, which is worse than being one short. Counting alone can be
    # gamed, so check placement too.
    refrain_paras = [i for i, p in enumerate(paragraphs) if _flatten(p) == flat_refrain]
    stacked = any(b - a == 1 for a, b in zip(refrain_paras, refrain_paras[1:]))

    return {
        "word_count": words,
        "target_words": target_words,
        "length_verdict": (
            "far too long"
            if words > target_words * TOO_LONG
            else "far too short"
            if words < target_words * BADLY_SHORT
            else "a little short"
            if words < target_words * TOO_SHORT
            else "ok"
        ),
        "refrain_used": refrain_hits,
        "refrain_expected": REFRAIN_IDEAL,
        "refrain_ok": REFRAIN_MIN <= refrain_hits <= REFRAIN_MAX,
        "longest_sentence_words": max((len(s.split()) for s in sentences), default=0),
        "sentences_over_limit": len(over_limit),
        "ends_on_refrain": ends_on_refrain,
        "repeats_a_paragraph": bool(duplicated),
        "refrain_stacked": stacked,
        "_long_example": over_limit[0][:120] if over_limit else "",
        "_preachy": preachy.group(0) if preachy else "",
        "_beat_leak": leaked,
        "_duplicate": duplicated,
    }


def to_fixes(checks: dict[str, Any]) -> list[dict[str, str]]:
    """Turn failed measurements into the same {issue, quote, fix} shape the
    judge emits, so the reviser reads one uniform list."""
    fixes: list[dict[str, str]] = []

    if checks["_preachy"]:
        fixes.append(
            {
                "issue": "the story states its moral out loud instead of showing it",
                "quote": checks["_preachy"],
                "fix": "delete that sentence; let the ending carry the meaning",
            }
        )

    used = checks["refrain_used"]
    if not checks.get("refrain_ok", True):
        short = used < REFRAIN_MIN
        fixes.append(
            {
                "issue": f"the refrain appears {used} times, it should appear "
                f"{REFRAIN_MIN} to {REFRAIN_MAX} times",
                "quote": "",
                "fix": (
                    f"add exactly {REFRAIN_IDEAL - used} more, word for word, at "
                    f"a rising moment"
                    if short
                    else f"delete exactly {used - REFRAIN_IDEAL} of them, keeping "
                    f"the {REFRAIN_IDEAL} strongest. Do not add any."
                ),
            }
        )

    if checks["length_verdict"] != "ok":
        too_long = "long" in checks["length_verdict"]
        fixes.append(
            {
                "issue": f"the draft is {checks['length_verdict']} "
                f"({checks['word_count']} words against a target of {checks['target_words']})",
                "quote": "",
                "fix": (
                    "cut from the middle beats, never the ending"
                    if too_long
                    else f"expand the middle beats to reach about "
                    f"{checks['target_words']} words: more dialogue, more of "
                    f"what the characters noticed and did"
                ),
            }
        )

    if checks.get("refrain_stacked"):
        fixes.append(
            {
                "issue": "the refrain is repeated back to back instead of spread through the story",
                "quote": "",
                "fix": "keep one of them where it is and move the others to the "
                "end of an earlier scene, where the story is rising",
            }
        )

    if checks.get("_duplicate"):
        fixes.append(
            {
                "issue": "a whole paragraph appears twice, word for word",
                "quote": checks["_duplicate"],
                "fix": "delete the second copy",
            }
        )

    if checks["sentences_over_limit"]:
        fixes.append(
            {
                "issue": f"{checks['sentences_over_limit']} sentence(s) run past "
                f"{MAX_SENTENCE_WORDS} words, too long to read aloud in one breath",
                "quote": checks["_long_example"],
                "fix": "split it into two or three short sentences",
            }
        )

    return fixes


def has_hard_failure(checks: dict[str, Any]) -> bool:
    """Objective misses. These hold the quality gate shut whatever the score is.

    Sentence length is deliberately absent: one long sentence is a note for the
    reviser, not grounds to block a story that is otherwise lovely.
    """
    return bool(
        checks["_preachy"]
        or not checks.get("refrain_ok", True)
        or checks["length_verdict"].startswith("far")
        or checks.get("repeats_a_paragraph")
        or checks.get("refrain_stacked")
    )


def facts_for_judge(checks: dict[str, Any]) -> dict[str, Any]:
    """The measurements, minus the private keys used only for fix messages."""
    return {k: v for k, v in checks.items() if not k.startswith("_")}


# --------------------------------------------------------------------------
# Score ceilings.
#
# These started life as instructions in the judge's prompt ("if the draft is
# too short, story_arc scores 3 at most"). A real run handed the judge a
# 308-word draft against a 600-word target, with the measurement right there in
# its prompt, and it awarded 5s across the board anyway. The rule was correct;
# putting it somewhere it could be ignored was not.
#
# Same principle as the rest of this file, one level up: a rule that depends on
# a measurement belongs in code. The judge is left to score what it is actually
# good at, and arithmetic consequences are applied afterwards.
# --------------------------------------------------------------------------

CEILINGS: list[tuple[str, tuple[str, ...], int, str]] = [
    ("length_bad", ("story_arc", "heart"), 3,
     "beats told in one sentence have been listed, not told"),
    ("refrain_bad", ("read_aloud",), 3, "the refrain count is wrong"),
    ("preachy", ("bedtime_landing", "heart"), 2, "the story states its moral out loud"),
    ("beat_leak", ("story_arc",), 2, "a beat label leaked into the prose"),
    ("ends_on_refrain", ("bedtime_landing",), 3, "it ends on the chant, not the closing image"),
]


def _triggered(name: str, checks: dict[str, Any]) -> bool:
    if name == "length_bad":
        # Only a badly malformed length caps a score. A story that runs a
        # little short can still have a complete arc, and usually does.
        return checks["length_verdict"].startswith("far")
    if name == "refrain_bad":
        return not checks.get("refrain_ok", True)
    if name == "preachy":
        return bool(checks["_preachy"])
    if name == "beat_leak":
        return bool(checks["_beat_leak"])
    return bool(checks.get("ends_on_refrain"))


def apply_ceilings(
    scores: dict[str, int], checks: dict[str, Any]
) -> tuple[dict[str, int], list[str]]:
    """Cap scores the measurements have already disproved.

    Returns the adjusted scores and a note for each cap that actually bit, so
    the run output can say why a score moved rather than silently changing it.
    """
    capped = dict(scores)
    notes: list[str] = []
    for name, dimensions, ceiling, reason in CEILINGS:
        if not _triggered(name, checks):
            continue
        hit = [d for d in dimensions if capped.get(d, 0) > ceiling]
        if hit:
            for d in hit:
                capped[d] = ceiling
            notes.append(f"{'/'.join(hit)} capped at {ceiling}: {reason}")
    return capped, notes
