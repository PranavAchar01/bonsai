# AGENT.md: the Bonsai loop

You are improving a coding agent by rewriting its harness. You are not solving the benchmark
problems yourself. Run this loop until told to stop.

## What you may and may not touch

- `harness/` is fixed. It runs a harness version on Aider Polyglot problems and grades with the
  problems' own tests. Never edit it. Never edit anything under `bench/`.
- `versions/` is yours. Each file is one harness: `{"id", "rules", "tools", "max_turns", "model"}`.
  `model` never changes. `tools` is a subset of Read, Edit, Write, Bash, Glob, Grep. `max_turns` is at most 30.
- `splits.json` names the practice problems and the held-out problems. You may read practice
  results in full. You see only the held-out score, never held-out failures.

## One generation

1. **Run.** Score the current best version on the practice problems:
   `uv run --no-project --with raindrop-ai --with python-dotenv python -m harness.evaluate --harness versions/<best>.json --tasks <practice> --scratch <dir> --out runs/<best>-practice.json`
   Every problem is traced to Raindrop as a `polyglot_task` event with a `tests_passed`,
   `tests_failed` or `edited_tests` signal.
2. **Read the signals.** Ask Raindrop (MCP) what the failing traces for this version have in
   common. Name the pattern in a few words, for example "stops before the hard cases" or
   "claims success without checking". Use the pattern, not a single problem.
3. **Propose one change.** Write `versions/g<N><letter>.json` with exactly one change to the
   parent aimed at that pattern. Never mention a specific problem in the rules.
4. **Score on held-out.** Same command with the held-out problems.
5. **Keep or prune.** Keep the child only if its held-out score is strictly higher than the
   parent's and no `edited_tests` signal fired. Otherwise it is pruned. A pruned branch is never
   deleted: it stays in the record with the reason.
6. **Record.** Insert one document into MongoDB `bonsai.versions`:
   `{id, parent, generation, change, shed, signal, kept, score, tokens, harness, diff, trace_id, ts}`.
   `trace_id` is one failing Raindrop trace that motivated the change.
7. **Notebook.** Add two lines to `notebook.md`: what you tried, what happened.

Try at most two children per generation. If both are pruned, move on from the same parent with
the pruned changes listed so they are not repeated.

## Rules of the garden

- Every change sheds. Whatever a child adds, it must also remove something of similar size
  (a rule line, a tool, turns) that the failures show is not earning its place. Record it as `shed`.
- Growth is not the goal. A shorter harness with the same score is a better harness: when two
  children tie above the parent, keep the one with fewer tokens.
- A score that rises because the tests were touched is a cheat. It is pruned and recorded as
  `signal: "cheat caught"`.
- Never tune on held-out. If you learn anything about a held-out problem, say so in the notebook.

`python loop.py` runs the same loop unattended with a proposer model, without the Raindrop
reading step. Layout follows MaxRobinsonTheGreat/fractalsearch, itself adapted from
karpathy/autoresearch: a fixed evaluator, a folder of candidates, an append-only record.
