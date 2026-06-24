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
