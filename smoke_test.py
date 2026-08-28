"""
smoke_test.py - runs the whole graph against a stubbed model. No API key, no
network, about a second.

I wrote this because the two rules that are easiest to get wrong (revise from
the best draft, ship the best draft) are also the two you cannot see by reading
one run's output. The fake judge below deliberately scores the third draft
*worse* than the second, which is the case that broke an earlier version.

    python smoke_test.py
"""

from __future__ import annotations

import json
import os
import types

os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-real")

from llm import StoryLLM, _loads
from checks import apply_ceilings, has_hard_failure, measure, to_fixes
from pipeline import Draft, StoryEngine, split_title, weighted_score
from prompts import RUBRIC_WEIGHTS, get_strategy

DIMS = list(RUBRIC_WEIGHTS)

STORY = (
    "The Lantern Moth\n\n"
    'Alice held the jar. "Slow paws, brave heart," she whispered.\n\n'
    "Bob purred at the glass.\n\n"
    "Slow paws, brave heart.\n\n"
    "They slept."
)

BRIEF = {
    "strategy": "brave_but_cozy",
    "characters": [{"name": "Alice", "who": "a girl with a jar"}],
    "setting": "a garden at dusk",
    "must_include": ["Bob the cat"],
    "energy": "low",
    "needs_softening": True,
    "softened_note": "dropped the wolf's teeth",
    "clean_request": "Alice and her cat Bob meet a shy moth in the dark.",
}

PLAN = {
    "title": "The Lantern Moth",
    "logline": "Alice wants to see the glow.",
    "refrain": "Slow paws, brave heart.",
    "beats": [{"beat": f"beat {i}", "what_happens": "something happens"} for i in range(7)],
    "sensory_anchor": "warm jar glass",
    "landing_image": "a moth asleep on the sill",
}

# draft 1 mediocre, draft 2 strong, draft 3 worse than draft 2.
JUDGE_SCORES = [3, 5, 2]


def reply(text: str):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text))],
        usage=types.SimpleNamespace(prompt_tokens=100, completion_tokens=50),
    )


class FakeClient:
    """Replays canned responses, picked by which system prompt is calling."""

    def __init__(self):
        self.judge_calls = 0
        self.revise_inputs = []  # the draft text handed to each revision
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create)
        )

    def _create(self, **kw):
        system = kw["messages"][0]["content"]
        user = kw["messages"][1]["content"]

        if "intake desk" in system:
            return reply(json.dumps(BRIEF))

        if "story architect" in system:
            # fenced, and only 5 beats, to exercise cleanup and padding
            return reply("```json\n" + json.dumps(dict(PLAN, beats=PLAN["beats"][:5])) + "\n```")

        if "bedtime storyteller" in system:
            return reply(STORY)

        if "picky editor" in system:
            n = JUDGE_SCORES[min(self.judge_calls, len(JUDGE_SCORES) - 1)]
            self.judge_calls += 1
            return reply(
                json.dumps(
                    {
                        "scores": {d: n for d in DIMS},
                        "one_line_verdict": "fine",
                        "safety_flags": [],
                        "what_works": ["the jar"],
                        "must_fix": [
                            {"issue": "flat ending", "quote": "They slept.", "fix": "slow it down"}
                        ],
                    }
                )
            )

        if "You are Maya" in system:
            return reply(
                json.dumps(
                    {
                        "favourite_part": "the moth",
                        "confusing_bit": "",
                        "boring_bit": "the middle",
                        "too_scary": "",
                        "sleepy_at_the_end": False,
                        "one_wish": "more cat",
                        "again_tomorrow": True,
                    }
                )
            )

        if "revision editor" in system:
            self.revise_inputs.append(user)
            return reply(STORY.replace("They slept.", "They slept, warm and slow."))

        if "fix malformed JSON" in system:
            return reply('{"repaired": true}')

        return reply("...")


def check(label, condition):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    assert condition, label


