# Connecting your own agent or task

Any folder in `tasks/` that has a `set.json` shows up in the page's dropdown under "Connected agents and tasks". Bonsai then grows a harness for it the same way it does for a benchmark. `tasks/chess/` (its own agent, playing Stockfish) and `tasks/mongo/` are working examples.

## Layout

```
tasks/chess/
  set.json
  _shared/            optional, copied into every problem's working folder
  mate-in-two/        one folder per problem
    .docs/instructions.md
    expected.json     listed in "hidden", so the agent never sees it
  fork-the-queen/
    ...
```

Folders whose names start with `_` are not problems. You need at least four problems, because half are used for practice and half are held out.

## set.json

```json
{
  "name": "Chess puzzles",
  "prompt": "Read .docs/instructions.md and write your move to answer.txt.",
  "solution": ["answer.txt"],
  "hidden": ["expected.json"],
  "grade": "python {root}/tasks/chess/_grade.py answer.txt {task}/expected.json",
  "agent": "python {root}/tasks/chess/_agent.py"
}
```

| Field | What it does |
|---|---|
| `name` | The label in the dropdown. |
| `prompt` | What the agent is told for every problem. |
| `hidden` | File names that are kept out of the agent's working folder. Put answers here. |
| `grade` | A shell command run in the working folder after the agent finishes. Exit code 0 means the problem passed. `{task}` is the problem's source folder and `{root}` is the repo. |
| `agent` | Optional. Leave it out and the problem is solved by Claude running under the harness. Set it to a shell command to bring your own agent. |
| `needs_port`, `needs` | Optional. The set is greyed out, with the `needs` text as the reason, until something is listening on that local port. |

## Bringing your own agent

When `agent` is set, Bonsai runs that command in the problem's working folder with three environment variables:

- `BONSAI_HARNESS`: path to the harness JSON for this version (`rules`, `tools`, `max_turns`, `model`). Your agent should read its instructions from `rules`, because that is the part Bonsai rewrites.
- `BONSAI_PROMPT`: the set's prompt.
- `BONSAI_TASK`: the problem's source folder.

Whatever your agent prints last is shown on the page next to its row. After it exits, `grade` decides pass or fail.

## Try it without the page

```bash
uv run --no-project --with raindrop-ai --with python-dotenv python -m harness.evaluate --tasks chess/mate-in-two chess/fork-the-queen --scratch /tmp/bonsai --out runs/chess-seed.json
```

## The chess set

`tasks/chess/` has 20 games against Stockfish at skill level 0: ten openings, each played once as White and once as Black. `_agent.py` plays a game with one model call per move, and the harness's `rules` are the system prompt for every move. A game passes if the agent wins, draws, or reaches move 18 without an illegal-move forfeit and not behind on material. `_grade.py` replays the record to check that.

It needs Stockfish on the path:

```bash
brew install stockfish
```

### Replays for the 3D render

Every game played is saved to `tasks/chess/_replays/<harness version>/<game>.json`, which is committed so the whole team has it:

```json
{
  "game": "game-italian-white",
  "harness": "g0",
  "agent_plays": "white",
  "moves": ["e2e4", "e7e5", "g1f3"],
  "san": ["e4", "e5", "Nf3"],
  "final_fen": "...",
  "forfeit": null
}
```

`moves` is the whole game from the starting position in UCI, opening included, so a renderer can replay it without knowing any chess rules beyond moving a piece from one square to another (plus castling, en passant and promotion). The same game under two harness versions, for example `_replays/g0/` next to `_replays/g2a/`, is the before and after for a montage.
