# Bonsai Chess Ladder RSI: final spec

Status: judged synthesis of the three designs (robust-minimal, max-learning, measurement-rigor).
Base: **max-learning's packaging** (a new `rsi/` package, with zero edits to existing files), cut down to **robust-minimal's scope**. Grafted from measurement-rigor: a pinned model id, a frozen run config (prereg-lite), opening-family splits, an A/A control and a small paired final check.

## 0. Facts re-checked against the repo today (2026-10-11 14:39)

- HEAD is `4e0ca4a`. The "uncommitted edits by someone else" are now committed. `git status` shows only `.claude/` and `versions/g3b.json` untracked. The research quoted line numbers from `f9701a6`, so re-read files before porting.
- `server.py` is running (pid 40969), with 4 live `tasks/chess/_agent.py` games. **Any edit to `tasks/chess/_agent.py`, `_grade.py`, `set.json` or `build.py` would change the user's live job mid-run.** This rules out robust-minimal's in-place edits.
- `loop.store()` already upserts Mongo on `{id, run}`, with run = the seed's ts. The reliability research said `{id}` only; that is stale. The torn-line issue is real: `json.loads` runs on every line with no guard.
- The replay viewer serves only `/replays/<set>/<version>/<game>.json`, where every part is `[\w-]+`. Max-learning's nested `_replays/<RUN_ID>/<vid>/` and any seed containing `|` would **not** be viewable. Use a flat dir `tasks/chess/_replays/<RUN_ID>-<vid>/<family>-<color>-<seedhash>.json`.
- `server.py`'s heavy lock is `~/helloworld/.heavy-lock` (mkdir+pid). The RSI run uses its own flock, `~/.bonsai/heavy.lock`, and only *reads* the server's lock as a warning.
- CLI `/opt/homebrew/bin/claude` is 2.1.236 and rejects `claude-opus-5-5`. A stale `~/.local/bin/claude` 2.0.37 is also on PATH, so the CLI path must be pinned by absolute path. The engine is Stockfish 19.
- Power: AC, battery 23% and charging. Load is about 4.

## 1. Scores

| Design | Reliability | Learning signal | Honesty | Build effort (10 = cheap) | Repo fit | **Overall** |
|---|---|---|---|---|---|---|
| robust-minimal | 7 | 6 | 7 | 7 | 5 | **6.5** |
| max-learning | 8 | 8 | 7 | 4 | 8 | **7.0** |
| measurement-rigor | 8 | 6 | 9 | 3 | 8 | **6.5** |

**robust-minimal: concrete flaws**
- It edits `tasks/chess/_agent.py`, `build.py`, `set.json`, `evaluate.py` and `loop.py` while the user's server job is using them. Changing `set.json`'s seed or `build.py`'s game folders breaks the running job and the page's existing runs.
- It runs games through `evaluate.run_one`, which copies a folder and spawns `uv run` and then a grader per game. That adds overhead and another layer of timeouts and orphans, with no gain for an RSI-only driver.
- The cap is 80 moves, but the fractional adjudication table was measured at the move-100 cap. Below 600 Elo, 49% of games reach move 100, so a lower cap adjudicates even more games with an uncalibrated table.
- Rollback is only "P(C worse) ≥ 0.9 by likelihood". With 24 games per side it can only catch drops of about 150 Elo or more. It says so, but then treats it as protection.
- The budget assumes about 45 calls per game. The evidence gives a 57-agent-move median at 600–1200 and up to 100 below 600.

**max-learning: concrete flaws**
- Its replay path `_replays/<RUN_ID>/<vid>/` is not servable by the viewer regex.
- It is too big for 2–4 hours: samples or voting (unproven; 4/4 identical samples in the test), verify calls, a cost penalty, conversion_failure, reason-vs-reality tags, auto-shed, prescreen and exam. That is a multi-day build.
- The synthetic dry run expects Elo "100 to >800 in 8 generations". That tests the fake proposer, not the loop, and costs about 25 minutes. Keep it, with a smaller pass bar.
- The 0.5 win-point cost penalty per extra call is a guess, as it admits.
- Overnight it plans about 20k calls on 4 workers while the user's server job also runs. That will probably hit the subscription window.

