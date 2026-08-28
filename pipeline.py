"""
pipeline.py - the agent graph. This is the file to read second.

WHAT THIS FILE IS
    The orchestration. It decides what happens in what order, when to loop, and
    which draft ships. Every model call it makes is one function in prompts.py.

WHAT IT IS NOT
    Prompts (those are in prompts.py), the OpenAI client (llm.py), or the
    measurements (checks.py). If you are looking for the words sent to the
    model, this is the wrong file.

THE FLOW
    request
      -> route()       stage 1  classify + soften        JSON, temp 0.0
      -> plan_story()  stage 2  seven beats + refrain    JSON, temp 0.9
      -> write()       stage 3  the only prose call      text, temp 0.85
      -> evaluate()    stage 4  measure, then judge + child in parallel
      -> is_good_enough()       the gate
      -> revise()      stage 5  surgical edits           text, temp 0.5
      -> champion()             highest score ships, not the last draft

TWO RULES WORTH KNOWING BEFORE YOU READ
    Anything countable is counted in checks.py, never asked of the model.
    The model never does arithmetic: the weighted mean lives in weighted_score().

READ NEXT
    prompts.py for what each stage actually says. docs/architecture.md for why.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from checks import apply_ceilings, facts_for_judge, has_hard_failure, measure, to_fixes
from llm import LLMError, StoryLLM
from prompts import (
    RUBRIC_WEIGHTS,
    Strategy,
    architect_messages,
    audience_messages,
    baseline_messages,
    classifier_messages,
    get_strategy,
    judge_messages,
    reviser_messages,
    storyteller_messages,
)

TARGET_SCORE = 4.3  # weighted, out of 5
MIN_DIMENSION = 4.0  # no single dimension may sit below this
MAX_ROUNDS = 2  # revision passes before we ship the best we have

# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------


@dataclass
class Draft:
    round: int
    title: str
    body: str
    scores: dict[str, int] = field(default_factory=dict)
    overall: float = 0.0
    critique: dict[str, Any] = field(default_factory=dict)
    audience: dict[str, Any] = field(default_factory=dict)
    checks: dict[str, Any] = field(default_factory=dict)
    note: str = ""  # what prompted this draft to exist

    @property
    def full_text(self) -> str:
        return f"{self.title}\n\n{self.body}".strip()

    @property
    def word_count(self) -> int:
        return len(self.body.split())

    @property
    def weakest(self) -> tuple[str, int]:
        if not self.scores:
            return ("", 0)
        return min(self.scores.items(), key=lambda kv: kv[1])


def weighted_score(scores: dict[str, Any]) -> float:
    """Weighted mean over the rubric. A missing dimension counts as a 3."""
    if not isinstance(scores, dict):
        scores = {}
    total = 0.0
    for key, weight in RUBRIC_WEIGHTS.items():
        try:
            value = float(scores.get(key, 3))
        except (TypeError, ValueError):
            value = 3.0
        total += max(1.0, min(5.0, value)) * weight
    return round(total, 2)


def split_title(text: str) -> tuple[str, str]:
    """First non-empty line is the title, the rest is the story."""
    lines = [ln.rstrip() for ln in text.strip().splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return "Untitled", ""
    title = re.sub(r"^(?:story\s+)?title\s*[:\-]\s*", "", lines[0].strip(), flags=re.I)
    title = title.strip(" #*_\"'\u201c\u201d\u2018\u2019").strip()
    body = "\n".join(lines[1:]).strip()
    if not body:
        # Model ignored the format and ran the title into the prose. Rather than
        # guess where the title ends, keep the whole thing as the body.
        return "Untitled", text.strip()
    return title or "Untitled", body


# --------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------


class StoryEngine:
    def __init__(
        self,
        llm: StoryLLM | None = None,
        *,
        max_rounds: int = MAX_ROUNDS,
        target_score: float = TARGET_SCORE,
        min_dimension: float = MIN_DIMENSION,
        verbose: bool = True,
    ):
        self.llm = llm or StoryLLM()
        self.max_rounds = max_rounds
        self.target_score = target_score
        self.min_dimension = min_dimension
        self.verbose = verbose

        self.request: str = ""
        self.brief: dict[str, Any] = {}
        self.strategy: Strategy | None = None
        self.plan: dict[str, Any] = {}
        self.drafts: list[Draft] = []
        self.started = time.time()

    def _say(self, line: str) -> None:
        if self.verbose:
            print(line, flush=True)

    # -- stage 1 -----------------------------------------------------------

    def route(self, request: str) -> dict[str, Any]:
        """Vague or unsuitable request in, structured brief and a strategy out."""
        system, user = classifier_messages(request)
        brief = self.llm.complete_json(system, user, stage="1-intake", temperature=0.0)
        brief.setdefault("clean_request", request)
        self.request, self.brief = request, brief
        self.strategy = get_strategy(brief.get("strategy", ""))
        self._say(
            f"  [1/5] intake      {self.strategy.label.lower()} · {str(brief.get('setting', ''))[:46]}"
        )
        if brief.get("needs_softening"):
            self._say(f"        softened    {str(brief.get('softened_note', ''))[:70]}")
        return brief

    # -- stage 2 -----------------------------------------------------------

    def plan_story(self) -> dict[str, Any]:
        """Seven beats, a refrain and a landing image, before a word is written."""
        assert self.strategy is not None
        system, user = architect_messages(self.brief, self.strategy)
        plan = self.llm.complete_json(
            system, user, stage="2-architect", temperature=0.9, max_tokens=1000
        )
        beats = plan.get("beats") or []
        if len(beats) != 7:
            # Happens maybe one run in ten, usually 5 or 6 beats. Not worth a
            # retry when the arc names are already known; pad and carry on.
            plan["beats"] = _pad_beats(beats, self.strategy)
        plan.setdefault("refrain", "")
        self.plan = plan
        self._say(
            f'  [2/5] architect   7 beats · "{plan.get("title", "?")}" '
            f'· refrain: "{plan.get("refrain", "")}"'
        )
        return plan

    # -- stage 3 -----------------------------------------------------------

    def write(self) -> Draft:
        """The only prose call in the system. Everything else is JSON."""
        assert self.strategy is not None
        system, user = storyteller_messages(self.brief, self.strategy, self.plan)
        text = self.llm.complete(
            system, user, stage="3-storyteller", temperature=0.85, max_tokens=1500
        )
        title, body = split_title(text)
        title, body = self._repair_title(title, body)
        draft = Draft(round=1, title=title, body=body, note="first draft")
        self._say(f"  [3/5] storyteller draft 1 · {draft.word_count} words")
        return draft

    # -- stage 4 -----------------------------------------------------------

    def evaluate(self, draft: Draft) -> Draft:
        """Measure first, then ask the panel. The measurements go into the
        judge's prompt so it isn't guessing at numbers, and any that failed are
        appended to the fix list afterwards so the reviser can't skip them."""
        assert self.strategy is not None
        refrain = str(self.plan.get("refrain", ""))
        checks = measure(draft.body, self.strategy.target_words, refrain, self.strategy.arc)

        # The judge and the child are independent, so run them together. Saves
        # roughly a third of the wall clock on every round. The OpenAI client is
        # thread-safe; the only shared state is the trace list, and appends to a
        # list are atomic, so ordering can interleave but nothing is lost.
        j_sys, j_user = judge_messages(
            draft.full_text, self.brief, self.strategy, self.plan, facts_for_judge(checks)
        )
        a_sys, a_user = audience_messages(draft.full_text)

        with ThreadPoolExecutor(max_workers=2) as pool:
            judge_future = pool.submit(
                self.llm.complete_json,
                j_sys,
                j_user,
                stage=f"4a-judge-r{draft.round}",
                temperature=0.0,
            )
            audience_future = pool.submit(
                self.llm.complete_json,
                a_sys,
                a_user,
                stage=f"4b-audience-r{draft.round}",
                temperature=0.7,
                max_tokens=500,
            )
            critique = judge_future.result()
            try:
                audience = audience_future.result()
            except LLMError:
                # The child is advisory. If she fails to parse, the run goes on.
                audience = {}

        critique = _normalize_critique(critique)

        # Machine-found problems go to the front: they are certainly true, and
        # the reviser reads the list top down.
        machine_fixes = to_fixes(checks)
        critique["must_fix"] = (machine_fixes + critique["must_fix"])[:4]

        draft.checks = checks
        raw = {k: _clamp(critique["scores"].get(k)) for k in RUBRIC_WEIGHTS}
        draft.scores, ceiling_notes = apply_ceilings(raw, checks)
        draft.critique = critique
        draft.audience = _clean_audience(audience)
        draft.overall = weighted_score(draft.scores)

        low = draft.weakest
        line = (
            f"  [4/5] panel       draft {draft.round} scored {draft.overall:.2f}/5 "
            f"· weakest: {low[0]} {low[1]}/5"
        )
        if critique["safety_flags"]:
            line += f" · SAFETY: {critique['safety_flags'][0][:30]}"
        self._say(line)
        for note in ceiling_notes:
            self._say(f"        capped      {note[:64]}")
        if machine_fixes:
            self._say(f"        checks      {machine_fixes[0]['issue'][:62]}")
        if draft.audience.get("boring_bit"):
            self._say(f'        maya (7)    "{str(draft.audience["boring_bit"])[:60]}"')
        return draft

    # -- stage 5 -----------------------------------------------------------

    def revise(self, draft: Draft, user_note: str = "") -> Draft:
        """Surgical edits to one draft. Called with the champion, not the latest."""
        assert self.strategy is not None
        system, user = reviser_messages(
            draft.full_text,
            draft.critique,
            draft.audience,
            self.strategy,
            self.plan,
            draft.checks,
            user_note,
        )
        text = self.llm.complete(
            system, user, stage=f"5-reviser-r{draft.round}", temperature=0.5, max_tokens=1600
        )
        title, body = split_title(text)
        title, body = self._repair_title(title, body)
        note = user_note.strip() or "; ".join(
            f.get("issue", "") for f in draft.critique.get("must_fix", [])[:2]
        )
        # Numbered by position in the session, not parent + 1. When we revise
        # from an earlier champion those two disagree, and the scorecard would
        # otherwise show two rows both labelled "3".
        new = Draft(
            round=len(self.drafts) + 1, title=title or draft.title, body=body, note=note
        )
        self._say(
            f"  [5/5] reviser     draft {new.round} · {new.word_count} words · fixing: {note[:56]}"
        )
        return new

    def _repair_title(self, title: str, body: str) -> tuple[str, str]:
        """Fall back to the planned title when the writer didn't supply one.

        One run opened its story with the refrain, so the first line - which we
        treat as the title - was the refrain, and the story shipped headless.
        The planner already chose a title, so use it and hand the line back to
        the story where it belongs.
        """
        planned = str(self.plan.get("title") or "").strip()
        refrain = " ".join(str(self.plan.get("refrain", "")).lower().split())
        looks_like_refrain = refrain and " ".join(title.lower().split()).strip(".!?") == refrain.strip(".!?")
        if planned and (looks_like_refrain or title in ("", "Untitled")):
            return planned, (f"{title}\n\n{body}" if looks_like_refrain else body)
        return title, body

    # -- the gate ----------------------------------------------------------

    def is_good_enough(self, draft: Draft) -> bool:
        """The gate. Safety and measured failures block regardless of the score."""
        if draft.critique.get("safety_flags"):
            return False
        if has_hard_failure(draft.checks):
            return False  # objective miss, no opinion required
        if draft.overall < self.target_score:
            return False
        return min(draft.scores.values(), default=0) >= self.min_dimension

    # -- the loop ----------------------------------------------------------

    def tell(self, request: str) -> Draft:
        """The whole graph, start to finish. This is the function to read first."""
        self.route(request)
        self.plan_story()
        self.drafts.append(self.evaluate(self.write()))

        for _ in range(self.max_rounds):
            best = self.champion()
            if self.is_good_enough(best):
                break
            # Revise from the best draft so far, not the most recent one. A bad
            # revision used to poison every round that followed it.
            self.drafts.append(self.evaluate(self.revise(best)))

        champion = self.champion()
        if champion is not self.drafts[-1]:
            self._say(f"        keeping draft {champion.round}, it scored higher than the last pass")
        return champion

    def apply_feedback(self, draft: Draft, note: str) -> Draft:
        """A change the family asked for. Revised, then graded like any draft."""
        revised = self.evaluate(self.revise(draft, user_note=note))
        revised.note = f"family asked: {note}"
        self.drafts.append(revised)
        # Deliberately returned even if it scores lower. They asked for it, and
        # the judge does not get a veto over the person in the room.
        return revised

    def champion(self) -> Draft:
        """Best draft so far. Ties go to the later one."""
        return max(self.drafts, key=lambda d: (d.overall, d.round))

    # -- reporting ---------------------------------------------------------

    def scorecard(self) -> str:
        """The per-draft score table printed after a run."""
        cols = list(RUBRIC_WEIGHTS)
        head = "  draft  " + "  ".join(c[:5].rjust(5) for c in cols) + "   overall  checks"
        rows = [head, "  " + "-" * (len(head) - 2)]
        best = self.champion()
        for d in self.drafts:
            cells = "  ".join(str(d.scores.get(c, "-")).rjust(5) for c in cols)
            state = "fail" if has_hard_failure(d.checks) else "ok"
            mark = "  <- shipped" if d is best else ""
            rows.append(f"  {d.round:>5}  {cells}   {d.overall:>7.2f}  {state:>6}{mark}")
        if has_hard_failure(best.checks):
            # Be honest on the way out rather than quietly shipping a miss.
            issues = "; ".join(f["issue"] for f in to_fixes(best.checks)[:2])
            rows.append(f"\n  note: shipped with unresolved checks - {issues}")
        return "\n".join(rows)

    def save_run(self, directory: str | Path = "runs") -> Path:
        """Dump prompts, scores, checks and every draft to runs/ for inspection."""
        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"run-{datetime.now():%Y%m%d-%H%M%S}.json"
        payload = {
            "request": self.request,
            "brief": self.brief,
            "strategy": self.strategy.key if self.strategy else None,
            "plan": self.plan,
            "seconds": round(time.time() - self.started, 1),
            "usage": {
                "calls": self.llm.usage.calls,
                "prompt_tokens": self.llm.usage.prompt_tokens,
                "completion_tokens": self.llm.usage.completion_tokens,
                "estimated_cost_usd": round(self.llm.usage.estimated_cost_usd(), 4),
            },
            "drafts": [
                {
                    "round": d.round,
                    "note": d.note,
                    "title": d.title,
                    "words": d.word_count,
                    "scores": d.scores,
                    "overall": d.overall,
                    "checks": d.checks,
                    "critique": d.critique,
                    "audience": d.audience,
                    "text": d.full_text,
                }
                for d in self.drafts
            ],
            "shipped_round": self.champion().round,
            "trace": self.llm.trace_as_dicts(),
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return path


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def baseline_story(llm: StoryLLM, request: str) -> Draft:
    """What the original skeleton produced: one prompt, one call, no judge."""
    system, user = baseline_messages(request)
    text = llm.complete(system, user, stage="baseline", temperature=0.1, max_tokens=1500)
    title, body = split_title(text)
    return Draft(round=0, title=title, body=body, note="baseline (single prompt)")


def _normalize_critique(critique: Any) -> dict[str, Any]:
    """
    The judge is prompted for a precise shape and usually delivers it, but a
    stray list-of-strings where dicts belong would blow up the reviser two
    stages later. Coerce once here rather than defending in three places.
    """
    c = critique if isinstance(critique, dict) else {}
    c["scores"] = c.get("scores") if isinstance(c.get("scores"), dict) else {}
    c["what_works"] = [str(w) for w in c.get("what_works") or [] if w]
    c["safety_flags"] = [str(f) for f in c.get("safety_flags") or [] if f]

    fixes: list[dict[str, str]] = []
    for item in c.get("must_fix") or []:
        if isinstance(item, dict):
            fixes.append({k: str(item.get(k, "")) for k in ("issue", "quote", "fix")})
        elif item:
            fixes.append({"issue": str(item), "quote": "", "fix": str(item)})
    c["must_fix"] = fixes[:3]
    return c


# The model answers "None" or "N/A" as a *string* when a field should be empty,
# which then gets printed as though the child had a complaint.
_NON_ANSWERS = {"none", "n/a", "na", "null", "nothing", "-", "no", "nope"}


def _clean_audience(audience: Any) -> dict[str, Any]:
    if not isinstance(audience, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in audience.items():
        if isinstance(value, str) and value.strip().lower().strip(".") in _NON_ANSWERS:
            out[key] = ""
        else:
            out[key] = value
    return out


def _clamp(value: Any) -> int:
    try:
        return max(1, min(5, int(round(float(value)))))
    except (TypeError, ValueError):
        return 3


def _pad_beats(beats: list[Any], strategy: Strategy) -> list[dict[str, Any]]:
    out = [b for b in beats if isinstance(b, dict)][:7]
    for name in strategy.arc[len(out) :]:
        out.append({"beat": name, "what_happens": f"({name} - writer's choice)"})
    return out
