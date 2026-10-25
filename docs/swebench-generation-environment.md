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
- Working inside the instance's prebuilt eval image (see below).
- The agent running the repo's existing test suite to check its work before
  finishing.

None of these hand nod the answer. The gold patch is still scrubbed from git
by the harness; the agent only sees the issue text and the code at the base
commit. Letting it run tests just lets it do the same verify-before-submit
loop a human developer would.

## No internet during generation

The harness scrubs future commits from git, but the internet still has them.
On its first run DeepSeek V4.1 Flash searched GitHub and fetched the actual fix
for 13 of 15 instances (`pull/<n>.diff`, the fixing commit's `.patch`, later
releases via `pip download`) and scored 15/15. That's the gold patch by another
route, so it is cheating by the rule above.

So generation runs offline: the instance container starts with
`--network none` (no `curl`, `pip`, or `urllib`), nod's `http_get` tool is left
out of the bench agent, and the soul says there's no internet. The image
already holds everything the tests need. A few test suites call live services
(requests' tests hit httpbin), so those particular tests can't pass during
generation; that's fine, since grading runs them with network.

## The one real sin at submission time

Ship **source edits only** in the diff. Strip test files, build directories,
and any large blob before grading — the harness supplies its own `test_patch`
and re-runs the official tests on a clean container. Nod's bench already
handles this: `run_nod.py` captures `git diff` of the source tree, and the
SWE_SOUL instructs nod to delete scratch/repro files before finishing and to
leave only the fix in the working tree. Don't regress on that.

## What we do

### Generate inside the instance's eval image

The first attempt at fixing this (a host venv per instance with
`pip install -e .`) meant rebuilding each repo's environment by hand. It took
eight reruns and a pile of legacy pins (`setuptools<70`, `extension_helpers`,
`--no-build-isolation`) to get one astropy instance installing. It would have
needed the same for every repo and version in the dataset, because the base
commits span a decade of Python and dependency versions.

SWE-bench already publishes a working environment for every instance: the eval
image the harness grades in, named in the dataset's `image` field. So
`bench/swebench/run_nod.py` now uses those:

1. Pull the instance's image.
2. Copy its `/testbed` out to a host dir. Copying, not cloning, keeps the build
   artifacts of the installed package (compiled extensions, egg-info).
3. Run a container from the image with that dir bind-mounted back at
   `/testbed` (and at its own host path).
4. nod's file tools edit the host copy. `run_shell` goes through
   `docker exec` with the `testbed` conda env active, running as the host
   user so files it creates stay editable from the host.
5. The patch is the diff against a snapshot of `/testbed` as the image shipped
   it, so untracked files from the image's install step stay out of the patch.

This is fair for the same reason the venv was: it's the setup SWE-agent treats
as the intended condition, and the image's git history is already sanitized by
the harness. It also means a test nod runs during generation behaves the same
way it will at grading.

### Tell the agent about the environment

`SWE_SOUL` in `run_nod.py` says where the repo is, that the env is already
active in `run_shell`, to use repo-relative paths with the file tools, to run
targeted tests (the shell timeout is 60s), and not to modify the environment.
The aim is still to stop the failure mode where the agent hits an import error,
decides "the environment is just broken", and ships an unverified patch.

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