**measurement-rigor: concrete flaws**
- Final exam: 192 games, about 5 h, about $35. A/A game control: about 1.6 h, about $12. Total run: 16–17 h, $110–130. That does not fit "run on this computer without issues" overnight, and it doubles the usage-limit exposure.
- The stage-2 "child replays C's 12 rate slots exactly" pairs only the opponent. Haiku is stochastic, so pairing removes little variance at n = 12, and GSPRT at 12 + 12 games cannot resolve +100 Elo (research: about 97 per arm).
- Three splits with 6 families each give thin exam banks near the current rung. The select bank of the "120 sharpest" positions from 12 rate games (about 700 agent moves) may not reach 120 sharp positions early on.
- The build has about 13 steps. That is too long for 2–4 hours.

**Shared problem, which no design resolves fully.** Research found exam move-loss of 21–27 against about 7 in game. The likeliest cause is that in-game moves used uncapped default thinking while the exam ran with `--tools ""` and a different thinking setting. Every design turns thinking off by default, and research saw a think-0 haiku give away its queen. The seed baseline therefore **must be measured with the new interface**, and the exam has to be validated against games under the *same* call settings before it is allowed to gate (step 9).

## 2. Final design

### 2.1 Principles
1. Zero edits to existing repo files. Everything lives in a new `rsi/` package plus `runs/rsi/<RUN_ID>/` and `tasks/chess/_replays/<RUN_ID>-<vid>/`, built on branch `rsi-ladder`.
2. The interface is frozen before the seed is rated: SAN-or-UCI parsing, json-schema output, clean env, pinned model id, fixed tool definitions. The proposer can change only harness content.
3. Infra is never chess. Every game is `complete | infra`, and infra never reaches a rating, digest, prune or forfeit count.
4. Every step is resumable from files, and no LLM work is bought twice.
5. Gates are cheap and paired (exam). Claims come from ladder games plus a small paired final check on fresh seeds.

### 2.2 Files (ported from scratch unless marked NEW)

| File | Source | Content |
|---|---|---|
| `rsi/reliable.py` | reliability/reliable.py | `ask_claude` (allowlisted env, `MAX_THINKING_TOKENS`, `--safe-mode --tools '' --no-session-persistence --disable-slash-commands --strict-mcp-config --mcp-config '{"mcpServers":{}}' --json-schema`, stdin=DEVNULL, `start_new_session` + killpg, classify ok/timeout/cli_crash/api_error/rate_limit/usage_limit/auth with `is_error` checked even when subtype=success, backoff 5/20/60 s), `atomic_write_json`, `append_jsonl`/`read_jsonl` (flock, torn tail), `RunLock`, `Stopped` |
| `rsi/ladder.py`, `rsi/elo.py`, `rsi/rating.py`, `rsi/ladder.json` | ladder/ | `Bot(spec, seed)` softmax over depth-1 scores, Threads=1, ucinewgame per move, restart ×2 then infra; `estimate()` (performance MLE + profile 95% CI), `rung_for_elo()`, `next_target()`. `ladder.json` gains `stockfish_version` |
| `rsi/perceive.py` | rsi/perceive.py | Tier-1 tools: `legal_moves`, `pieces`. Tier-2 tools: `threats`, `hanging`, `checks`, `pins`. Tier 3 is not importable from `player.py` |
| `rsi/player.py` | rsi/player.py | `choose(board, harness, ctx) -> {move, reason, attempts[], calls, cost, think_tokens, latency}`; json-schema `{move, reason≤25 words}`, parses UCI or SAN, up to 3 chess attempts (illegal/format), infra retried separately, `InfraAbort` |
| `rsi/game.py` | ladder/example_game.py + reliability/safe_agent.py | `play_game(harness, slot, out_dir)`: opening line, Bot opponent, auto draw claims, cap at move 100 / 1500 s, fractional `ADJUDICATION`, per-ply checkpoint `game-state.json` with resume, atomic record in `finally` + SIGTERM handler, viewer-compatible replay copy |
| `rsi/openings.json` (NEW, small) | — | 16 ECO families, 4–8-ply seeded book lines: **12 train** and **4 held**, both colours |
| `rsi/mine.py` | rsi/mine.py | 100k nodes, Threads=1, nice 10; win%-loss class, motifs with pre/post visibility, rejected-reply causes; train games only (assert) |
| `rsi/bank.py` | rsi/make_bank.py | Held-game positions with loss ≥ 10, or sharp positions → `bank.jsonl`, plus an eval cache. The first 120 built after the seed are frozen as `anchor.jsonl` |
| `rsi/propose.py` | rsi/propose.py | Typed ONE-field change + `expected_effect{motif, from, to}` + lint, re-asked once on a lint failure |
| `rsi/exam.py` | rsi/exam.py | Staged paired exam, fresh interleaved answers, void handling, bootstrap P(better) |
| `rsi/run.py` (NEW) | rsi/rsi_loop.py sketch | Generation driver, phases checkpointed in `state.json`, events in `events.jsonl`, `export()` to a loop-shaped `versions.jsonl` |
| `rsi/doctor.py`, `rsi/supervise.py` | reliability/ | Preflight checks; detached runner (flock, `caffeinate -dimsu -w`, stall watchdog 900 s, backoff 30/120/600 s, exit 75 = sleep and retry) |
| `rsi/tests/*` | reliability/test_*.py, fake_claude.py, repro.py, crashtest.py | All run offline |

