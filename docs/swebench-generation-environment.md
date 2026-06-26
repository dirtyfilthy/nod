# SWE-bench generation environment: fair vs. cheating

A note on what the agent may do during the **generation** phase (phase 1, the
bit `run_nod.py` runs on the nodgrader box) without invalidating the benchmark,
and on the environment we should hand it so it can actually verify its own
work.

This exists because the first failed re-run (`astropy__astropy-14182`) traced
to a generation-environment gap, not a grading one: nod produced a real patch
but could never run the test suite to catch that it was wrong, because the bare
clone wasn't importable and pytest wasn't installed.

---

## The one-line rule

**Only the submitted diff is graded, on a fresh, sanitized Docker container
built by the official harness.** Whatever the agent does during generation —
installing packages, building the repo, running tests, creating venvs —
cannot reach the grade. The grade is recomputed from scratch.

So the question "is X cheating?" reduces to: *does X give the agent
information about the gold patch it wouldn't otherwise have?* If no, it's
fair. If yes, it's cheating.

## What the harness actually protects against

The cheating SWE-bench guards against is **future leakage**: the agent reading
the gold patch out of git history. The harness sanitizes the eval container:

- removes `origin` so future commits can't be fetched,
- deletes git tags created after the base commit's timestamp,
- expires the reflog and runs aggressive `git gc`,
- removes unused branches.

That keeps the gold patch out of reach. None of it is about what tooling the
agent sets up at inference time — it's about what *information* the repo
exposes.

## What's fair (and standard) during generation

SWE-agent's own docs treat building a custom Docker image with the repo and
conda env preinstalled as a supported **speedup**, and `post_startup_commands`
(e.g. `pip install flake8`) as a normal way to ready the environment before
the agent starts. The assumed setup is a working, installed checkout the
agent can run tests in — that is the intended eval condition, not a loophole.

Concretely, for nodgrader these are all fair:

- `pip install -e .` (or the repo's own install instructions) so the package
  imports in generation.
- Installing `pytest` and the repo's test dependencies into the generation
  environment.
- Creating a venv for the work (see below).
- The agent running the repo's existing test suite to check its work before
  finishing.

None of these hand nod the answer. The gold patch is still scrubbed from git
by the harness; the agent only sees the issue text and the code at the base
commit. Letting it run tests just lets it do the same verify-before-submit
loop a human developer would.

## The one real sin at submission time

Ship **source edits only** in the diff. Strip test files, build directories,
and any large blob before grading — the harness supplies its own `test_patch`
and re-runs the official tests on a clean container. Nod's bench already
handles this: `run_nod.py` captures `git diff` of the source tree, and the
SWE_SOUL instructs nod to delete scratch/repro files before finishing and to
leave only the fix in the working tree. Don't regress on that.

## What we do

### 1. Install the target package into the generation environment

`bench/swebench/run_nod.py` now creates a venv per instance (in the workdir,
outside the clone so it can't leak into `git diff`) and runs
`pip install -e .` + `pytest` into it before handing control to the agent.
The agent is told the venv path in its soul and prompt and runs tests through
that interpreter. A `--no-install` flag keeps the old bare-clone behavior for
comparison or when the install is known to fail.

This is the change that turns the `astropy__astropy-14182` failure mode from
"confident patch, no verification, wrong" into "confident patch, ran the
tests, found it wrong, tried again." If the editable install fails (missing
build deps, etc.) it falls back to the old behavior and tells the agent no
venv was prepared, rather than aborting the run.

### 2. Tell the agent, in its soul, to use venvs

The `SWE_SOUL` in `run_nod.py` now has a "Using the venv" section: the venv
path, the instruction to run Python and pytest through that interpreter, the
warning that the checkout is a source tree and importing from system `python3`
will fail, the reminder that `py_compile` is not evidence the patch works, and
the constraint not to create another venv or modify the environment.

The point isn't to script the agent's every move; it's to stop the failure
mode where the agent hits an import error, mis-diagnoses it as "the
environment is just broken", and ships an unverified patch.

## References

- SWE-bench repo state loopholes (the cheating the harness prevents):
  https://github.com/SWE-bench/SWE-bench/issues/465
- Harness git-history sanitization fix:
  https://github.com/SWE-bench/SWE-bench/pull/471
- SWE-bench evaluation docs (Docker, fresh container per instance):
  https://www.swebench.com/SWE-bench/guides/evaluation/
- SWE-agent custom environment / `post_startup_commands` (installing at
  inference time is standard): https://swe-agent.com/latest/usage/cl_tutorial/
- "How Not to Run SWE-bench Pro" (capture source only, strip tests/build
  before grading): https://june.kim/how-not-to-run-swebench-pro
