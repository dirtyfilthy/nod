# Experiments

A scratch space for trying things with Nod. It lives on the long-lived
`experiments` branch, which is never merged into `main`. Anything here can be
hacky; when an idea proves out, re-do it properly on a fresh branch off `main`.

## Layout

```
experiments/
  lab.py          shared helpers: a Config and an Agent with per-experiment state
  base.toml       model / routing defaults for every experiment (no secrets)
  env.sh          `source` it to put OPENROUTER_API_KEY in your shell (via sops)
  new.sh          `./experiments/new.sh some-idea` -> experiments/2026-11-20-some-idea/
  _template/      what new.sh copies
  <date>-<name>/
    notes.md      question, setup, what happened, verdict
    run.py        the experiment itself
    runs/         sessions, job logs, outputs (gitignored)
```

Each experiment keeps its memory and job logs in its own `runs/`, so nothing
touches `~/.nod/memory` and one experiment can't see another's sessions.

## Running

From the repo root:

```sh
source experiments/env.sh
./experiments/new.sh some-idea
uv run python experiments/2026-11-20-some-idea/run.py
```

With nod-extras' job tools on the path:

```sh
uv run --with-editable ~/src/mine/nod-extras python experiments/2026-11-20-some-idea/run.py
```

To change a part (a different soul, fewer tools, a wrapped provider), build the
`Agent` yourself in `run.py` from the pieces `lab.py` gives you instead of
editing `src/nod/`. Edit `src/nod/` directly only when the experiment *is* a core
change; that's fine on this branch.

## Keeping up with main

```sh
git merge main
```

Merge rather than rebase so the experiment history stays as it was run.
