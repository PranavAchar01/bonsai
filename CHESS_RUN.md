# Chess run for Nithin

The chess tree has not been grown yet. Three attempts on Pranav's Mac died: one when Claude Code updated itself mid-run, one to a server restart, and one because the machine was overloaded and every game hit the 10 minute limit a few seconds before it ended. That last problem is fixed on master: `tasks/chess/set.json` now gives each game 30 minutes.

Nothing is running on Pranav's machine now, so this run is yours. Results on GitHub by 3:45 PM PT. Submission is 5:00 PM.

## What the run is

- 20 games against Stockfish at skill 0, ten openings, each played as white and as black, up to move 18.
- A game passes if the agent wins, draws, or reaches move 18 without forfeiting and without being behind on material.
- The player is `haiku`, one call per move, and stays `haiku`. The proposer is `claude-opus-5-5`, which is the default.
- Black games are practice, white games are held out. A change is kept only if the held-out score goes up.

## Setup

```bash
git pull origin master
brew install stockfish
```

You also need `uv` and a signed-in `claude` command, since the agents run through it.

Check the machine before you start. Each game is 18 model calls plus Stockfish, and on a loaded laptop they crawl. Close anything heavy, and use `--workers 2` if four is slow.

## Run it

One game first, to see that it finishes and gets a grade:

```bash
uv run --no-project --with raindrop-ai --with python-dotenv python -m harness.evaluate --tasks chess/game-italian-white --workers 1 --scratch /tmp/bonsai-chess --out runs/chess-one.json
```

It should take 5 to 10 minutes. If the result says `agent timeout`, stop and tell Pranav. Do not start the loop on top of that.

Then the tree:

```bash
BONSAI_LOG=runs/chess-versions.jsonl uv run --no-project --with raindrop-ai --with python-dotenv python loop.py \
  --practice chess/game-caro-kann-black chess/game-english-black chess/game-french-black chess/game-italian-black chess/game-kings-indian-black chess/game-london-black chess/game-queens-gambit-black chess/game-ruy-lopez-black chess/game-scandinavian-black chess/game-sicilian-black \
  --heldout chess/game-caro-kann-white chess/game-english-white chess/game-french-white chess/game-italian-white chess/game-kings-indian-white chess/game-london-white chess/game-queens-gambit-white chess/game-ruy-lopez-white chess/game-scandinavian-white chess/game-sicilian-white \
  --generations 3 --workers 4 --scratch /tmp/bonsai-chess
```

Each step is 10 games, so expect 20 to 30 minutes per step and about two hours for three generations. If time runs short, use `--generations 2`.

Do not run `server.py` on the same clone while the loop is going. It uses the default log and would collide with yours.

## If the seed scores too high or too low

The loop only has something to do when the seed fails some games. The one earlier seed batch that finished cleanly had mixed results, so this should be fine. If the seed comes back at 90% or more, or at 10% or less, stop and tell Pranav the number before spending two hours on it.

## What to push

```bash
git add tasks/chess/_replays versions
git add -f runs/chess-versions.jsonl runs/chess-one.json
git commit -m "Chess tree run"
git push origin master
```

- `runs/chess-versions.jsonl` is the tree: every version, kept or pruned, with its score, change and diff.
- `tasks/chess/_replays/<version>/` holds every game move by move. Kabir's 3D replay reads these.

Put one line in the commit body: seed held-out score, best kept held-out score, how many changes were kept and how many pruned. For example: `chess: seed 50%, best 70%, 2 kept, 4 pruned`.

## Keeping the numbers honest

- Do not change the pass rule, the Stockfish level or the player model after you have seen results.
- Report the run even if nothing was kept.
- If a game errors for a reason that is not chess (timeout, crash, rate limit), say so in the commit and do not count it as a loss.
