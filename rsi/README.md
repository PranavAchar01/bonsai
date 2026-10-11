# rsi: chess ladder recursive self-improvement

A fixed player model (haiku, one `claude -p` call per move) plays an Elo ladder of Stockfish-based opponents,
starting near Elo 100. After every batch of games Stockfish marks its mistakes, an Opus proposer makes ONE edit to
the player's harness (rules, one lesson, one tool, or the thinking budget), and a paired exam on held-out positions
keeps the edit or prunes it. The opponents follow the player's rating upward. The full design is in `SPEC.md`.

Nothing here edits the rest of the repo. Everything a run writes goes to `runs/rsi/<RUN_ID>/` and
`tasks/chess/_replays/<RUN_ID>-<vid>/`.

All commands below run from the repo root. `$PY` is shorthand for:

```sh
PY="uv run -q --no-project --with chess python"
```

## 1. Check the machine (doctor)

```sh
$PY -m rsi.doctor            # no model calls
$PY -m rsi.doctor --call     # also a haiku canary and an opus canary (about $0.01)
```

It checks AC power, the pinned CLI (`/opt/homebrew/bin/claude`, the version in `rsi/run.json`, the light-call
flags), the Stockfish version against `rsi/ladder.json`, free disk of at least 5 GB, and `~/.bonsai/heavy.lock`.
Each check prints OK, WARN or FAIL; any FAIL means do not start. If `server.py` is running a job you get a WARN,
and the run uses 2 workers instead of 3.

## 2. Try the whole loop offline first (dry run)

```sh
$PY -m rsi.run --dry --run dry1 --generations 3 --games 8
```

`--dry` makes no model calls. The player is a ladder Bot whose strength is a `temp` knob in the harness. The
proposer is a stub: its first child lowers `temp` (stronger) and its second raises it (weaker). Stockfish, the
games, mining, the bank, prescreen, exam, store and export are all real. On this laptop it takes about 5 minutes.

## 3. Rate the seed

```sh
$PY -m rsi.supervise --run seed1 --detach --args "--seed-only"
# or in the foreground, unsupervised:
$PY -m rsi.run --run seed1 --seed-only
```

Generation 0 is a **calibration**, not an improvement:

- The seed starts at target 100 (rung E100).
- While it scores at least 0.9 over at least 8 games, the target doubles: 100, 200, 400, 800, 1200, then +100 a
  step.
- It then plays `games.seed_rating_games` (40) games at its estimate.
- No proposals are made.

The seed doc `g0` in `versions.jsonl` carries `elo`, `elo_lo` and `elo_hi`.

## 4. The overnight run (supervised)

```sh
$PY -m rsi.doctor --call && \
$PY -m rsi.supervise --run night1 --detach --args "--generations 8"
```

The supervisor runs `uv run ... python -m rsi.run --run <ID> <args>`. Every RUN_ID rates its own seed as
generation 0 first, so `--generations 8` means the seed rating plus 8 generations. To build on the seed you
already rated in step 3, reuse its RUN_ID: `--run seed1 --args "--generations 8"`.

The supervisor:

- takes `~/.bonsai/heavy.lock`, so a second supervisor is refused
- runs `caffeinate -dimsu -w <its pid>`
- starts `rsi.run` in its own session

It acts on how the run ends:

| What happens | What the supervisor does |
|---|---|
| exit 0 | done |
| exit 75 (usage limit, failed gate, too many void games) | sleeps until the usage-limit reset (or 10 minutes), then resumes |
| exit 77 (auth failure, `run.json` changed) | stops |
| a crash | restarts after 30, then 120, then 600 seconds |
| `events.jsonl` silent for 900 s | kills the whole process tree and restarts |

To stop a run, run `kill <supervisor pid>`. A restart resumes from `state.json`, and no game, proposal or exam
answer is bought twice.

The caps `max_usd` and `max_hours` in `rsi/run.json` stop the run cleanly at a phase boundary.

`rsi/run.json` is copied into the run directory at the first start and hashed. To change a threshold, start a new
RUN_ID. Small overrides at the first start look like `--set games.per_gen=16` or `--games 16`.

## 5. Where the results go

`runs/rsi/<RUN_ID>/` (git-ignored) contains:

| File | Contents |
|---|---|
| `run.json` | the frozen config this run uses |
| `state.json` | the current generation and phase, the champion, the rating state, children, the pruned list |
| `events.jsonl` | one line per event: `phase_start`/`phase_end` for gate, play, rate, mine, propose, prescreen, exam and store; `game_end`, `proposal`, `exam`, `swap`, `rollback`, `milestone`, `pause`, `budget_stop`, `done` |
| `versions.jsonl` | one doc per harness, in loop.py's shape plus `run, elo, elo_lo, elo_hi, games, target_elo, exam, prescreen, think, tier, rolled_back, anchor_loss` |
| `versions/<RUN_ID>-<vid>.json` | every harness, written before it is scored |
| `games/<vid>/<slot>.json` | full game records |
| `digests/g<N>.md` | what the proposer saw |
| `exam/` | answer caches and results |
| `bank.jsonl`, `anchor.jsonl` | held-out exam positions, and the frozen anchor set |
| `supervise.jsonl`, `run.log` | the supervisor's decisions and the run's output |

To follow a run as it goes:

```sh
tail -f runs/rsi/night1/events.jsonl | grep -v '"ply"'
```

To see the lineage on the web page:

```sh
BONSAI_LOG=runs/rsi/night1/versions.jsonl python server.py
```

## 6. Watch the games

The replays are viewer-compatible. Each game is at:

```
tasks/chess/_replays/<RUN_ID>-<vid>/<family>-<color>-<hash>.json
```

- With `server.py` running, open `http://localhost:8765/replays/chess/<RUN_ID>-<vid>/<file>.json`.
- Or serve the repo root and open `web/replay.html`, which reads `tasks/chess/_replays`.

## Tests (offline, no model calls)

```sh
uv run -q --no-project --with chess --with pytest python -m pytest rsi/tests -q
```
