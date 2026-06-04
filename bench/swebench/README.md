# nod on SWE-bench Lite

An honest, officially-graded benchmark of the nod agent: let nod fix real GitHub
issues in real repos, then score the patches with the official SWE-bench harness.

nod is a deliberately thin agent (one synchronous tool loop, five built-in
tools, a 60s shell timeout), not a tuned SWE scaffold. Treat the result as a real
baseline number, not a leaderboard attempt.

## Two phases

Patch generation and grading are separate. Only grading needs Docker.

### Phase 1 — generate patches (no Docker)

Runs anywhere with git, network, and an OpenRouter key. For each instance nod
clones the repo at its base commit, works the issue with its tools, and we
capture `git diff` as the predicted patch.

```sh
pip install -e .                                   # nod itself
pip install -r bench/swebench/requirements.txt     # datasets (+ swebench, for phase 2)
export OPENROUTER_API_KEY=sk-or-...

# Smoke run: 5 instances. Use --dry-run first to see the selection with no spend.
python bench/swebench/run_nod.py --dry-run --count 5
python bench/swebench/run_nod.py --count 5 --output preds.jsonl
```

Useful flags: `--instance-ids astropy__astropy-12907 django__django-11099`,
`--model <openrouter-model>`, `--max-turns 50`. Output is a JSONL of
`{"instance_id", "model_patch", "model_name_or_path"}` — the official prediction
format.

### Phase 2 — grade (Docker, x86)

The official harness applies each patch in a per-instance Docker container and
runs the repo's real test suite. This is the **only** credible scorer; we do not
reimplement it. It needs Docker and is x86-centric (the images are flaky on ARM),
so run it on an x86 box — e.g. the Hetzner deploy host once Docker is installed.

```sh
# On the x86 + Docker host, with preds.jsonl copied over:
pip install swebench
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Lite \
  --predictions_path preds.jsonl \
  --max_workers 4 \
  --run_id nod-smoke-1

# Sanity-check the harness itself first with the gold patches:
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Lite \
  --predictions_path gold --max_workers 1 \
  --instance_ids sympy__sympy-20590 --run_id validate-gold
```

The harness writes a report with `resolved` / `unresolved` / `error` /
`empty_patch` counts. Read all four: a high `empty_patch` rate means nod is
timing out or hitting the turn limit before producing a diff, not that the fixes
were wrong.

## Notes / caveats

- **Disk + time:** even a smoke run pulls a Docker image per repo during grading;
  budget several GB and minutes per instance.
- **Tokens:** each instance is many model round-trips. Start with the 5-instance
  smoke before scaling.
- **No test edits:** nod is instructed not to touch test files (the harness
  supplies its own `test_patch`); patches that modify tests can be rejected.
