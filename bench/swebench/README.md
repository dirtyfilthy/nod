# nod on SWE-bench Lite

An honest, officially-graded benchmark of the nod agent: let nod fix real GitHub
issues in real repos, then score the patches with the official SWE-bench harness.

nod is a deliberately thin agent (one synchronous tool loop, five built-in
tools, a 60s shell timeout), not a tuned SWE scaffold. Treat the result as a real
baseline number, not a leaderboard attempt.

## Two phases

Patch generation and grading are separate, but both run in the same
per-instance Docker images: the published SWE-bench eval images named in the
dataset's `image` field.

### Phase 1 — generate patches (Docker)

Needs Docker, network, and an OpenRouter key. For each instance we pull its eval
image, copy the image's `/testbed` (the repo at the base commit, already
installed into the image's `testbed` conda env) to a host dir and bind-mount it
back into a container. nod's file tools edit the host copy; `run_shell` runs
inside the container with the env active, so nod can run the real tests. We
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
`--model <openrouter-model>`, `--max-turns 100` (the default), `--workers 4`
(solve up to 4 instances at once, each in its own process and container;
default 1). See
`docs/swebench-generation-environment.md` for why letting nod run tests in the
eval image is fair on SWE-bench. The images are x86; on an ARM machine Docker
emulates them, which works but is slow. Output is a JSONL of
`{"instance_id", "model_patch", "model_name_or_path"}` — the official prediction
format.

### Phase 2 — grade (Docker, x86)

The official harness applies each patch in a fresh container of the same
per-instance image (images pulled in phase 1 are reused) and
runs the repo's real test suite. This is the **only** credible scorer; we do not
reimplement it. It needs Docker and is x86-centric (the images are flaky on ARM),
so run it on an x86 box — e.g. the Hetzner deploy host once Docker is installed.

```sh
# On the x86 + Docker host, with preds.jsonl copied over:
pip install swebench
python -m swebench.harness.run_evaluation \
  --dataset_name SWE-bench/SWE-bench_Lite \
  --predictions_path preds.jsonl \
  --max_workers 4 \
  --run_id nod-smoke-1

# Sanity-check the harness itself first with the gold patches:
python -m swebench.harness.run_evaluation \
  --dataset_name SWE-bench/SWE-bench_Lite \
  --predictions_path gold --max_workers 1 \
  --instance_ids sympy__sympy-20590 --run_id validate-gold
```

The harness writes a report with `resolved` / `unresolved` / `error` /
`empty_patch` counts. Read all four: a high `empty_patch` rate means nod is
timing out or hitting the turn limit before producing a diff, not that the fixes
were wrong.

## Notes / caveats

- **Disk + time:** every instance pulls its own eval image (shared layers per
  repo/version, but still multiple GB per repo); budget disk accordingly.
- **Dataset:** use `SWE-bench/SWE-bench_Lite`. The older
  `princeton-nlp/SWE-bench_Lite` copy has no `image` field, and swebench 5.x
  can't grade it.
- **Tokens:** each instance is many model round-trips. Start with the 5-instance
  smoke before scaling.
- **No test edits:** nod is instructed not to touch test files (the harness
  supplies its own `test_patch`); patches that modify tests can be rejected.
