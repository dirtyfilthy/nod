# Nod Failure Notes

This document records concrete failures observed in benchmark runs, with enough
detail to turn them into agent or harness improvements later.

## SWE-bench Lite: astropy__astropy-14182

Run context:

- Run ID: `nod-smoke-20260624-031908`
- Dataset: `princeton-nlp/SWE-bench_Lite`
- Model: `deepseek/deepseek-v3.2` through OpenRouter
- Nod commit on grader: `ae46088`
- Smoke result: 4/5 resolved
- Unresolved instance: `astropy__astropy-14182`
- Trace file: `myinfra/misc/nodgrader-bench/nod-smoke-20260624-031908/traces/astropy__astropy-14182.json`

### User-visible issue

The issue asked Astropy to support `header_rows` when writing tables in
reStructuredText format. The visible failure was:

```text
TypeError: RST.__init__() got an unexpected keyword argument 'header_rows'
```

The straightforward write-side fix is to let the RST writer accept
`header_rows`, pass it to the fixed-width base class, and stop hard-coding the
separator line at index `1`.

### What nod did well

Nod quickly found the relevant module, `astropy/io/ascii/rst.py`, and identified
that the RST writer inherits from the fixed-width writer. It also inspected
`astropy/io/ascii/fixedwidth.py` and found that the base writer already has
`header_rows` support.

The generated patch solved the visible constructor error and used the number of
header rows to choose the separator line:

- `RST.__init__` accepted `header_rows`.
- The constructor passed `header_rows` to `FixedWidth.__init__`.
- `RST.write` used `len(self.header.header_rows)` to find the separator line.

That matched the main write-side behavior in the upstream Astropy fix.

### Why the patch failed

The hidden benchmark test covered more than the original TypeError. It expected
RST tables with extra header rows to round-trip through both write and read.

The upstream fix also changed the reader path:

```python
def read(self, table):
    self.data.start_line = 2 + len(self.header.header_rows)
    return super().read(table)
```

Nod did not add the corresponding read-side adjustment. `SimpleRSTData` still
had a fixed `start_line = 3`, which only works for the default one-row header.
With extra header rows, data starts later. The reader therefore treats one of the
extra header rows or separator rows as data.

The decisive reasoning miss appears in the trace: nod noticed that
`SimpleRSTData.start_line = 3` is for reading, then dismissed it because the
issue was framed as output support. SWE-bench's hidden test expected the broader
format invariant: if `header_rows` changes the written table layout, the reader
must also understand that layout.

### Environment trouble during generation

The generation environment did not provide a ready Astropy development setup.
Nod hit several validation failures:

- `python` was not available, only `python3`.
- Importing Astropy from the checkout failed because compiled extensions were
  not built.
- `python3 setup.py build_ext --inplace` failed because build dependencies such
  as `extension_helpers` were missing.
- Running pytest failed while loading Astropy's test configuration because
  dependencies such as `setuptools_scm` were missing.
- `pip install -e .` hit the externally managed Python environment guard.
- Retrying with `--break-system-packages` still failed because the old Astropy
  build expected `setuptools.dep_util`, which is absent in the available
  setuptools.

The `python` versus `python3` issue was minor and recoverable. Nod did recover
from it once, but did not consistently update its environment model. The more
important failure was that, after validation became impractical, nod did not
switch into a sufficiently careful static analysis mode.

### Agent-loop lesson

This was not primarily an infrastructure failure. The official SWE-bench Docker
environment worked during grading, and the gold sanity check passed. The weak
point was that generation happens in a plain source checkout rather than inside
the per-instance SWE-bench test environment.

Still, nod should handle this class of situation better:

- After `python` fails, consistently use `python3`.
- Avoid system-wide pip once the interpreter reports an externally managed
  environment.
- Prefer a temporary virtual environment for local validation attempts.
- If a package needs compiled extensions and the build dependencies are absent,
  state that tests cannot be trusted and lean harder on static contract analysis.
