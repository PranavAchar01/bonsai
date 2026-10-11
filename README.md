# Bonsai

A coding agent that rewrites its own harness, and has to cut something every time it adds something.

Each change the agent tries on itself is a branch. A branch stays only if the agent then scores higher on problems it has never seen. The rest are pruned, and the cuts stay in the record. The page draws that history as a bonsai.

Built on 11 October 2026 at the MongoDB Harness Engineering hackathon in San Francisco, for Statement 1, Recursive Harnessing: a harness that rewrites its own rules, tool access and turn budget, and is only allowed to keep a rewrite that measurably helps.

## Results

Every score is on 20 held-out problems the proposer never saw. The player is Claude Haiku in every run and never changes. The proposer is Claude Opus.

| Set | Seed harness | Best kept harness | Changes kept | Changes pruned |
|---|---|---|---|---|
| Health: MedQA exam questions | 80% | 95% | 2 | 2 |
| Legal: LegalBench | 65% | 75% | 2 | 2 |
| Finance: FinQA | 75% | 75% | 0 | 6 |
| Understudy: robot failure lines (4 held out) | 100% | 100% | 0 | 6 |

What the kept changes were:

- Health, 80% to 85%: write only the option letter to the answer file, with no prose around it. Then 85% to 95%: match the chosen answer text back to the letter printed beside it before writing.
- Legal, 65% to 70%: apply the question's own terms literally and bring in nothing from outside. Then 70% to 75%: decide only from the definitions and facts in the provided file.

Finance is the honest null result. Six different rewrites of the rule all scored exactly 75%, so none was kept. The same happened on Understudy, where the seed already passes everything: three of the six changes tied at 100% and three scored lower. A tie is pruned, which is why the tree does not grow from noise.

Coding was dropped as a tree. The seed passes 34 of 34 on Aider Polyglot Python and 23 of 26 on C++, and even a bare seed (three file tools, six turns) passes 24 of 26, so there was nothing to climb.

The health set is a public benchmark of multiple-choice licensing exam questions with one graded letter per question. Bonsai gives no medical advice to anyone.

### Raindrop as a second count

`raindrop_audit.py` reads the held-out attempts back out of Raindrop and recounts every version's score from Raindrop's own record. For all 17 versions in the legal, health and finance runs, Raindrop had 20 of 20 attempts and the recount matched the score the loop logged (`runs/raindrop-audit.json`). The only signal on those events is our own `task_outcome`.

### What was built at the event

Written on the day: the loop, the evaluator and cheat guard, the graders, the server, the page and its live views, the chess set, the nursery question set, the builders for the three benchmark sets, and the Understudy adapter in `tasks/understudy/`.

Not written on the day, and not ours to claim: Understudy itself, which is an earlier project and appears here only as a source of log lines to label. LegalBench, MedQA, FinQA and Aider Polyglot are public datasets. The folder layout follows fractalsearch (see Credits).

## The loop

A harness is a small JSON file holding the rules (the system prompt), the tools the agent may use, the turn limit and the model. The model never changes. The rest is open to revision.

1. Run. The current best harness attempts the practice problems. Every attempt goes to Raindrop as a trace with one `task_outcome` signal, positive for a pass and negative for a fail or for touching a test file.
2. Propose. A second model reads the failures and writes exactly one change to the harness. Whatever it adds, it must remove something of about the same size, such as a rule, a tool or some turns. The proposal is traced to Raindrop too.
3. Score. The new harness runs on held-out problems. The proposer sees that score and never the failures behind it.
4. Keep or prune. A child with a higher held-out score and no tampering is kept and becomes the new best. Any other child is pruned. The proposal's Raindrop trace gets the same signal, positive when kept and negative when pruned.
5. Record. Every version, kept or pruned, is one document: id, parent, the change, what was shed, the failure pattern, score, the harness itself and the diff. They go to `runs/versions.jsonl`, and to the MongoDB collection `bonsai.versions` when `MONGODB_URI` is set.

## Run it

