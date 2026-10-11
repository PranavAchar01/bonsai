# Bonsai

A coding agent that rewrites its own harness, and has to cut something every time it adds something.

Each change the agent tries on itself is a branch. A branch stays only if the agent then scores higher on problems it has never seen. The rest are pruned, and the cuts stay in the record. The page draws that history as a bonsai.

Built on 11 October 2026 at the MongoDB Harness Engineering hackathon in San Francisco.

## The loop

A harness is a small JSON file holding the rules (the system prompt), the tools the agent may use, the turn limit and the model. The model never changes. The rest is open to revision.

1. Run. The current best harness attempts the practice problems. Every attempt goes to Raindrop as a trace with a `tests_passed` or `tests_failed` signal, plus `edited_tests` if the agent touched a test file.
2. Propose. A second model reads the failures and writes exactly one change to the harness. Whatever it adds, it must remove something of about the same size, such as a rule, a tool or some turns. The proposal is traced to Raindrop too.
3. Score. The new harness runs on held-out problems. The proposer sees that score and never the failures behind it.
4. Keep or prune. A child with a higher held-out score and no tampering is kept and becomes the new best. Any other child is pruned. The proposal's Raindrop trace is marked `kept` or `pruned`.
5. Record. Every version, kept or pruned, is one document: id, parent, the change, what was shed, the failure pattern, score, the harness itself and the diff. They go to `runs/versions.jsonl`, and to the MongoDB collection `bonsai.versions` when `MONGODB_URI` is set.

## Run it

You need [uv](https://docs.astral.sh/uv/), the `claude` CLI signed in, and a C++ compiler for the C++ set.

```bash
git clone https://github.com/Aider-AI/polyglot-benchmark bench
```

```bash
uv run --no-project --with raindrop-ai --with python-dotenv python server.py
```

Open http://localhost:8765. Pick what the agent should improve on, type a line to steer it if you want, and press Grow. While it works, the page lists every agent in the run: one solver per problem on the model being improved, and the proposer that rewrites the harness. Stop ends the run after the current step.

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

### Aider Polyglot, C++ and Python

The practice problems from the Aider Polyglot benchmark, graded by each problem's own tests.

## Keeping it honest

- The tests that grade are always the originals. Test files are hashed before the agent starts and restored before grading. A changed hash is recorded as `edited_tests` and that version is pruned.
- For the nursery questions the expected answer never enters the agent's folder.
- Practice and held-out problems are separate, and a change is judged on held-out alone.
- A new set of held-out problems starts a new tree, because scores on different problems cannot be compared.

## Baselines

Measured on the seed harness (three tools, twelve turns, Claude Haiku):

| Set | Passed |
|---|---|
| Aider Polyglot, Python | 34 of 34 |
| Aider Polyglot, C++ | 24 of 26 (both misses need Boost, which this machine does not have) |

The seed already clears both Polyglot sets, so there is little left to gain there. We wrote the nursery questions to have a set with room to improve.

## Files

| Path | What it is |
|---|---|
| `harness/evaluate.py` | Runs one harness on a batch of problems and grades them. The agent may not edit it. |
| `harness/grade_mongo.py` | Grades one nursery answer against MongoDB. |
| `loop.py` | The loop: run, propose, score, keep or prune, record. |
| `server.py` | The local app: serves the page and starts or stops runs. |
| `web/index.html` | The page: the bonsai, the agents list, the lineage. |
| `tasks/mongo/` | The nursery question set and its builder. |
| `versions/` | One JSON file per harness version. |
| `AGENT.md` | The same loop written as instructions for an agent to follow by hand. |
| `DESIGN.md` | Notes on the page for whoever works on it next. |

## Credits

The layout (a fixed evaluator, a folder of candidates, an append-only record) follows [MaxRobinsonTheGreat/fractalsearch](https://github.com/MaxRobinsonTheGreat/fractalsearch), which adapts [karpathy/autoresearch](https://github.com/karpathy/autoresearch). The coding problems are from the [Aider Polyglot benchmark](https://github.com/Aider-AI/polyglot-benchmark), drawn from Exercism. Traces go to [Raindrop](https://raindrop.ai).