`run.json` (frozen per run and hashed into `state.json`; a change means a new RUN_ID) holds:
- the absolute CLI path and version
- player model `claude-haiku-4-5-20251001`
- the proposer model: `opus`, resolved id recorded from the canary
- the ladder sha and Stockfish version
- the splits and the seed root
- all thresholds, game counts and workers
- `max_usd` and `max_hours`

### 2.3 Harness v2 (what RSI may change)
```json
{"id":"g3a","parent":"g2a","model":"claude-haiku-4-5-20251001",
 "rules":"<=1200 chars","lessons":[{"id":"L2","text":"<=220 chars","motif":"hung_other_piece","gen_added":2,"rate_at_add":3.2}],
 "tools":["legal_moves"],"think":0}
```
- **Mutable, exactly one per child:**
  - `rules`
  - one lesson (add, edit or retire; at most 8)
  - one tool toggled (tier ≤ 2)
  - `think`, one step within {0, 1024, 2048}
- **Frozen:** model, parser, opponent, grader, openings, ladder.
- **Deferred to v2:** samples/voting and the verify call (unproven, and they double cost).
- **Lint rejects:**
  - square names, SAN moves, family or opening names in rules or lessons
  - unknown tools
  - more than one field changed
  - an exact repeat of a pruned change
- **Think policy:**
  - A think increase must win the exam by ≥ 1.0 win-point per +1024 (cost guard).
  - Every Elo is labelled with think and tool tier.
- **Seed harness:**
  - the existing set.json rules
  - `tools:["legal_moves"]`, part of the frozen interface fix because it removes most impossible-geometry replies
  - `think` = user's choice, default 0

### 2.4 Ladder and opponent choice
- Rungs come from `ladder.json`:
  - random 32
  - **E100 93 (start)**
  - T8 185, T5.7 282, T4 395, T2.8 512, T2 709
  - T1.6 829, T1.25 963, T1 1127, T0.75 1300, T0.5 1458
  - T0.25 1613, d1 1706
  - sf2100→1830, sf2500→2094, sf2900→2442
- Any target uses `rung_for_elo`. The scale is labelled "Bonsai ladder Elo (Stockfish-anchored, sf1320 = 1320; not lichess/chess.com)".
- **Bootstrap:**
  - Start at target 100.
  - While the champion scores ≥ 0.9 over ≥ 8 games at the target, double it: 100 → 200 → 400 → 800 → 1200, then steps of 100.
  - Reported as **calibration**, not improvement.
