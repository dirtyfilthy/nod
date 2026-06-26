---
name: run-nodgrader
description: Run the nodgrader SWE-bench benchmark against the nod agent — provision the ephemeral Hetzner box, generate patches, grade them, collect results, and tear down. Use when the user asks to benchmark nod, run SWE-bench, grade patches, or drive the nodgrader box.
disable-model-invocation: true
---

# Run nodgrader

`nodgrader` is an **ephemeral, on-demand Hetzner box** for grading nod against SWE-bench Lite. It is not always-on: spin it up, run a bench, collect results, destroy it. **It costs money (Hetzner compute + OpenRouter tokens) while it runs** — always tear it down when done.

The runner and full reference live in the sibling `myinfra` repo, **not this one**. This skill is the short "how to actually run it." Full reference: `../myinfra/docs/nodgrader.md` (relative to this repo root).

## Prerequisites (one-time, on your workstation)

- `terraform`, `ansible`, `hcloud` CLI, `sops` + age key, `ssh`, `jq`.
- An hcloud CLI context named `nodgrader` (reusing the `nod` project token is fine).
- `myinfra/secrets/projects/nodgrader.sops.yaml` holding `OPENROUTER_API_KEY` + `GH_TOKEN`.

If any are missing, stop and tell the user — the bench cannot run without them.

## The easy path: one command

From the `myinfra` repo root:

```sh
scripts/nodgrader_bench.sh
```

That's the default 5-instance smoke. It provisions the box, pulls latest nod `main`, generates patches **with chat traces**, grades with the official SWE-bench harness, fetches everything to `misc/nodgrader-bench/<run-id>/`, and **destroys the box on exit** (even on failure or Ctrl-C, unless `--keep`).

## Selecting specific instances

`-i` / `--instance-id` takes one instance id and is **repeatable** — pass it multiple times for a targeted set:

```sh
scripts/nodgrader_bench.sh -i astropy__astropy-12907 -i django__django-11099
```

`--instance-ids IDS` is the comma-separated alternative (one flag, many ids):

```sh
scripts/nodgrader_bench.sh --instance-ids astropy__astropy-12907,django__django-11099
```

When instance ids are given, `--count` is ignored. Validate ids with `--dry-run` semantics by listing them first if unsure — wrong ids silently produce no predictions.

## Options

| Flag | Default | Meaning |
| --- | --- | --- |
| `-c, --count N` | `5` | Instances to generate and grade (ignored when instance ids given). |
| `-i, --instance-id ID` | — | One specific instance. **Repeatable.** |
| `--instance-ids IDS` | — | Comma-separated instance ids (alternative to `-i`). |
| `-w, --max-workers N` | `4` | Grading workers (drop to `2` if OOM). |
| `-r, --run-id ID` | `nod-smoke-<UTC timestamp>` | Grading run id. |
| `-d, --results-dir DIR` | `misc/nodgrader-bench/<run-id>` | Where results land locally. |
| `--dataset NAME` | `princeton-nlp/SWE-bench_Lite` | Dataset. |
| `--no-validate` | off | Skip the gold-patch sanity check. |
| `--keep` | off | Leave the box up on exit. |

## Reading the results

Everything lands under `myinfra/misc/nodgrader-bench/<run-id>/` (gitignored):

- `preds.jsonl` — one line per instance (the official prediction format).
- `traces/` — one JSONL chat trace per instance (full agent loop: tools called, files read, shell output, where it got stuck).
- `report.json` — grading report: `resolved_ids`, `unresolved_ids`, `empty_patch_ids`, `error_ids`.
- `summary.txt` — human-readable counts and resolved rate.

Read all four report categories. A high `empty_patch` count means nod hit its turn/time limit before producing a diff — **not** that the fixes were wrong, and not an infra failure.

## Discipline

- **Always destroy the box.** The bench script does this for you; manual runs do not. With `--keep`, destroy by hand: `terraform -chdir=terraform/projects/nodgrader destroy`.
- **Start with the 5-instance smoke** before scaling — each instance is many model round-trips (OpenRouter spend) plus a Docker image per repo (disk + time).
- **Confirm the gold sanity check resolved** before treating misses as real. If gold fails, the grade numbers are suspect.
- **Reproducibility:** deploys track the tip of `main`, so two runs at different times use different nod code. Pin `nod_version` in the deploy playbook to a tag/SHA if a comparable baseline matters.

## Manual path

For debugging or resuming a partial run, the step-by-step (provision, generate on-box as `grader` in a login shell, grade, destroy) is in `../myinfra/docs/nodgrader.md`. Prefer the bench script unless something is broken.