You need [uv](https://docs.astral.sh/uv/), the `claude` CLI signed in, and a C++ compiler for the C++ set.

```bash
git clone https://github.com/Aider-AI/polyglot-benchmark bench
```

```bash
uv run --no-project --with raindrop-ai --with python-dotenv --with pymongo python server.py
```

Open http://localhost:8765. Pick what the agent should improve on and press Grow. While it works, the page lists every agent in the run: one solver per problem on the model being improved, and the proposer that rewrites the harness. Click an agent to watch it work in a live view made for that kind of task (a chess board, a question and answer, or a list of steps). Stop ends the run after the current step.

The header switches between three views of the same data: the tree, the score charts for every set, and a replay of the chess games.

After each run the loop writes the final context file and a full list of what changed to `web/out/latest/` (`context.md`, `CHANGES.md`).

Raindrop tracing turns on when `RAINDROP_WRITE_KEY` is in `.env`. Without it the loop still runs and grades.

The same loop runs without the page:

```bash
uv run --no-project --with raindrop-ai --with python-dotenv python loop.py --practice cpp/clock cpp/yacht --heldout cpp/diamond cpp/sublist --scratch /tmp/bonsai
```

## What it can improve on

### Nursery questions

This set is ours and is not a benchmark. It has sixteen questions about a plant nursery's order database, and each one is answered with a single MongoDB aggregation pipeline. The grader compares the pipeline's output with a result computed in plain Python, so every answer is either right or wrong. `tasks/mongo/build.py` builds the data, the questions and the answers. The set needs a local MongoDB on port 27018:

```bash
mongod --port 27018 --dbpath runs/mongo-data
```

```bash
uv run --no-project --with pymongo python tasks/mongo/build.py --load
```

### Legal, health and finance

Three public benchmarks, 40 problems each, split 20 for practice and 20 held out. `tasks/build_qa.py` builds all three with a fixed seed: LegalBench (hearsay, insurance policy interpretation, citation prediction), MedQA and FinQA. Each answer is one label, letter or number, graded by `harness/grade_exact.py` (exact match after normalising, or within 1% for numbers).

### Chess against Stockfish

Sixteen games, eight openings from each side. Haiku makes one call per move and Stockfish replies. A game passes on a win, a draw, or reaching move 18 without forfeiting and without being behind on material. This set brings its own tools: `legal_moves`, `hanging` and `checks` each add computed facts to what the player sees, so the proposer can trade them against rules and turns. `CHESS_RUN.md` has the run instructions.

### Understudy: robot failures (RSI on a VLA pipeline)

[Understudy](https://github.com/PranavAchar01/understudy-replay) is one place we plugged Bonsai in. With a Runway API key, it films people doing a robot task. 21 physics gates and a MuJoCo replay throw out the clips a robot should not learn from, and SmolVLA, a vision-language-action model, trains on the rest. The last stage is text: every gate rejection and every MuJoCo rollout prints a line. `tasks/understudy/` asks an agent to name each line with one failure term (`grasp_miss`, `missed_bowl`, `duplicated_object` and so on), so the data step knows what to fix. Bonsai grows that agent's rules: recursive self-improvement on the data that trains the VLA.

The 12 problems are batches of the 277 lines from Understudy's completed runs (the Runway clip gates and five MuJoCo evaluations). The gold term comes from the structured fields behind each line, never from the text. A problem passes at 90% of its lines right. Rebuild from an Understudy checkout:

```bash
python3 tasks/understudy/build.py ../understudy-replay
```

For an LLM instead of a VLA there is no Runway step: connect the agent's own transcripts as a set, the same way.

### Your own agent or task

Drop a folder with a `set.json` into `tasks/` and it appears in the dropdown. The file names the prompt, the grading command and, if you want, your own agent command in place of Claude. `tasks/README.md` has the format.

### Aider Polyglot, C++ and Python

The practice problems from the Aider Polyglot benchmark, graded by each problem's own tests.

## Keeping it honest

- The tests that grade are always the originals. Test files are hashed before the agent starts and restored before grading. A changed hash is recorded as `edited_tests` and that version is pruned.
- For the nursery questions the expected answer never enters the agent's folder.
- Practice and held-out problems are separate, and a change is judged on held-out alone.
- A new set of held-out problems starts a new tree, because scores on different problems cannot be compared.

## Files

| Path | What it is |
|---|---|
| `harness/evaluate.py` | Runs one harness on a batch of problems and grades them. The agent may not edit it. |
| `harness/grade_mongo.py` | Grades one nursery answer against MongoDB. |
| `harness/grade_exact.py` | Grades a one-line answer for the legal, health and finance sets. |
| `raindrop_audit.py` | Recounts every version's score from Raindrop's record. |
| `loop.py` | The loop: run, propose, score, keep or prune, record. |
| `server.py` | The local app: serves the page and starts or stops runs. |
| `web/index.html` | The page: the bonsai, the agents list, the lineage. |
| `web/benchmarks.html` | Score charts for every set. |
| `web/views/` | The live views. `gen/` holds views a model wrote for new task sets. |
| `runs/*-versions.jsonl` | The record of each run, one line per version. |
| `deploy/` | A hosted copy of the page that mirrors a local run through Atlas. |
| `tasks/mongo/` | The nursery question set and its builder. |
| `versions/` | One JSON file per harness version. |
| `AGENT.md` | The same loop written as instructions for an agent to follow by hand. |
| `DESIGN.md` | Notes on the page for whoever works on it next. |

## Credits

The layout (a fixed evaluator, a folder of candidates, an append-only record) follows [MaxRobinsonTheGreat/fractalsearch](https://github.com/MaxRobinsonTheGreat/fractalsearch), which adapts [karpathy/autoresearch](https://github.com/karpathy/autoresearch). The question sets are built from [LegalBench](https://huggingface.co/datasets/nguha/legalbench), [MedQA](https://huggingface.co/datasets/GBaker/MedQA-USMLE-4-options) and FinQA. The coding problems are from the [Aider Polyglot benchmark](https://github.com/Aider-AI/polyglot-benchmark), drawn from Exercism. Traces go to [Raindrop](https://raindrop.ai).
