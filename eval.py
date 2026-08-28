"""
eval.py - does the pipeline actually make better stories than one prompt?

For each request: run the full pipeline, then run the naive single-prompt
baseline (what the original skeleton did), then grade both with the same judge,
at temperature 0, against the same brief and strategy. Order matters - the
pipeline runs first so we know which strategy the request routes to, and the
baseline gets marked on those same terms rather than on a default it never saw.

Two things I want to be honest about:

  The judge shares a style bible with the writer, so it will lean toward the
  pipeline. This is a regression check, not proof. Validating the judge against
  human labels is first on the list in main.py.

  The refrain requirement is waived when grading the baseline. Nothing ever
  asked it for a refrain, so counting it as a miss would be marking it down for
  not following instructions it was never given.

    python eval.py                # 5 requests, ~3 minutes
    python eval.py --requests 2   # quicker
"""

from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime
from pathlib import Path

from llm import StoryLLM
from checks import facts_for_judge, measure
from pipeline import StoryEngine, baseline_story, weighted_score
from prompts import RUBRIC_WEIGHTS, judge_messages

SUITE = [
    "A story about a girl named Alice and her best friend Bob, who happens to be a cat.",
    "something really silly about a penguin who wants to be a firefighter",
    "a story about the dark because my brother is scared of it",
    "why is the ocean salty",
    "my friend didn't sit with me at lunch today",
]


def judge_freeform(llm: StoryLLM, story_text: str, brief: dict, strategy, refrain: str) -> dict:
    """
    Grade the baseline against the same brief and strategy the pipeline was
    routed into. The first version of this graded the baseline against a default
    strategy, which stacked the deck: the baseline was being marked against a
    spec chosen for a different story. Now both sides are judged on the same
    terms, and the refrain requirement is waived for the baseline since nothing
    ever told it to write one.
    """
    checks = facts_for_judge(measure(story_text, strategy.target_words, refrain))
    checks.pop("refrain_used", None)
    checks.pop("refrain_expected", None)
    system, user = judge_messages(story_text, brief, strategy, {"refrain": "(not required)"}, checks)
    return llm.complete_json(system, user, stage="eval-judge", temperature=0.0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=len(SUITE))
    parser.add_argument("--out", default="runs")
    args = parser.parse_args()

    llm = StoryLLM()
    rows = []

    for i, request in enumerate(SUITE[: args.requests], start=1):
        print(f"\n[{i}/{min(args.requests, len(SUITE))}] {request[:64]}")

        # Run the pipeline first so we know which strategy this request routes
        # to, then grade the baseline on those same terms.
        engine = StoryEngine(llm, verbose=False)
        best = engine.tell(request)
        print(f"      pipeline {best.overall:.2f}/5  ({best.word_count} words, draft {best.round})")

        base = baseline_story(llm, request)
        base_scores = judge_freeform(
            llm, base.full_text, engine.brief, engine.strategy, str(engine.plan.get("refrain", ""))
        ).get("scores", {})
        base_overall = weighted_score(base_scores)
        print(f"      baseline {base_overall:.2f}/5  ({base.word_count} words)")

        rows.append(
            {
                "request": request,
                "baseline_overall": base_overall,
                "baseline_scores": base_scores,
                "pipeline_overall": best.overall,
                "pipeline_scores": best.scores,
                "pipeline_rounds": len(engine.drafts),
                "delta": round(best.overall - base_overall, 2),
            }
        )

    print("\n" + "=" * 72)
    print("  request".ljust(46) + "baseline  pipeline   delta")
    print("  " + "-" * 68)
    for r in rows:
        print(
            f"  {r['request'][:42]:<44}"
            f"{r['baseline_overall']:>7.2f}{r['pipeline_overall']:>10.2f}"
            f"{r['delta']:>+8.2f}"
        )
    print("  " + "-" * 68)
    print(
        f"  {'mean':<44}"
        f"{statistics.mean(r['baseline_overall'] for r in rows):>7.2f}"
        f"{statistics.mean(r['pipeline_overall'] for r in rows):>10.2f}"
        f"{statistics.mean(r['delta'] for r in rows):>+8.2f}"
    )

    print("\n  per-dimension mean")
    for dim in RUBRIC_WEIGHTS:
        b = statistics.mean(float(r["baseline_scores"].get(dim, 3)) for r in rows)
        p = statistics.mean(float(r["pipeline_scores"].get(dim, 3)) for r in rows)
        print(f"  {dim:<20}{b:>5.2f}{p:>10.2f}{p - b:>+8.2f}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"eval-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"\n  saved to {path}")
    print(f"  {llm.usage.calls} calls · ~${llm.usage.estimated_cost_usd():.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