- **Batch of games per generation, by expected score:**
  - 60% at expected score 0.5 (target = estimate)
  - 20% at estimate +150
  - 20% at estimate −150
- **Rating:** `estimate()` over the champion's last 60 complete games, with rung Elos fixed.
- **Milestone:** "beats rung X" is logged when the CI lower bound > Elo(X) with ≥ 16 games at or above X.
- **Demotion:** the target follows the estimate. If the CI upper bound falls below the target after ≥ 8 games, the target resets.
- **Fairness:** Stockfish is only the opponent, the post-game coach and the exam scorer. It is never a play-time tool.
- **Pinning:** doctor FAILs if `stockfish` ≠ `ladder.json.stockfish_version`.

### 2.5 One generation (`rsi/run.py`, every phase checkpointed)
0. **Gate.** Checks: RunLock held; doctor-lite (Stockfish ping, one haiku canary); budget caps not hit. Failure → exit 75.
1. **Play** 24 games with champion C at 3 workers. 16 games are on train families and 8 on held families, both colours. Seed = `sha1(RUN_ID, gen, slot)[:10]`.
   - Each game writes `games/<vid>/<slot>.json`. On resume, slots with a result are skipped.
   - An infra game is replayed with the same seed (×2), then dropped.
   - If more than 20% of games are void, or 5 infra games in a row → exit 75 and **no proposer call**.
2. **Rate.** Update the estimate, milestones and bootstrap target.
   - If C was swapped in last generation, run the rollback check: P(C worse than P) ≥ 0.9 → restore P and add C's change to the pruned list. This check is coarse and only catches large regressions.
3. **Mine** the train games, complete only. Writes a digest of ≤ 2.5k tokens:
   - motif table with trend against the last 2 generations
   - top 3 motifs × ≤ 2 examples
   - rejected-reply causes
   - lesson ledger (rate at add vs now)
   - pruned list with exam numbers
   - knob costs
   Held games' positions go to `bank.jsonl`, never into the digest. Infra counts never appear in the digest.
4. **Propose** up to 2 children, each a separate proposer call with the pruned list updated in between. Proposer timeout 300 s, 3 attempts. A proposer infra failure → retried next generation, never stored as pruned.
5. **Prescreen**, graft from max-learning:
   - 16 **train-bank** positions tagged with the child's target motif (or the sharpest 16 for a think change).
   - Fresh, interleaved parent and child answers: 32 calls.
   - Kill the child if P(better) < 0.5, or if its unusable-reply rate rises by ≥ 5 points. It is a filter only and never accepts.
6. **Exam** (accept gate), the survivor with the best prescreen first:
   - Positions from the held bank near C's rung, staged looks at 40 / 80 / 120 positions.
   - Keep if P(child better) ≥ 0.99 / 0.97 / 0.90; stop for futility if P < 0.30.
   - A removal-only child is kept if the mean gain ≥ −0.5.
   - More than 10% void → inconclusive, re-queued.
7. **Store.** `versions/<RUN_ID>-<vid>.json` is written *before* scoring. The doc is appended to `runs/rsi/<RUN_ID>/versions.jsonl` in loop.py's shape plus:
   `{run, elo, elo_lo, elo_hi, games, target_elo, exam{n, gain, p}, prescreen{...}, think, tier, rolled_back}`
   `score` = mean game score, so the page works with `BONSAI_LOG` pointed at it.
8. **Every 4 generations:**
   - a shed attempt (remove the lesson with the worst rate drop, removal-only exam)
   - the anchor exam (frozen 120 positions) for the progress curve

