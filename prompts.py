"""
prompts.py - everything the models are told. This is the file to read first.

WHAT THIS FILE IS
    Every word sent to gpt-3.5-turbo, and the six generation strategies. The
    prompts are the product here, so they live apart from the control flow
    where they can be read and tuned without touching pipeline code.

WHAT IT IS NOT
    Orchestration. Nothing in here calls a model or decides what runs next.
    Every function returns a plain (system, user) pair of strings.

HOW IT IS ORGANISED
    READ_ALOUD_STYLE   the style bible, shared by writer, judge and reviser
    STRATEGIES         six request categories, each with its own seven-beat arc
    RUBRIC_WEIGHTS     how the seven judge dimensions are weighted
    *_messages()       one function per stage, in pipeline order

THREE IDEAS DRIVE THE DESIGN
    1. One shared spec. Writer, judge and reviser are handed the same style
       bible. A judge grading against a spec the writer never saw produces
       critique nobody can act on.
    2. Structure before prose. Cheap JSON calls (classify, plan, judge) surround
       one expensive prose call. 3.5 is far better at executing a plan than at
       inventing and executing one in a single breath.
    3. Critique must be actionable. The judge never rewrites, and every fix has
       to quote the phrase it refers to.

READ NEXT
    pipeline.py to see when each of these is called.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

JSON_ONLY = (
    "Return ONLY a single valid JSON object. No markdown fences, no commentary "
    "before or after, no trailing commas. Every string must be on one line."
)


# --------------------------------------------------------------------------
# The style bible. Shared by writer, judge and reviser so all three agree on
# what "good" means.
#
# Most of these rules exist because 3.5 broke them. Left alone it writes 30-word
# sentences, ends on "and they couldn't wait for tomorrow's adventure!", and
# tacks a stated moral onto the last paragraph roughly every other draft. The
# banned-behaviour lines are the specific failures, written down.
# --------------------------------------------------------------------------

READ_ALOUD_STYLE = """\
AUDIENCE AND VOICE
- One adult reading aloud to a child aged 5-10, in a dim room, at the end of the day.
- Warm narrator, past tense, third person, unless the request clearly asks otherwise.
- Write to be heard, not read. The ear is the only editor that matters here.