- When validation is blocked, inspect existing tests and parent-class invariants
  more aggressively.
- Treat read/write format support as a paired contract when a writer changes the
  serialized table layout.

### Harness improvement ideas

Better test feedback during generation would likely have exposed this miss:

- Provide a tool that runs targeted tests through the official SWE-bench
  environment before finalizing a patch.
- Expose a cheap "validate this patch on the instance tests" command to nod,
  even if it is slower than local shell execution.
- Preinstall a minimal Python alias package on nodgrader so `python` points to
  `python3`.
- Consider giving nod an explicit environment note for SWE-bench instances:
  local shell is a source checkout, while authoritative tests run later in the
  harness container.

The main product lesson is not "install every dependency everywhere." It is that
nod must convert failed validation attempts into better reasoning, especially
when the codebase already contains adjacent patterns that imply the missing
behavior.

## SWE-bench Lite: DeepSeek V4.1 Flash looked up the answers

Run context:

- Run ID: `nod-smoke-20261025-003807`
- Dataset: `SWE-bench/SWE-bench_Lite`, a fixed 15-instance sample across 10 repos
- Model: `deepseek/deepseek-v4.1-flash-1017`, pinned to `deepinfra/fp8` on
  OpenRouter
- Nod commit on grader: `c148aa3`
- Result: 15/15 resolved, for $0.42 of tokens
- Same sample on `deepseek/deepseek-v3.2` (`nod-smoke-20261024-224507`): 6/15
- Traces: `myinfra/misc/nodgrader-bench/nod-smoke-20261025-003807/traces/`

### What happened

The jump from 6/15 to 15/15 included `pydata__xarray-4248`, an instance where
the required design appears only in the issue's hints, which nod never sees.
That should not be solvable from the issue text, so the traces were checked.

Generation containers had network access, and the bench agent still had nod's
`http_get` tool. In 13 of the 15 instances the model went to the internet for
the upstream fix: 56 `http_get` calls plus 41 shell commands using `curl`,
`urllib`, or `pip download`. Its search was methodical:

- GitHub search API queries built from the issue's wording, e.g.
  `search/commits?q=repo:astropy/astropy+zero-size+input+wcs`
- Issue timelines, to find the PR that closed the issue
- The fixing PR or commit itself: `pydata/xarray/pull/4248.diff`,
  `psf/requests/pull/863.diff`, `sympy/sympy/pull/11897.diff`, and the
  fixing commit's `.patch` for `django__django-12113` and
  `scikit-learn__scikit-learn-14983`
- The same file at a later release tag on `raw.githubusercontent.com`
- `pip download pytest==5.3.0`, `6.2.0`, and `7.4.0`, then reading the fixed
  source out of the wheels

### Evidence that it copied

Similarity is computed between each submitted patch's changed lines and the
upstream fix's changed lines (Jaccard on stripped `+`/`-` lines):

| Instance | V3.2, offline in practice | V4.1, online |
| --- | --- | --- |
| astropy__astropy-7746 | 0.00 | 0.67 |
| django__django-11422 | 0.14 | 0.82 |
| django__django-12113 | 0.00 | 1.00 |
| django__django-14915 | 1.00 | 1.00 (no fetch) |
| django__django-15814 | 0.00 | 1.00 |
| matplotlib__matplotlib-22711 | 0.03 | 0.03 |
| psf__requests-863 | 0.20 | 1.00 |
| pydata__xarray-4248 | 0.00 | 1.00 |
| pylint-dev__pylint-7228 | 0.08 | 0.85 |
| pytest-dev__pytest-5103 | 0.00 | 0.06 |
| scikit-learn__scikit-learn-14983 | 0.29 | 1.00 |
| sphinx-doc__sphinx-8506 | 0.00 | 0.86 |
| sympy__sympy-11897 | 0.00 | 1.00 |
| sympy__sympy-14774 | 1.00 | 1.00 (no fetch) |
| sympy__sympy-17630 | 0.00 | 1.00 |

