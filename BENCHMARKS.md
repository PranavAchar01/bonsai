# Benchmark runs for Kabir

We need four more trees besides chess: legal, health, finance and coding. Each one is a connected set under `tasks/`, grown with the same loop, and they can all run at the same time on your machine. This file says what to build, how to run it, and exactly which files to push so the page and the README can use your numbers.

Deadline for results on GitHub: 3:45 PM PT. Submission is 5:00 PM.

## What a usable result looks like

A tree only grows when the seed harness fails some problems. If the seed scores 100%, every change gets pruned, which is what happened on Aider Polyglot Python. So for each set:

1. Score the seed first on about 20 problems.
2. Keep the set only if the seed lands between 30% and 75%. Above that, swap in harder problems. Below that, the task is probably broken or graded wrong.
3. Then run the loop for 3 generations.

The player model is `haiku` in every set and must stay `haiku`. The proposer is `claude-opus-5-5`, which is now the default.

## Setup

```bash
git checkout chess-replay
git pull origin master
```

`loop.py` on master now reads a `BONSAI_LOG` variable, so each set writes its own log and four loops do not overwrite each other.

## The four sets

Each set is a folder `tasks/<key>/` with a `set.json`, one folder per problem, and a grader. `tasks/README.md` has the full format and `tasks/chess/` is a working example. Use 40 problems per set: 20 for practice and 20 held out. Problem folders are split by alternating name order, so name them `q001`, `q002` and so on.

Every problem folder has:

- `.docs/instructions.md`: the question as the agent sees it, ending with the exact answer format, for example "Write only the letter A, B, C or D to answer.txt".
- `expected.json`: `{"answer": "B"}`. List this file under `hidden` in `set.json` so the agent never sees it.

One grader can serve all three question sets. Put it at `harness/grade_exact.py`: read `answer.txt`, strip and lowercase it, compare with `expected.json`, exit 0 on a match and 1 otherwise. For finance, compare numbers with a 1% tolerance after removing `$`, `%` and commas.

| Key | Source on Hugging Face | Which part to use | Answer |
|---|---|---|---|
| `legal` | `nguha/legalbench` | Two or three tasks with a fixed label set, such as `hearsay`, `abercrombie` and one of the `contract_nli_*` tasks | The label, exact match |
| `health` | `GBaker/MedQA-USMLE-4-options` | Test split | A, B, C or D |
| `finance` | `ibm/finqa` | Test split, questions with a single numeric answer | A number within 1% |
| `coding` | Aider Polyglot, already in the repo | C++, Java or Rust problems that the seed fails | The repo's tests |

Check each dataset card before you download it. If a dataset is gated or its card asks you to accept terms, that is a decision for you or Pranav, so stop and ask.

Seed harness for the three question sets, as `seed` in `set.json`:

```json
{"rules": "Answer the question in .docs/instructions.md and write the answer to answer.txt.", "tools": ["Read", "Write"], "max_turns": 4}
```

For `coding` you do not need a new folder. Run the seed on 30 problems in one language, write down the ones it fails, and build the practice and held-out lists so each has at least four failures.

If a seed score comes out too high, make the set harder before running the loop: for legal, drop the easy task and add a harder one; for health, keep only the questions the seed got wrong plus an equal number it got right; for finance, keep questions that need two or more calculation steps.

## Running them in parallel

Score a seed (one terminal per set):

```bash
uv run --no-project --with raindrop-ai --with python-dotenv python -m harness.evaluate --tasks legal/q001 legal/q002 legal/q003 --workers 4 --scratch /tmp/bonsai-legal --out runs/legal-seed.json
```

List all 40 problems after `--tasks`. Then grow the tree, again one terminal per set, each with its own log and scratch folder:

```bash
BONSAI_LOG=runs/legal-versions.jsonl uv run --no-project --with raindrop-ai --with python-dotenv python loop.py --practice legal/q001 legal/q003 --heldout legal/q002 legal/q004 --generations 3 --workers 4 --scratch /tmp/bonsai-legal
```

Odd-numbered problems go to `--practice` and even-numbered to `--heldout`, all 20 of each. Repeat with `health`, `finance` and the coding lists, changing the key in the log name and the scratch path. Four loops at four workers each is 16 agents at once. If you hit rate limits, drop to `--workers 2`.

Do not run the app (`server.py`) on the same clone while these are going. It uses the default log.

## What to push

For each set, these files:

| File | Why we need it |
|---|---|
| `tasks/<key>/` (set.json, problems, build script) | So anyone can rerun it from the page |
| `harness/grade_exact.py` | The grader |
| `runs/<key>-seed.json` | The seed score on all 40 problems |
| `runs/<key>-versions.jsonl` | The tree: every version, kept or pruned, with its score, change and diff |

`runs/` is ignored by git, so force-add it:

```bash
git add tasks/legal tasks/health tasks/finance harness/grade_exact.py
git add -f runs/*-seed.json runs/*-versions.jsonl
git commit -m "Legal, health, finance and coding runs"
git push origin chess-replay
```

Also put one line per set in the commit body or in a message to Pranav: seed held-out score, best kept held-out score, how many changes were kept and how many pruned. For example: `legal: seed 55%, best 70%, 2 kept, 4 pruned`.

## Rules that keep the numbers honest

- Held-out answers stay hidden. Never put an answer, or anything derived from one, in `instructions.md`.
- Do not edit a problem after you have seen the loop's results on it. If a problem is broken, remove it from both lists and say so in the commit.
- Report every run, including ones where nothing was kept. A tree with only pruned branches is still a result.
- Do not change the player model to get a higher score.
- If chess games are sitting in `tasks/chess/_replays/` on your machine, push those too with `git add tasks/chess/_replays`.