### 2.6 Claims
- **Progress curve:** champion ladder Elo with 95% CI per generation, plus anchor-exam mean loss.
- **Headline:** `final.py` plays seed vs final champion on 24 *fresh-seed* held-family slots each. Opponents are identical per slot and games are interleaved A, B, A, B. It reports the ΔElo paired-bootstrap CI.
  - Only "Δ > 0" claims with CI lower bound > 0 are made. Otherwise the report says "not detectable at this budget (about ±150)".
  - The report lists children tried, void rate and the think/tier labels.
- **A/A control:**
  - offline A/A simulation of exam false-accept (≤ 5%)
  - one real seed-vs-seed exam of 40 positions, which must not accept

## 3. Reliability checklist (all of it is in the ported code)
- **Thinking:** `MAX_THINKING_TOKENS` is explicit (fixes the 14–110 s calls and the 120 s timeout death). Timeout per call = 60 + think/50 s.
- **Clean call:** allowlisted env (removes `CLAUDE_EFFORT=xhigh`, the caveman/cavemem hooks via `--safe-mode`, and about 22k tokens of tool and skill text). `DISABLE_AUTOUPDATER=1`, `HOMEBREW_NO_AUTO_UPDATE=1`, `CLAUDE_CODE_MAX_RETRIES=2`, `API_TIMEOUT_MS=60000`.
- **Processes:** process-group kill on every subprocess, so there are no orphans. Stockfish runs with Threads=1, Hash 16, depth-limited only, and is restarted ×2 (catching `EngineTerminatedError` and `CancelledError`).
- **Persistence:** atomic records, checkpoints every ply, flocked JSONL with a torn-tail reader. `state.json` is atomic, and a pending child is re-examined from disk without a new proposer call.
- **Supervision:** run under `rsi/supervise.py` (setsid, detached from server.py, caffeinate, stall watchdog, backoff). A usage limit → sleep until the reset epoch. Auth → stop.
- **Doctor refuses to start if:**
  - not on AC
  - the CLI path or version ≠ `run.json`
  - the canary fails
  - the Stockfish version ≠ ladder
  - free disk < 5 GB
  - `~/.bonsai/heavy.lock` is held
  It warns (does not fail) if server.py's job is active: the run then uses 2 workers.
- **Caps:** `max_usd` and `max_hours` in `run.json`. The run stops cleanly at a phase boundary.

## 4. Build plan (about 3–4 h of coding; then unattended)
See the structured `build_plan`. Order: port + offline tests → player/game → seed smoke → driver dry run → doctor/supervisor → seed rating → exam validity → overnight.

## 5. Budget (this laptop, think 0, 3 workers; 2 workers while server.py's job runs)

| Item | Calls | Wall | API-equiv $ |
|---|---|---|---|
| Ladder calibration | 0 | done (20 min if SF changes) | 0 |
| Offline tests + dry run | 0 | about 15 min | 0 |
| Smoke: 1 real game vs E100 | about 60 | about 8 min | 0.2 |
| Seed rating (bootstrap + 40 games) | about 2,400 | about 85 min | 7 |
| Exam validity (anchor 120 × seed + 16 games at think 0 vs 1024) | about 1,200 | about 45 min | 4 |
| Per generation: 24 games | about 1,450 | about 50 min | 4.5 |
| Per generation: mining | 0 | about 12 min (overlaps) | 0 |
| Per generation: 2 proposals | 2–4 opus | about 1 min | 0.3 |
| Per generation: prescreen 2 × 32 | 64 | about 3 min | 0.2 |
| Per generation: exam (typical, early stops) | about 300 | about 10 min | 1 |
| **Per generation total** | **about 1,800** | **about 65–75 min** | **about $6** |
| Overnight 10 h | about 15k | 8 generations | about $45–50 |
| Final paired check (48 games) | about 2,900 | about 100 min | 9 |

think = 2048 roughly doubles time and cost. On a subscription, roughly 15k calls in 10 h may hit the 5-hour window; the run sleeps and resumes, so expect about 5–6 generations instead.

## 6. Questions for the user (deduplicated, with defaults)
See the structured `user_questions`. No MongoDB, Atlas cluster upgrade or API key is required. Everything runs locally.
