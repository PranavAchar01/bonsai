# Connecting your own agent or task

Any folder in `tasks/` that has a `set.json` shows up in the page's dropdown under "Connected agents and tasks". Bonsai then grows a harness for it the same way it does for a benchmark. `tasks/mongo/` is a working example.

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