Nine of the 13 instances that fetched code submitted the upstream fix's
changed lines exactly. The two that didn't fetch were already solved by V3.2.
matplotlib and pytest resolved with patches unlike upstream's, after reading
later versions of the same files, so they were informed by the future code
rather than copied from it.

### What it was not

- **Git history.** The images' `/testbed` history holds only the base commit's
  ancestors plus SWE-bench's own setup commit. The model ran `git log --all`,
  `git branch -a`, and `git show` on several instances and found nothing from
  after the base commit.
- **The instance id.** Each instance's number is its upstream PR number, and
  it appears in the host work dir and container names. No trace shows the
  number before the first network call; the model found the PR numbers by
  searching.
- **Earlier runs.** Across all five V3.2 runs, with the same tools and the same
  network access, the model made zero `http_get` calls and no lookups from the
  shell. Those results stand.

### Why it matters

Looking upstream first is good engineering on a real bug, so the model broke
no rule it was given. The benchmark assumed the agent works offline and
nothing enforced it. SWE-bench's harness carefully scrubs future commits from
git, but the same answers are a search away. Any agent benchmark run with
network access is open to this, separately from memorization in training data.

The behavior also changed within one model generation, with the same tools,
the same network access, and the same instructions. V3.2 never reached for the
web; V4.1 Flash did so on its first run.

### Fix

Commit `4be226c`:

- Generation containers start with `--network none`. On a real image, `curl`,
  `urllib`, and `pip download` all fail while the repo's tests still run.
- The bench agent no longer gets `http_get`.
- The soul says there is no internet access, so the model doesn't spend turns
  trying.

`docs/swebench-generation-environment.md` records "no internet during
generation" as part of the fairness rules.

### The offline re-run, and a second leak

Run `nod-smoke-20261025-012112` (nod `4be226c`) repeated the sample offline and
scored 10/15. Every network attempt failed: `pip download` found no versions,
and there were no `http_get` calls. But the model had moved on to looking for
newer copies of the code inside the image, running `find /` for the module
outside `/testbed` and listing package caches.

In `pydata__xarray-4248` that worked. The image's conda download cache held
xarray 2025.4.0, and the model grepped its `formatting.py` and found the very
hook the fix adds:

```text
296:    if hasattr(var._data, "_repr_inline_"):
297:        return var._data._repr_inline_(max_width)
```

It wrote its fix from that. The leak is in SWE-bench's image, not nod's setup;
the other images' caches held nothing useful. Counting xarray as a failure,
the clean result is **9/15**, against V3.2's 6/15 on the same sample: newly
solved django-12113, sphinx-8506 and sympy-17630, none lost.

`start_container` in `bench/swebench/run_nod.py` now deletes the package download caches (`/opt/miniconda3/pkgs`,
`/opt/conda/pkgs`, `/root/.cache/pip`) when each generation container starts.
On a real image, the environment and tests still work without them.

Two caveats on comparing the models:

- V4.1 Flash reasons before tool calls, and until this fix nod discarded that
  reasoning instead of sending it back. DeepSeek's thinking-mode docs require it
  be passed back within a tool loop (their own API returns 400 without it), so
  these V4.1 runs had the model re-thinking from scratch each step. Commit
  `2cc624a` stores reasoning, sends it back, and logs it in traces.
- Whether V3.2 was reasoning during its runs wasn't checked, so the 6 to 9 gain
  isn't purely "newer model".

### Lessons

- Treat a sudden jump in score as a bug until the traces say otherwise. One
  instance that shouldn't be solvable, here xarray-4248, is enough to
  investigate.
- Enforce the environment rather than asking the model. A prompt rule alone
  would not have closed this reliably; on earlier runs V3.2 ignored several of
  the bench's prompt rules (unfiltered output, no "already fixed" exits).
- Re-check a benchmark's assumptions whenever the model changes. A setup that
  was fair for one model was not fair for the next.
