# Bonsai: notes on the page

For whoever works on the UI next. The whole page is one file, `web/index.html`, written in plain HTML, CSS and canvas with no build step. Run `server.py` (see the README) and open http://localhost:8765.

## The idea

A coding agent rewrites its own harness. Each change it tries is a branch. A branch is kept only if the agent scores higher on problems it has not seen, and every change has to shed something. So the bonsai prunes itself as it grows, and the tree on screen is the whole history.

The hackathon rules ban a dashboard as the main feature. Keep the tree as the largest thing on the page and keep the numbers small beside it.

## What is on the page now

| Part | Where in the file | What it does |
|---|---|---|
| Ask bar (`#ask`) | top | Dropdown of what to improve on, number of generations, an optional line to steer the proposer, Grow and Stop. |
| Status line (`#phase`) | under the ask bar | What the run is doing right now. Red dot while running. |
| Agents (`#agents`) | left, top | Every agent in the run: solvers (one per problem) and the proposer. Working, queued, passed, failed, edited tests. A green ◆ means the attempt has a Raindrop trace. |
| Lineage (`#rows`) | left, bottom | One row per version, newest first. Pruned versions are red and struck through. Click to select. |
| Selected version (`#viewer`) | right | Layers: tree, diff, signal, harness, activity. |
| Tree canvas (`#c`) | right | `draw()` paints it: `paper()`, `stroke()`, `wash()`, `pad()`. |
| Falling needles (`#fx`) | over the tree | `tick()`: needles drop from every kept branch that shed something. |

## How data maps to the drawing

| Data | Drawing |
|---|---|
| Seed harness | The pot and the base of the trunk |
| Each kept version | One more segment of trunk, plus a branch with a foliage pad |
| Pad size | Held-out score |
| A pruned change | A bare stub with a red cut |
| A cheat that was caught | A stub with a heavier red slash |
| Something shed | Needles falling from that pad and settling on the ground |

## Where the data comes from

- `GET lineage.json`: `{"versions": [...]}`, written by the loop after every version. Each version has `id, parent, generation, change, shed, signal, kept, score, tokens, harness, diff, trace_id`. Until a run has produced one, the page shows the `SAMPLE` tree and says so.
- `GET api/state`: `{job, agents, log, sets, raindrop}`, polled every second. `sets` fills the dropdown. A set that cannot be graded on this machine comes back with `ready: false` and a reason.
- `POST api/grow` with `{set, goal, generations}` starts a run. `POST api/stop` ends it after the current step.

## Style

Sumi-e on paper. The palette is ink black, one vermilion red and a moss green for needles. Words are set in Shippori Mincho and data in IBM Plex Mono. Please do not add cards, gradients or icons.

## Good next things

- Brush a new branch in pale while it is being scored, then darken it when kept or slash it when pruned.
- A scrub bar that replays growth from the seed.
- Hover a branch for its change, signal and score.
