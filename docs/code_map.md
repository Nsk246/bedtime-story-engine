# Code map

For anyone opening this repo cold. Ten minutes end to end.

## Reading order

| # | File | Lines | What it is | Why it's separate |
|---|---|---|---|---|
| 1 | `prompts.py` | ~600 | Every word sent to the model, and the six strategies | The prompts are the product. They should be readable without wading through control flow. |
| 2 | `pipeline.py` | ~440 | The graph: what runs, in what order, and when to loop | Orchestration only. It contains no prompt text. |
| 3 | `checks.py` | ~165 | Pure measurement over a string | Imports nothing from the project. Readable in two minutes, testable with no API key. |
| 4 | `llm.py` | ~250 | The OpenAI client, retries, JSON coercion, tracing | Knows nothing about stories. Would work unchanged for any other task. |
| 5 | `main.py` | ~125 | CLI, rendering, the family feedback loop | Thin on purpose. Nothing here decides anything. |
| — | `smoke_test.py` | ~220 | Whole graph against a stubbed model | Start here if you'd rather see behaviour than read code. No key needed. |
| — | `eval.py` | ~140 | Pipeline vs the naive single prompt | The efficacy argument, and its own caveats. |

Every file opens with a header saying what it is, what it is *not*, and what to
read next. If you only have five minutes: read `READ_ALOUD_STYLE` in
`prompts.py`, then `StoryEngine.tell()` in `pipeline.py`. That's the whole idea.

## One request, traced through the code

Following `python main.py --request "a story about a shy dragon who bakes bread"`:

```
main.main()
└─ StoryEngine.tell(request)                          pipeline.py

   ├─ route()                                         stage 1 · temp 0.0
   │    prompts.classifier_messages()   → picks 1 of 6 strategies, softens if
   │    llm.complete_json()               needed, returns a structured brief
   │
   ├─ plan_story()                                    stage 2 · temp 0.9
   │    prompts.architect_messages()    → seven named beats, a refrain, and
   │    llm.complete_json()               the image the story lands on
   │    _pad_beats()                      if fewer than 7 came back
   │
   ├─ write()                                         stage 3 · temp 0.85
   │    prompts.storyteller_messages()  → the only prose call in the system
   │    llm.complete()
   │    split_title()                     first line is the title
   │
   ├─ evaluate(draft)                                 stage 4
   │    checks.measure()                → word count, refrain repeats,
   │                                       sentence lengths, stated morals
   │    checks.facts_for_judge()          strip private keys
   │    ThreadPoolExecutor:               these two are independent
   │      ├─ prompts.judge_messages()   → 7 scores + ≤3 quoted fixes · temp 0.0
   │      └─ prompts.audience_messages()→ Maya, age 7: bored? scared? · temp 0.7
   │    _normalize_critique()             coerce odd shapes once, at the boundary
   │    checks.to_fixes()                 measured failures, prepended to the list
   │    weighted_score()                  the mean, computed in Python
   │
   ├─ is_good_enough(champion)                        the gate
   │    checks.has_hard_failure()         objective misses block regardless
   │    → if no: revise(champion) and evaluate again, up to 2 rounds
   │
   ├─ revise(best_draft)                              stage 5 · temp 0.5
   │    prompts.reviser_messages()      → fixes + listener notes + a
   │    llm.complete()                    do-not-touch list
   │
   └─ champion()                          highest score ships, not the last draft

main then renders it, offers the family a revision, and optionally saves the
full trace with engine.save_run().
```

## Where the tuning knobs live

Everything adjustable, in one place, so you don't have to hunt:

| Knob | Where | Default | What it does |
|---|---|---|---|
| `TARGET_SCORE` | `pipeline.py` | 4.3 | Weighted score needed to ship |
| `MIN_DIMENSION` | `pipeline.py` | 4.0 | No single dimension may sit below this |
| `MAX_ROUNDS` | `pipeline.py` | 2 | Revision passes before shipping the best we have |
| `RUBRIC_WEIGHTS` | `prompts.py` | 7 dims | Age fit and the landing carry 0.20 each |
| `STRATEGIES` | `prompts.py` | 6 | Categories, arcs, tone, target length |
| `READ_ALOUD_STYLE` | `prompts.py` | — | The style bible: sentences, fear budget, the landing |
| `MAX_SENTENCE_WORDS` | `checks.py` | 28 | Longest sentence readable in one breath |
| `TOO_LONG` / `TOO_SHORT` | `checks.py` | 1.25 / 0.6 | Length tolerance around the target |
| Temperature per stage | `pipeline.py` | 0.0–0.9 | Set at each call site, not globally |

## The five decisions worth knowing

1. **Count in code, judge with the model.** Anything countable lives in
   `checks.py`. The judge used to be asked for these numbers and got them wrong
   most of the time.
2. **Plan before prose.** `plan_story()` before `write()`. The single biggest
   quality lever with a model this size.
3. **The judge never rewrites.** A judge that rewrites becomes a second author
   and you lose the evaluation signal entirely.
4. **The best draft ships, and the next revision starts from it.** Without the
   second half, one bad revision poisons every round after it. `smoke_test.py`
   has a fake judge that scores draft 3 worse than draft 2 to test exactly this.
5. **Soften, don't refuse.** A refusal at bedtime is a product failure. The
   router keeps what the child loved and drops what would scare them.