SENTENCES AND WORDS
- Most sentences 8-16 words. Vary the rhythm. A three-word sentence is a gift.
- Never go past about 25 words in one sentence. No semicolons.
- Everyday vocabulary, plus at most three "stretch" words - and make each one
  obvious from the sentence around it ("the lantern guttered, shrinking down to
  a small orange bead").
- Concrete over abstract: what a child could see, hear, smell or touch.
  "The bread smelled like Sunday" beats "there was a pleasant aroma".

STRUCTURE
- Follow the beat sheet in order. Two or three short paragraphs per beat.
- The beat names are scaffolding for you alone. Never write one into the prose.
  A sentence like "The ridiculous problem presented itself" means you have
  copied the scaffolding into the story, and the draft is ruined.
- Dialogue in nearly every beat. Children hold on to voices.
- Use the supplied REFRAIN three times, word for word, at rising moments. It is
  the line a child can say along with the reader.
- Show the meaning through what characters do. Never state a moral. A sentence
  like "and she learned that friendship matters" is a failure of the whole draft.

THE FEAR BUDGET
- Trouble is a wobble, not a threat: lost mittens, a stuck door, a friend who
  will not share, a shortcut that turned out longer.
- Nothing frightening drawn from real life: no violence, injury, death, illness,
  missing or absent caregivers, fire, intruders, or "you are all alone now".
- Mild suspense is allowed only in the middle beats and must be fully resolved
  before the last two. Anything with teeth turns out to be shy, small or lonely.
- No potty humour, no romance, no brand names, no jokes only an adult would get.

THE LANDING (the part that matters most)
- The last 80-120 words must slow down on purpose. Shorter sentences. Softer
  sounds. Characters getting warm, getting still, getting sleepy.
- Close on a settled image, and if it fits, one gentle wondering to drift off
  on. Never a cliffhanger, a new problem, or a question that makes a child sit up.
- The last line should feel like a hand smoothing a blanket.

FORMAT
- Line 1: the title alone. No quotation marks, no "Title:" label.
- Line 2: blank.
- Then the story in plain paragraphs separated by blank lines.
- No headings, no beat labels, no bullets, no author's note, no emoji.
"""


# --------------------------------------------------------------------------
# Six request categories, each with its own arc, tone and length. This is the
# "tailored generation strategy per category" idea: a giggly story and a
# lullaby need different shapes, not the same shape with a different adjective.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Strategy:
    key: str
    label: str
    when: str  # shown to the classifier
    arc: tuple[str, ...]  # seven named beats
    tone: str
    target_words: int
    extras: str


STRATEGIES: dict[str, Strategy] = {
    "gentle_adventure": Strategy(
        key="gentle_adventure",
        label="Gentle adventure",
        when="quests, journeys, pirates, dragons, treasure, exploring, magic doors",
        arc=(
            "The cosy beginning",
            "The invitation",
            "The first step out",
            "The wobble",
            "The clever turn",
            "The way home",
            "The sleepy landing",
        ),
        tone="Bright and brave, but never breathless. Wonder over danger.",
        target_words=500,
        extras=(
            "The wobble must be solved by something the hero notices, makes or "
            "asks for - never by luck and never by an adult arriving to fix it."
        ),
    ),
    "silly_and_funny": Strategy(
        key="silly_and_funny",
        label="Silly and funny",
        when="jokes, nonsense, animals behaving badly, mix-ups, anything 'funny'",
        arc=(
            "A perfectly normal day",
            "The ridiculous problem",
            "The sillier fix",
            "The silliest fix",
            "Everything goes sideways",
            "The small true solution",
            "The giggly landing",
        ),
        tone="Deadpan narrator, absurd events. The narrator never winks.",
        target_words=450,
        extras=(
            "Escalate in threes. Each fix must cause the next problem. Land the "
            "biggest laugh at beat 5, then let the energy drain out on purpose so "
            "the ending is still calm enough for sleep."
        ),
    ),
    "friendship_and_feelings": Strategy(
        key="friendship_and_feelings",
        label="Friendship and feelings",
        when="arguments, sharing, jealousy, shyness, missing someone, new siblings, first days",
        arc=(
            "Two together",
            "The small hurt",
            "The distance",
            "The feeling gets a name",
            "The brave repair",
            "Better than before",
            "The sleepy landing",
        ),
        tone="Tender and plain-spoken. Big feelings in small words.",
        target_words=500,
        extras=(
            "Name the feeling out loud in dialogue once ('I think I felt left "
            "out'). Both characters must be a little bit right. No apology speech "
            "longer than two sentences."
        ),
    ),
    "curious_world": Strategy(
        key="curious_world",
        label="Curious world",
        when="space, oceans, dinosaurs, weather, machines, 'why does...', 'how do...'",
        arc=(
            "A big wondering",
            "The guide appears",
            "The first discovery",
            "The puzzling bit",
            "The aha",
            "Sharing what was found",
            "The sleepy landing",
        ),
        tone="Awed and precise. The world is stranger than any invention.",
        target_words=500,
        extras=(
            "Weave in exactly two TRUE facts, stated the way a child would "
            "repeat them, never as a lecture. The explanation at the heart of "
            "the story must be really true - a real run explained that the sea "
            "is salty because of tears shed by sea creatures, which is charming "
            "and false, and a child may repeat it at school tomorrow. If you are "
            "not certain, use the simplest accurate version, or pick a different "
            "wonder you do know."
        ),
    ),
    "brave_but_cozy": Strategy(
        key="brave_but_cozy",
        label="Brave but cosy",
        when="monsters, the dark, shadows, strange noises, ghosts, 'a bit scary'",
        arc=(
            "Something is out there",
            "The small brave step",
            "The closer look",
            "It is not what it seemed",
            "The kindness",
            "A new friend in the dark",
            "The sleepy landing",
        ),
        tone="Hushed, then warm. Suspense that unclenches.",
        target_words=450,
        extras=(
            "The frightening thing must turn out to be lonely, lost or very small "
            "by beat 4 at the latest. Never describe teeth, claws, blood or "
            "chasing. The dark itself should end up feeling friendly."
        ),
    ),
    "dreamy_lullaby": Strategy(
        key="dreamy_lullaby",
        label="Dreamy lullaby",
        when="'something calm', clouds, stars, the moon, sleep itself, no plot requested",
        arc=(
            "The softest start",
            "A gentle drift",
            "The quiet companion",
            "One small wonder",
            "Slower and softer",
            "Everything settles",
            "The sleepy landing",
        ),
        tone="Nearly a poem. Almost no conflict. Images doing the work.",
        target_words=350,
        extras=(
            "Repetition and soft consonants throughout. Nothing is ever at stake. "
            "Each beat should be quieter than the one before it."
        ),
    ),
}

DEFAULT_STRATEGY = "gentle_adventure"


def strategy_menu() -> str:
    return "\n".join(f"- {s.key}: {s.when}" for s in STRATEGIES.values())


def get_strategy(key: str) -> Strategy:
    return STRATEGIES.get((key or "").strip().lower(), STRATEGIES[DEFAULT_STRATEGY])


# --------------------------------------------------------------------------
# Stage 2 - Intake and safety router
# --------------------------------------------------------------------------


def classifier_messages(request: str) -> tuple[str, str]:
    system = f"""\
You are the intake desk of a bedtime story service for children aged 5 to 10.
You do not write stories. You turn a rough request into a clean brief and pick
the generation strategy that fits it.

STRATEGIES
{strategy_menu()}

SAFETY
A request needs softening if it asks for violence, death, real-world danger,
horror, romance, adult themes, or anything that would leave a small child awake.
Never refuse. Soften instead: keep whatever the child actually loves about the
idea (the pirate, the wolf, the storm) and drop the part that would frighten
them. Record what you changed.

{JSON_ONLY}

Schema:
{{
  "strategy": "<one strategy key from the list>",
  "why_this_strategy": "<one short sentence>",
  "characters": [{{"name": "<name or 'the fox'>", "who": "<five words>"}}],
  "setting": "<where and when, one line>",
  "must_include": ["<concrete thing the child asked for>"],
  "energy": "low" | "medium" | "high",
  "needs_softening": true | false,
  "softened_note": "<what you changed and why, or empty string>",
  "clean_request": "<the request rewritten in one warm sentence, safe to build on>"
}}

Keep every name exactly as the child spelled it. If no character is named,
invent one that fits and say so in "who"."""
    user = f'Story request from the child:\n"""{request.strip()}"""'
    return system, user


# --------------------------------------------------------------------------
# Stage 3 - Story architect (beat sheet)
# --------------------------------------------------------------------------


def architect_messages(brief: dict[str, Any], strategy: Strategy) -> tuple[str, str]:
    beats = "\n".join(f"{i + 1}. {b}" for i, b in enumerate(strategy.arc))

    # Buried in the tone notes, this got ignored: two separate runs planned a
    # story explaining that the sea is salty because sea creatures cried into
    # it. Charming, false, and a child repeats it at school. It needs its own
    # block, at the top, for the one category that makes factual claims.
    truth_block = ""
    if strategy.key == "curious_world":
        truth_block = """
BEFORE ANYTHING ELSE - THIS STORY EXPLAINS SOMETHING REAL.
The explanation at the centre of the plot must be actually true. Decide the true
answer first, in one plain sentence, and build the beats around discovering it.
A poetic mechanism that is false - tears making the sea salty, the moon being a
hole in the sky - fails this story completely, no matter how pretty it is. If
you are not confident of the real answer, choose a different wonder you do know.
"""
    system = f"""\
You are a picture-book story architect. You plan; you never write prose.
{truth_block}
You are planning a "{strategy.label}" story.
Tone: {strategy.tone}
Special rule for this kind of story: {strategy.extras}

THE SEVEN BEATS you must fill:
{beats}

A good beat is one sentence of cause and effect, not a mood. "Alice knocks and
nobody answers, so she tries the window" is a beat. "Alice feels nervous" is not.

Each beat also needs a concrete DETAIL and a line of DIALOGUE. This is not
decoration: the writer turns each beat into two or three paragraphs, and with
only a single sentence to work from it produces one thin paragraph and the whole
story comes out half length. Give it enough to build with.
- detail: something seen, heard, smelled or touched at that moment. Specific.
  "the jar lid was warm on one side" - not "it was a nice evening".
- dialogue: one short line someone actually says, in their voice.

The REFRAIN is a short chantable line, 4-9 words, that a child could say along
with the reader. It should mean something slightly different each time it lands.
Good: "Slow paws, brave heart." Bad: "What an adventure this is!"

{JSON_ONLY}

Schema:
{{
  "title": "<5 words max, concrete and a little strange, no colon>",
  "logline": "<one sentence: who wants what, and what is in the way>",
  "refrain": "<the chantable line>",
  "beats": [
    {{"beat": "<beat name, copied from the list>",
      "what_happens": "<one sentence of cause and effect>",
      "detail": "<one concrete sensory detail at this moment>",
      "dialogue": "<one short line of speech>"}}
  ],
  "sensory_anchor": "<one smell, sound or texture that recurs>",
  "landing_image": "<the final settled picture the story closes on>",
  "true_explanation": "<for a curious-world story: the real answer in one plain sentence. Empty string otherwise>"
}}

Exactly seven beats, in the given order, using the given names."""
    user = f"""\
Brief:
- request: {brief.get('clean_request')}
- characters: {brief.get('characters')}
- setting: {brief.get('setting')}
- must include: {brief.get('must_include')}
- energy: {brief.get('energy')}

Plan the story."""
    return system, user


# --------------------------------------------------------------------------
# Stage 4 - Storyteller
# --------------------------------------------------------------------------


def storyteller_messages(
    brief: dict[str, Any], strategy: Strategy, plan: dict[str, Any]
) -> tuple[str, str]:
    system = f"""\
You are a bedtime storyteller with twenty years of practice reading aloud to
five-year-olds. You know exactly which sentences make a child sit up and which
ones make their eyes get heavy.

{READ_ALOUD_STYLE}

THIS STORY
Kind: {strategy.label}
Tone: {strategy.tone}
Extra rule: {strategy.extras}

LENGTH
Aim for roughly {strategy.target_words} words - about
{strategy.target_words // 7} per beat, which is two or three short paragraphs
each rather than one thin sentence. This is a guide, not a quota: a beat told
in a single line has been listed rather than told, and that is the thing to
avoid. Never pad to reach a number. A story that is genuinely finished at 400
words is finished, and stretching it would only spoil it.

Write the story now. Nothing else."""

    # The writer does not get the beat names, only what happens in them.
    #
    # It used to get "4. The wobble: the map blurs", and it kept using the label
    # as the topic sentence of the paragraph: "a ridiculous problem arose",
    # "as the sleepy landing comes". Banning that in the prompt helped a little.
    # Not showing it the labels at all fixes it outright - you cannot leak
    # scaffolding you were never handed. The architect still needs the names to
    # build a shaped plan; the writer only needs the events.
    beat_lines = "\n\n".join(
        "\n".join(
            part
            for part in (
                f"{i + 1}. {b.get('what_happens')}",
                f"   detail to use: {b.get('detail')}" if b.get("detail") else "",
                f"   someone says: {b.get('dialogue')}" if b.get("dialogue") else "",
            )
            if part
        )
        for i, b in enumerate(plan.get("beats", []))
    )
    user = f"""\
TITLE: {plan.get('title')}
LOGLINE: {plan.get('logline')}

REFRAIN: {plan.get('refrain')}
Use it word for word exactly three times, and nowhere else: once at the end of
beat 2, once inside beat 4, once near the end of beat 6. Three. Not two, not
five. Count them before you finish.

{("THE TRUE EXPLANATION - the story must say this and must not invent an alternative: " + str(plan.get("true_explanation")) + chr(10)) if plan.get("true_explanation") else ""}SENSORY ANCHOR (bring it back at least twice): {plan.get('sensory_anchor')}
LANDING IMAGE (end on this): {plan.get('landing_image')}
MUST INCLUDE: {brief.get('must_include')}
CHARACTERS: {brief.get('characters')}
SETTING: {brief.get('setting')}

WHAT HAPPENS, IN ORDER. Each step comes with a detail and a line of dialogue.
Use all three parts of every step - that is what turns a step into two or three
paragraphs instead of one thin sentence. Rewrite the dialogue in your own words
if it fits the voice better, but somebody speaks in nearly every step.

Write it as a story, not as a list being worked through. Never open a paragraph
by announcing the step ("a problem arose", "feeling a distance") - just show it
happening.

{beat_lines}"""
    return system, user


# --------------------------------------------------------------------------
# Stage 5a - Rubric judge
# --------------------------------------------------------------------------

RUBRIC_WEIGHTS: dict[str, float] = {
    "age_fit": 0.20,
    "bedtime_landing": 0.20,
    "story_arc": 0.15,
    "heart": 0.15,
    "read_aloud": 0.15,
    "faithfulness": 0.10,
    "spark": 0.05,
}


def judge_messages(
    story: str,
    brief: dict[str, Any],
    strategy: Strategy,
    plan: dict[str, Any],
    facts: dict[str, Any] | None = None,
) -> tuple[str, str]:
    # `facts` is measured in Python before we get here. Early versions asked the
    # judge for a word count and a refrain count and it was wrong most of the
    # time - it would confidently report "refrain appears 3 times" for a draft
    # that used it twice. Anything countable is now counted in code and handed
    # to the judge as ground truth, so the model only does the part that needs
    # taste.
    facts = facts or {}
    system = f"""\
You are a picky editor of children's read-aloud collections. You have rejected
far more stories than you have bought. You are grading a draft against the exact
brief the writer was given, which appears below.

{READ_ALOUD_STYLE}

RUBRIC - score each 1 to 5 (integers only).

age_fit: could a six-year-old follow every sentence by ear?
  5 = every line lands; 3 = two or three sentences too long or too abstract;
  1 = adult register, or content unsuitable for the age.
story_arc: all seven beats present, cause and effect clear.
  5 = the wobble is resolved by something a character does; 3 = one beat is thin,
  or the fix arrives by luck; 1 = episodic, no shape.
heart: do the characters want something, and does anything change in them?
  5 = a moment that would make a parent glance down; 3 = pleasant but flat;
  1 = cardboard.
bedtime_landing: does the last 80-120 words actually slow down?
  5 = sentences shorten, sound softens, everything settles; 3 = calm-ish but the
  energy never drops; 1 = ends on excitement, a cliffhanger, or a stated moral.
read_aloud: rhythm, dialogue, and whether the refrain lands naturally where it
  appears (the count itself is measured for you below - do not recount it).
  5 = a joy to perform; 3 = readable but monotone; 1 = tongue-twisters or long
  tangled clauses.
faithfulness: is everything the child asked for present and central, with names
  spelled exactly as requested?
  5 = all of it, load-bearing; 3 = present but decorative; 1 = missing.
spark: one genuinely surprising image, turn or joke.
  5 = something you have not read before; 3 = competent and familiar; 1 = generic.

CALIBRATION: a competent but forgettable story is a 3. Reserve 5 for work you
would put in print. Do not inflate - this is the instruction graders ignore most
often. A typical draft earns at most two 5s across the seven dimensions. If you
are about to award more than two, you are being generous, not accurate.

HARD RULES tied to the measurements below - these override your impression:
- If length_verdict is "far too short", story_arc and heart score 3 at most:
  beats told in one sentence each have been listed, not told. A draft marked
  "a little short" is fine - do not mark it down for length.
- If the refrain count is not 3, read_aloud scores 3 at most.
- If the final paragraph contains excitement, a new question, a stated moral, or
  anything that would make a child sit up, bedtime_landing scores 2 at most.
- If a beat name from the scaffolding appears in the prose (phrases like "the
  ridiculous problem" or "the wobble"), story_arc scores 2 at most.

MEASURED FACTS: the numbers below were counted in code. Treat them as true even
if your own reading disagrees, and factor them into the scores.

FIXES: at most three, ordered by how much they would raise the score. Each one
must quote the exact phrase from the draft it refers to, and each "fix" must be
an instruction the writer can carry out without guessing. Never rewrite the
story yourself and never write replacement prose longer than eight words.

SAFETY: list any breach of the fear budget. If safety_flags is non-empty,
age_fit must be 1.

{JSON_ONLY}

Schema:
{{
  "scores": {{"age_fit": 0, "story_arc": 0, "heart": 0, "bedtime_landing": 0,
              "read_aloud": 0, "faithfulness": 0, "spark": 0}},
  "one_line_verdict": "<what a parent would think, in one sentence>",
  "safety_flags": ["<breach>"],
  "what_works": ["<keep this - be specific>"],
  "must_fix": [{{"issue": "<what is wrong>", "quote": "<exact phrase from the draft>", "fix": "<what to do instead>"}}]
}}"""
    measured = "\n".join(f"- {k}: {v}" for k, v in facts.items()) or "- (none)"
    user = f"""\
BRIEF THE WRITER WAS GIVEN
- kind: {strategy.label} ({strategy.extras})
- target length: about {strategy.target_words} words
- child's request: {brief.get('clean_request')}
- must include: {brief.get('must_include')}
- refrain that should appear exactly 3 times: {plan.get('refrain')}

MEASURED FACTS (counted in code, not by you)
{measured}

DRAFT
\"\"\"
{story}
\"\"\"

Grade it."""
    return system, user


# --------------------------------------------------------------------------
# Stage 5b - The test audience. A simulated listener, not a rubric.
# This is the check that catches "technically correct but boring".
# --------------------------------------------------------------------------


def audience_messages(story: str) -> tuple[str, str]:
    system = f"""\
You are Maya. You are seven. Someone just read you this story at bedtime.

You are honest in the way seven-year-olds are honest. If a part was boring you
say it was boring. If you did not understand a word you say so. You are not
trying to be nice about it.

Answer in your own voice - short sentences, your own words, no big vocabulary.

{JSON_ONLY}

Schema:
{{
  "favourite_part": "<what you liked best, in your words>",
  "confusing_bit": "<a word or part you did not get, or empty string>",
  "boring_bit": "<where you stopped listening, or empty string>",
  "too_scary": "<anything that would keep you awake, or empty string>",
  "sleepy_at_the_end": true | false,
  "one_wish": "<one thing you wish had happened instead>",
  "again_tomorrow": true | false
}}"""
    user = f'Here is the story:\n"""\n{story}\n"""'
    return system, user


# --------------------------------------------------------------------------
# Stage 6 - Reviser. Surgical edits only.
# --------------------------------------------------------------------------


def reviser_messages(
    story: str,
    critique: dict[str, Any],
    audience: dict[str, Any],
    strategy: Strategy,
    plan: dict[str, Any],
    checks: dict[str, Any] | None = None,
    user_note: str = "",
) -> tuple[str, str]:
    # The length rule has to come from the measurement, not be a fixed line.
    # An earlier version told the reviser "do not grow by more than 10 percent"
    # in the system prompt while the fix list said "the draft is too short,
    # expand it". It obeyed the cap, so drafts that came out at a third of
    # target never recovered across any number of rounds.
    checks = checks or {}
    verdict = checks.get("length_verdict", "ok")
    words = checks.get("word_count", 0)
    if verdict == "far too short":
        length_rule = (
            f"- The draft is {words} words and should be about "
            f"{strategy.target_words}. You MUST make it substantially longer. "
            f"Expand the beats that are already there - beats 2 to 5 - into two "
            f"or three paragraphs each: more dialogue, more of what the "
            f"characters noticed, tried and said. Do NOT add scenes after the "
            f"ending, do NOT add a second ending, and do NOT continue the story "
            f"past its landing. The final paragraph of your revision must be the "
            f"same closing image as the draft you were given."
        )
    elif verdict == "far too long":
        length_rule = (
            f"- The draft is {words} words and should be about "
            f"{strategy.target_words}. Tighten the middle beats. Do not cut the "
            f"ending."
        )
    elif verdict == "a little short":
        length_rule = (
            f"- The draft is {words} words against a rough target of "
            f"{strategy.target_words}. That is acceptable. If a beat genuinely "
            f"needs more room, give it more; otherwise leave the length alone. "
            f"Do not pad."
        )
    else:
        length_rule = (
            f"- Length is fine at {words} words. Keep it roughly there."
        )

    system = f"""\
You are a revision editor. You do not start over. You make the smallest set of
changes that fixes the named problems, and you leave everything else alone.

{READ_ALOUD_STYLE}

RULES
- Keep the title unless a note specifically asks you to change it.
{length_rule}
- Every listed fix must actually be carried out. The first fixes in the list
  were measured by a machine and are certainly true - do those exactly.
- Do not touch the parts listed under WHAT WORKS.
- Return the complete revised story in the required format. No notes, no
  explanation of what you changed, no diff."""

    fixes = "\n".join(
        f'- {f.get("issue")}\n  (in: "{f.get("quote")}")\n  do: {f.get("fix")}'
        for f in critique.get("must_fix", [])
    ) or "- none listed"
    works = "\n".join(f"- {w}" for w in critique.get("what_works", [])) or "- none listed"

    listener = []
    if audience.get("confusing_bit"):
        listener.append(f'- she did not understand: "{audience["confusing_bit"]}"')
    if audience.get("boring_bit"):
        listener.append(f'- she stopped listening at: "{audience["boring_bit"]}"')
    if audience.get("too_scary"):
        listener.append(f'- this would keep her awake: "{audience["too_scary"]}"')
    if audience.get("sleepy_at_the_end") is False:
        listener.append("- the ending did not make her sleepy; slow the last paragraphs down")
    listener_block = "\n".join(listener) or "- the listener had no complaints"

    priority = ""
    if user_note.strip():
        priority = f"""\
HIGHEST PRIORITY - the family asked for this directly, and it outranks every
other note below. If it conflicts with an editor's note, follow the family:
\"\"\"{user_note.strip()}\"\"\"

"""

    user = f"""\
{priority}EDITOR'S FIXES (all must be done):
{fixes}

WHAT WORKS (do not disturb):
{works}

WHAT THE SEVEN-YEAR-OLD SAID:
{listener_block}

REFRAIN (must appear word for word exactly three times): {plan.get('refrain')}

CURRENT DRAFT:
\"\"\"
{story}
\"\"\"

Revise it."""
    return system, user


# --------------------------------------------------------------------------
# Baseline - the naive one-shot the original skeleton would have made.
# Used by eval.py to show what the pipeline is actually buying.
# --------------------------------------------------------------------------


def baseline_messages(request: str) -> tuple[str, str]:
    return (
        "You are a helpful assistant.",
        f"Tell a bedtime story for a child aged 5 to 10. {request}",
    )