def main():
    print("\nparsing")
    t, b = split_title('Title: "The Moth"\n\nOnce upon a time.')
    check("title label and quotes stripped", t == "The Moth" and b == "Once upon a time.")
    check("body-only input survives", split_title("just one line")[1] == "just one line")
    check("fenced json parses", _loads('```json\n{"a":1}\n```') == {"a": 1})
    check("json wrapped in chatter parses", _loads('Sure!\n{"a":{"b":2}}\nhope that helps') == {"a": {"b": 2}})
    check("braces inside strings survive", _loads('{"a":"}{"}') == {"a": "}{"})
    check("garbage returns None", _loads("no json here") is None)

    print("\nscoring")
    check("all fives weight to 5.0", weighted_score({d: 5 for d in DIMS}) == 5.0)
    check("empty scores default to 3.0", weighted_score({}) == 3.0)
    check("a non-dict does not explode", weighted_score(["nope"]) == 3.0)

    print("\nhard checks")
    bad = Draft(
        round=1,
        title="T",
        body=(
            'Alice held the jar. "Slow paws, brave heart," she said.\n\n'
            "She walked " + "and walked " * 15 + "past the shed.\n\n"
            "And she learned that being brave is good."
        ),
    )
    c = measure(bad.body, get_strategy("brave_but_cozy").target_words, "Slow paws, brave heart.")
    issues = " | ".join(f["issue"] for f in to_fixes(c))
    check("stated moral caught", "moral" in issues)
    check("refrain out of range caught",
          c["refrain_used"] == 1 and c["refrain_ok"] is False and "refrain appears" in issues)
    check("over-long sentence caught", c["sentences_over_limit"] == 1)
    check("badly short draft caught", c["length_verdict"] == "far too short")
    check("gate held shut by measurement alone", has_hard_failure(c) is True)

    clean = Draft(round=1, title="T", body="")
    clean.body = "word " * 400 + ("Slow paws, brave heart. " * 3) + "the end."
    check(
        "a compliant draft passes the checks",
        has_hard_failure(
            measure(clean.body, get_strategy("brave_but_cozy").target_words, "Slow paws, brave heart.")
        )
        is False,
    )

    print("\nscore ceilings")
    perfect = {d: 5 for d in DIMS}
    capped, notes = apply_ceilings(perfect, c)
    check("short draft caps story_arc and heart",
          capped["story_arc"] == 3 and capped["heart"] <= 3)
    check("refrain out of range caps read_aloud at 3", capped["read_aloud"] == 3)
    check("a cap explains itself", any("listed, not told" in n for n in notes))
    leak = measure(
        "The ridiculous problem presented itself, and the pole was high. " * 40,
        600, "none", ("The ridiculous problem", "The wobble"),
    )
    check("leaked beat label detected", leak["_beat_leak"] == "The ridiculous problem")
    article = measure(
        "a ridiculous problem arose and the fish were burning brightly. " * 20,
        450, "none", ("The ridiculous problem", "The wobble"),
    )
    check("leak caught despite a swapped article",
          article["_beat_leak"] == "The ridiculous problem")
    dupe = measure(
        "The wolf and the sheep nestled together under the moonlit sky.\n\n"
        "Something quite different happened in the middle of the meadow.\n\n"
        "The wolf and the sheep nestled together under the moonlit sky.",
        60, "none",
    )
    check("repeated paragraph detected", dupe["repeats_a_paragraph"] is True)
    check("repeated paragraph is a defect", has_hard_failure({**dupe, "refrain_ok": True}) is True)
    # The reviser twice hit "add 2 more refrains" by stacking them at the end.
    # Right count, wrong place, and worse than being one short.
    stacked = measure(
        ("filler words here for length. " * 40) + "\n\nBrave and small.\n\n"
        "Brave and small.\n\nBrave and small.",
        250, "Brave and small.",
    )
    check("refrain stacked back to back is caught", stacked["refrain_stacked"] is True)
    check("stacked refrain is a defect despite the right count",
          stacked["refrain_used"] == 3 and has_hard_failure(stacked) is True)
    check("leak caps story_arc at 2", apply_ceilings(perfect, leak)[0]["story_arc"] == 2)
    ends = measure("A story happened here.\n\nSlow paws, brave heart.", 12, "Slow paws, brave heart.")
    check("ending on the chant is detected", ends["ends_on_refrain"] is True)
    # 280/500 = 56%: inside the advisory band, outside the defect band.
    mild = measure("word " * 280, 500, "the line")
    check("a story running a little short is not a defect",
          mild["length_verdict"] == "a little short" and not has_hard_failure(
              {**mild, "refrain_used": 3, "refrain_ok": True}))
    check("a little short does not cap any score",
          apply_ceilings(perfect, {**mild, "refrain_used": 3, "refrain_ok": True})[1] == [])

    check("clean text passes all ceilings",
          apply_ceilings(perfect, measure("word " * 420 + "Slow paws, brave heart. " * 3 + "the end.",
                                          500, "Slow paws, brave heart."))[1] == [])

    print("\nfull run (stubbed model)")
    engine = StoryEngine(StoryLLM(api_key="sk-test"), verbose=True)
    fake = FakeClient()
    engine.llm._client = fake
    final = engine.tell("a story about Alice and her cat Bob in the dark")

    print("\nthe rules that are easy to get wrong")
    check("router picked the spooky strategy", engine.strategy.key == "brave_but_cozy")
    check("short beat sheet padded to 7", len(engine.plan["beats"]) == 7)
    check("loop ran both rounds", len(engine.drafts) == 3)
    check("draft 3 scored worse than draft 2", engine.drafts[2].overall < engine.drafts[1].overall)
    # Deliberately not asserting an exact number here: the score ceilings apply
    # to the stub's text too, so the value moves whenever those rules change.
    # The rule under test is which draft ships, not what it scored.
    check("champion ships, not the last draft", final.round == 2)
    check("champion outscores every other draft",
          all(final.overall >= d.overall for d in engine.drafts))
    check(
        "second revision started from the champion, not the worse draft",
        "warm and slow" in fake.revise_inputs[1],
    )
    check("measured fixes reached the reviser",
          "far too short" in fake.revise_inputs[0] or "words against" in fake.revise_inputs[0])

    before = len(engine.drafts)
    engine.apply_feedback(final, "make Bob talk more")
    check("family feedback adds a graded draft", len(engine.drafts) == before + 1)

    path = engine.save_run("runs")
    saved = json.loads(path.read_text())
    check("every call landed in the trace", len(saved["trace"]) == engine.llm.usage.calls)
    check("checks persisted for later inspection", "refrain_used" in saved["drafts"][0]["checks"])
    path.unlink()

    print("\nscorecard\n" + engine.scorecard())
    print(f"\n{engine.llm.usage.calls} stubbed calls, all checks passed\n")


if __name__ == "__main__":
    main()
