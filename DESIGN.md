# Bonsai: design brief

For the teammate refining the UI. Build day: Sun 2026-10-11, submissions 5:00 PM, 3-minute live demo.

## The idea in one line

A coding agent that rewrites its own harness, drawn as a bonsai: it grows a branch for every change it tries on itself, and prunes the ones that do not earn their place.

## Why a bonsai

Most self-improving agents only grow: more rules, more tools, more context. A bonsai is shaped by cutting. Ours keeps a change only when it scores higher on problems it has never seen and trips no cheating signal. Everything else is cut, and the cut stays visible. The tree is the full history of the agent improving itself.

## What is real underneath

- The agent solves coding problems from Aider Polyglot (graded by tests, pass or fail).
- Every run is traced to Raindrop, which names the failure pattern (for example: said the tests passed without running them; repeated the same edit; gave up early).
- The agent proposes one change to its own harness aimed at that pattern.
- The new version is scored on held-out problems. Higher score and no cheat signal: kept. Otherwise: pruned.
- Every version is a document in MongoDB: id, parent, the change, the signal that caused it, score, tokens, kept or pruned.

## How data maps to the drawing

| Data | Drawing |
|---|---|
| Seed harness | The pot and the base of the trunk |
| Each kept version | One more segment of trunk, climbing in an S curve |
| A kept change | A branch with a foliage pad; pad size follows the score |
| A pruned change | A short bare stub ending in a red cut mark |
| A cheat that was caught | A stub with a heavier red slash and the word "cheat" |
| Current best version | The crown at the top |
| Harness size (tokens) | Overall density of the tree: a leaner harness is a sparser, cleaner tree |
| Time | Bottom to top. Oldest at the roots |

## Layout

One screen, no navigation.

- **Left, about 70%: the scroll.** Paper background, the tree, a pale sun, a red seal in the corner. Nothing else.
- **Right, about 30%: the margin notes.**
  - The number: held-out pass rate now, and what it started at.
  - Generations list: one line per version, pruned ones struck through in red.
  - Blind baseline: the same loop choosing by score alone, as one thin grey line next to ours.
  - Cheats caught: a count.
- **Bottom strip, thin:** what the agent is doing right now (running batch 3 of 5, reading signals, proposing a change, scoring).

A dashboard is banned as the main feature, so the tree must be the screen. The numbers are annotations on a painting, not panels.

## Interaction

- **Hover a branch:** the change in one sentence, the Raindrop signal that caused it, score before and after.
- **Click a branch:** the actual diff of the harness, and one failing trace that motivated it.
- **Click a red cut:** why it was pruned.
- **Scrub bar under the tree:** replay growth from the seed to now.
- No other controls.

## Motion

- A new attempt is brushed in as a stroke, pale at first: it is being tested.
- Kept: the ink darkens and the foliage bleeds in like wet pigment.
- Pruned: one red slash, the branch fades to a stub.
- Keep it slow and quiet. One thing moves at a time.

## The demo, in order

1. The bare seed: a pot, a stump, a low score.
2. Replay: the tree grows through the generations in about 20 seconds.
3. Stop on the cheat. The agent tried to pass by editing the tests. The score went up. Raindrop caught it. Red slash.
4. Click a kept branch: signal, diff, score before and after.
5. End on the crown: start score, end score, the blind baseline beside it.
6. Live: the next generation brushes itself in while we take questions.

## Style

- Japanese ink and watercolor (sumi-e). Warm paper, black ink with visible brush pressure, washes of muted green and indigo for foliage, one red (vermilion) used only for cuts and the seal.
- Asymmetric composition, lots of empty paper. The empty space is part of it.
- Type: a Mincho serif for titles and the big number, a plain monospace for data. No rounded cards, no gradient glow, no icon sets.
- Everything is drawn in code (canvas or SVG brush strokes). No image-model art: it must not look AI-generated.
- It has to read from the back of a room on a projector: one big number, strong silhouette.

## Data the UI reads

One JSON document per version:

```json
{
  "id": "g2a",
  "parent": "g1b",
  "generation": 2,
  "change": "edit the test file",
  "signal": "cheat caught",
  "kept": false,
  "score": 0.63,
  "tokens": 4100,
  "diff": "...",
  "trace_id": "..."
}
```

The page polls for new documents every few seconds and redraws.

## Open questions for you

- Does the tree stay readable past about 15 generations, or do old branches need to thin out?
- Where does the blind baseline live so it reads as a comparison and not a second chart?
- Is the scrub bar worth its space, or should replay be a single button?
- How does the cheat moment get emphasis without breaking the quiet of the page?

## Honest limits

- No run has produced numbers yet. Any figures in a mock are placeholders and must be labelled as such.
- We expect a few generations on 30 to 50 problems, so the tree will be small. Design for 5 to 12 versions first.
