"""
Bedtime Story Engine - main entry point.

Before submitting the assignment, describe here in a few sentences what you
would have built next if you spent 2 more hours on this project:

    1. Validate the judge itself. Right now the judge is trusted on faith. I'd
       hand-label 30 stories, measure the judge's agreement with those labels
       per dimension, and rewrite the anchors on whichever dimensions drift.
       An unvalidated evaluator is just a confident opinion.
    2. Best-of-N instead of a serial loop. Plan once, write three drafts in
       parallel at different temperatures, let the judge pick the strongest, and
       revise only that one. Same wall-clock time, wider search.
    3. A story bible for continuity. Persist characters, places and the refrain
       so tomorrow night's story can be the next chapter, with the judge also
       grading consistency with what came before.
    4. Read-aloud output. The pacing rules in the style bible exist to be heard,
       so I'd add TTS with sentence-level timing and let the child pick a voice.
    5. Cost and latency. Cache intake results for repeat requests, run the judge
       and the test audience concurrently (they don't depend on each other),
       and short-circuit the loop when a revision fails to improve the score.

Usage:
    python main.py
    python main.py --request "a story about a shy dragon who bakes bread"
    python main.py --request "..." --rounds 3 --save
    python main.py --request "..." --baseline     # compare against a single prompt
"""

from __future__ import annotations

import argparse
import sys
import textwrap

from llm import LLMError, StoryLLM
from pipeline import Draft, StoryEngine, baseline_story

RULE = "-" * 72

example_requests = (
    "A story about a girl named Alice and her best friend Bob, who happens to be a cat."
)


def render(draft: Draft) -> str:
    out = [draft.title.upper(), ""]
    for para in draft.body.split("\n\n"):
        para = " ".join(para.split())
        if para:
            out.append(textwrap.fill(para, width=72))
            out.append("")
    return "\n".join(out).rstrip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Tell a bedtime story for ages 5-10.")
    parser.add_argument("--request", "-r", help="the story request (otherwise you'll be asked)")
    parser.add_argument("--rounds", type=int, default=2, help="max revision rounds (default 2)")
    parser.add_argument("--save", action="store_true", help="write the full run trace to runs/")
    parser.add_argument("--quiet", "-q", action="store_true", help="story only, no pipeline output")
    parser.add_argument(
        "--baseline", action="store_true", help="also print a single-prompt story for comparison"
    )
    parser.add_argument("--no-feedback", action="store_true", help="skip the revision prompt")
    args = parser.parse_args()

    request = args.request or input("What kind of story do you want to hear? ").strip()
    if not request:
        request = example_requests
        print(f"(nothing entered - using the example: {request})")

    try:
        llm = StoryLLM()
        engine = StoryEngine(llm, max_rounds=args.rounds, verbose=not args.quiet)

        if not args.quiet:
            print(f"\n{RULE}\nBuilding your story\n{RULE}")

        draft = engine.tell(request)

        if not args.quiet:
            print(f"\n{RULE}")
            print(engine.scorecard())
            print(RULE)

        print("\n" + render(draft) + "\n")

        if args.baseline:
            print(RULE)
            print("For comparison - one prompt, one call, no judge:\n")
            base = baseline_story(llm, request)
            print(render(base) + "\n")
            print("(run eval.py to score the two against the same rubric)\n")

        # The family gets the last word. Their note outranks the judge.
        while not args.no_feedback and not args.quiet:
            note = input("Change anything? (describe it, or press Enter to keep it) ").strip()
            if not note or note.lower() in {"no", "n", "quit", "q", "exit"}:
                break
            print()
            draft = engine.apply_feedback(draft, note)
            print("\n" + render(draft) + "\n")

        if args.save:
            path = engine.save_run()
            print(f"Full trace written to {path}")

        if not args.quiet:
            u = llm.usage
            print(
                f"{u.calls} model calls · {u.total_tokens:,} tokens · "
                f"~${u.estimated_cost_usd():.4f}"
            )
        return 0

    except LLMError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nGoodnight.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
