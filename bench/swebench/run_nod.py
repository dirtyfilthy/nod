"""Generate SWE-bench predictions by letting nod fix each issue in a real repo.

This is the patch-generation half of a SWE-bench run. For each instance it:

  1. pulls the instance's official SWE-bench eval image (the same one grading
     uses: right Python, dependencies, and the package already installed),
  2. copies the image's /testbed out to a host dir and bind-mounts it back into
     a container, so nod's file tools edit it on the host while run_shell runs
     inside the container with the `testbed` conda env active,
  3. points nod at the issue text with its normal tools,
  4. captures `git diff` as the predicted patch,
  5. writes one JSONL line in the official prediction format.

Grading is a *separate* step you run afterwards with the official harness (see
bench/swebench/README.md). Both phases need Docker; generation also needs an
OpenRouter key for the model.

    export OPENROUTER_API_KEY=sk-or-...
    python bench/swebench/run_nod.py --count 5 --output preds.jsonl

nod is a deliberately thin agent, so expect a modest resolve rate -- the point
is a real, officially-graded number, not a SOTA chase.
"""

from __future__ import annotations

import argparse
import functools
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from nod.agent import Agent
from nod.builtins import builtin_tools
from nod.config import Config
from nod.memory import InMemoryMemory, JSONFileMemory
from nod.provider import OpenAICompatibleProvider, ProviderError
from nod.skills import SkillLoader
from nod.tools import ToolRegistry

# The SWE-bench org's copy names each instance's published eval image in an
# `image` field; the older princeton-nlp mirror doesn't, and swebench>=5 can't
# grade it.
DEFAULT_DATASET = "SWE-bench/SWE-bench_Lite"
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash-1017"
# One OpenRouter provider with no fallbacks, so every run gets the same weights
# and runs stay comparable. DeepSeek's own endpoint is the config default, but
# the account's privacy setting excludes providers that may train on prompts,
# which removes it; DeepInfra states its quantization (fp8).
DEFAULT_PROVIDER = "deepinfra/fp8"
# Set explicitly so results don't ride on a provider default that can change.
# Whatever the model reasons is sent back each step, as DeepSeek's thinking mode
# requires in tool loops.
DEFAULT_REASONING_EFFORT = "high"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
CONTAINER_WORKDIR = "/testbed"
CONDA_ACTIVATE = "source /opt/miniconda3/bin/activate && conda activate testbed"
# Longer than the CLI's 60s: some test files the fix has to pass take minutes in
# these images (sympy's test_arit.py timed out at 60s in sympy-20322).
SHELL_TIMEOUT_SECONDS = 180

# A SWE-focused persona. nod's default SOUL is a chat agent; for the benchmark we
# want a terse engineer that edits files and stops when the fix is in.
SWE_SOUL = """\
You are a senior software engineer working inside a git repository. Your job is
to resolve the reported issue by editing the code in the current working
directory.

Rules:
- The repository is already checked out at the relevant commit in your CWD.
- You have a small set of tools and a library of skills you can read on demand.
  When a task matches a skill, read it before guessing.
- Investigate with the tools: list_dir, read_file, run_shell (grep, find, etc.).
- Reproduce the issue before changing code: write a small script (or run a
  specific test) that shows the reported behavior, and confirm it fails. After
  your fix, re-run it and confirm it now passes.
- The issue was reported against this exact commit, so assume it is real here.
  Existing tests passing does NOT mean it is fixed; they usually don't cover the
  reported case. If your reproduction doesn't show the problem, re-read the
  issue for the case you missed rather than concluding nothing needs changing.
- Make the smallest change that lets the existing code handle the case. Fix
  where the bad value or behavior originates, not where it surfaces downstream.
  Keep the changed function's structure and return shape; don't rewrite it,
  delegate it elsewhere, or add new dependencies, fallbacks, or features. If
  you try a small fix and its output surprises you, work out why before
  replacing it with something bigger.
- Edit source files with cud_file SEARCH/REPLACE blocks. Do NOT edit the test
  files.
- After the fix, test beyond the issue's literal example: other inputs that
  reach the same code (mixed or edge-case values, other invalid inputs, other
  entry points), and every piece of state the changed code maintains, not just
  the one that showed the bug. Assert the expected values, not merely that no
  error is raised. If any result surprises you, explain it before moving on.
- Also run the relevant existing tests with run_shell to check nothing broke.
  Read verification output in full: don't pipe it through `head`, `tail`, or
  `grep -v`, which can hide the failure or warning you're checking for.
  run_shell already shortens long output and saves the full text to a file
  you can read.
- Do NOT run `git commit`, `git add`, or `git push`. Just leave your edits in the
  working tree; the harness captures the diff.
- If you create any temporary or scratch files while investigating (helper
  scripts, notes, repros), DELETE them before finishing so they don't leak into
  the patch. Only the actual fix should remain in the working tree.
- When the fix is complete, reply with a one-line summary and stop calling tools.

Environment:
- You have no internet access, so don't try to fetch code, issues, or packages.
  Work from the repository in front of you.
- The repository is at /testbed inside this instance's SWE-bench Docker image.
  The right Python, the project's dependencies, and the package itself are
  already installed in the `testbed` conda env.
- run_shell runs inside that container with the env active and /testbed as the
  working directory, so `python` runs the right interpreter.
- read_file, cud_file and list_dir see the same files. Give them paths relative
  to the repository root (e.g. `pkg/module.py`), not `/testbed/...`.
- Verify your fix by running the relevant existing tests with the project's
  own runner. pytest is NOT installed in every image, so use:
  - Django: `./tests/runtests.py <test_module>` (e.g. `./tests/runtests.py utils_tests.test_text`)
  - SymPy: `bin/test path/to/test_file.py`
  - Most other projects: `python -m pytest path/to/test_file.py -x -q`
  If a runner is missing, don't install it; switch to the project's runner.
  Shell commands time out after {shell_timeout} seconds, so run targeted
  tests, not the whole suite.
- Do not pip install or otherwise modify the environment. The patch is graded
  in a fresh copy of this same image.
"""

PROMPT_TEMPLATE = """\
Resolve the following issue in the repository in your current working directory.

--- ISSUE ---
{problem}
--- END ISSUE ---

Find the root cause, apply the minimal fix to the source (not the tests), run
the relevant tests to verify it, and stop when done.
"""


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )


# Docker Hub allows 100 anonymous pulls per hour per IP. A full Lite run pulls
# 300 images, faster than that, so a failed pull waits and retries rather than
# failing the instance with an empty patch. The limit shows up in several
# wordings ("toomanyrequests", "pull rate limit", or a bare "unexpected status
# from HEAD request"), so every failure is retried except the few that can never
# succeed.
PULL_RATE_LIMIT_WAIT_SECONDS = 300
PULL_RATE_LIMIT_MAX_WAIT_SECONDS = 2 * 3600
PULL_PERMANENT_ERRORS = ("manifest unknown", "not found", "denied", "invalid reference")


def pull_image(image: str, log=print, sleep=time.sleep) -> bool:
    waited = 0
    while True:
        pull = _run(["docker", "pull", image], timeout=3600)
        if pull.returncode == 0:
            return True
        err = pull.stderr.strip()
        permanent = any(marker in err.lower() for marker in PULL_PERMANENT_ERRORS)
        if permanent or waited >= PULL_RATE_LIMIT_MAX_WAIT_SECONDS:
            log(f"! pull failed: {err[:500]}")
            return False
        log(f"pull failed, likely Docker Hub's rate limit; retrying in "
            f"{PULL_RATE_LIMIT_WAIT_SECONDS // 60} min ({waited // 60} min waited so far): {err[:300]}")
        sleep(PULL_RATE_LIMIT_WAIT_SECONDS)
        waited += PULL_RATE_LIMIT_WAIT_SECONDS


def start_container(image: str, name: str, host_dir: str, log=print) -> bool:
    """Start `name` from `image` with its /testbed mirrored at host_dir.

    /testbed is copied out rather than replaced with a fresh clone because the
    image's copy carries the build artifacts of the installed package (compiled
    extensions, egg-info). The copy is mounted both at /testbed, where the
    testbed env expects the package, and at its own host path, so absolute host
    paths from the file tools also resolve inside the container.
    """
    _run(["docker", "rm", "-f", name])
    if _run(["docker", "image", "inspect", image]).returncode != 0:
        log(f"pulling {image} ...")
        if not pull_image(image, log=log):
            return False
    steps = [
        ["docker", "create", "--name", name, image],
        ["docker", "cp", f"{name}:{CONTAINER_WORKDIR}", host_dir],
        ["docker", "rm", name],
        # No network: the upstream fix is a search away (PR diffs, later
        # releases on PyPI), and fetching it would turn the run into a lookup.
        # The image already has everything the tests need.
        ["docker", "run", "-d", "--name", name, "--network", "none",
         "-v", f"{host_dir}:{CONTAINER_WORKDIR}", "-v", f"{host_dir}:{host_dir}",
         "-w", CONTAINER_WORKDIR, image, "tail", "-f", "/dev/null"],
        # Package download caches can hold newer releases of the repo under test
        # (the xarray-4248 image's conda cache had xarray 2025.4.0, with the
        # fix). Installed envs keep their own copies of the files, so the caches
        # aren't needed to run anything. Grading uses fresh containers.
        ["docker", "exec", "-u", "0", name, "rm", "-rf",
         "/opt/miniconda3/pkgs", "/opt/conda/pkgs", "/root/.cache/pip"],
    ]
    for cmd in steps:
        result = _run(cmd, timeout=1800)
        if result.returncode != 0:
            log(f"! {' '.join(cmd[:2])} failed: {result.stderr.strip()[:200]}")
            _run(["docker", "rm", "-f", name])
            return False
    return True


def container_shell_argv(name: str):
    """Build the run_shell wrapper that executes commands inside `name`.

    Commands run as the host user so files they create in the bind mount stay
    editable by the file tools and by `git add` in make_patch. The inner
    `timeout` makes sure a command the client gave up on doesn't keep running in
    the container.
    """
    user = f"{os.getuid()}:{os.getgid()}"

    def argv(command: str) -> list[str]:
        return [
            "docker", "exec", "-u", user, "-e", "HOME=/tmp", "-w", CONTAINER_WORKDIR, name,
            "timeout", "-k", "5", str(SHELL_TIMEOUT_SECONDS),
            # `source activate` reads positional args as an env name, so stash the
            # command and clear them before activating.
            "bash", "-c", f'__nod_cmd=$1; set --; {CONDA_ACTIVATE} && eval "$__nod_cmd"',
            "bash", command,
        ]

    return argv


def snapshot_tree(repo_dir: str) -> str:
    """Return a tree id for the working tree as the image ships it.

    The image's /testbed can hold untracked, non-ignored files from its install
    step. Diffing against this snapshot instead of HEAD keeps them out of the
    patch; they already exist in the grading container, so including them would
    make the patch fail to apply.
    """
    _run(["git", "-C", repo_dir, "add", "-A"])
    tree = _run(["git", "-C", repo_dir, "write-tree"]).stdout.strip()
    _run(["git", "-C", repo_dir, "reset", "-q"])
    return tree


def load_instances(dataset: str, split: str, count: int, instance_ids: list[str] | None):
    try:
        from datasets import load_dataset
    except ImportError:
        sys.exit(
            "error: the 'datasets' package is required. "
            "Install bench deps: pip install -r bench/swebench/requirements.txt"
        )

    data = load_dataset(dataset, split=split)
    rows = list(data)
    if rows and not rows[0].get("image"):
        sys.exit(
            f"error: {dataset} has no 'image' field naming each instance's eval "
            f"image. Use {DEFAULT_DATASET}."
        )
    if instance_ids:
        wanted = set(instance_ids)
        rows = [r for r in rows if r["instance_id"] in wanted]
    else:
        rows = sorted(rows, key=lambda r: r["instance_id"])[:count]
    return rows


def make_patch(repo_dir: str, base: str = "HEAD") -> str:
    # Stage everything so new files show up, diff against base, then unstage so we
    # leave no side effects. core.fileMode off avoids spurious permission diffs.
    _run(["git", "-C", repo_dir, "add", "-A"])
    diff = _run(["git", "-c", "core.fileMode=false", "-C", repo_dir, "diff", "--cached", base])
    _run(["git", "-C", repo_dir, "reset"])
    return diff.stdout


def build_bench_agent(
    *,
    model: str,
    base_url: str,
    max_turns: int,
    traces_dir: str | None,
    shell_argv=None,
    provider: str = DEFAULT_PROVIDER,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> Agent:
    """Build the default nod instance used for SWE-bench generation.

    `provider` pins an OpenRouter provider with fallbacks off; "" lets
    OpenRouter choose. `reasoning_effort` is sent as OpenRouter's
    `reasoning.effort`; "" leaves the model's default.
    """
    cfg = Config(model=model, base_url=base_url, max_turns=max_turns)
    routing = {"order": [provider], "allow_fallbacks": False} if provider else {}
    llm = OpenAICompatibleProvider(
        model=cfg.model,
        base_url=cfg.base_url,
        provider_routing=routing,
        # "default" (or "") sends no reasoning field, leaving the model's own.
        reasoning={"effort": reasoning_effort} if reasoning_effort not in ("", "default") else {},
    )
    skills = SkillLoader(cfg.resolved_skills_dirs())
    return Agent(
        provider=llm,
        # http_get is left out for the same reason the container has no network.
        registry=ToolRegistry([
            t for t in builtin_tools(shell_argv=shell_argv, shell_timeout=SHELL_TIMEOUT_SECONDS)
            if t.name != "http_get"
        ]),
        memory=JSONFileMemory(root=traces_dir) if traces_dir else InMemoryMemory(),
        skills=skills,
        soul=SWE_SOUL.format(shell_timeout=SHELL_TIMEOUT_SECONDS),
        max_turns=cfg.max_turns,
    )


def solve_instance(
    inst: dict,
    *,
    model: str,
    base_url: str,
    max_turns: int,
    workdir: str,
    traces_dir: str | None,
    provider: str = DEFAULT_PROVIDER,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> dict:
    instance_id = inst["instance_id"]
    empty = {"instance_id": instance_id, "model_patch": "", "model_name_or_path": f"nod-{model}"}

    def log(msg: str) -> None:
        # Prefixed so lines from instances solved in parallel stay attributable.
        print(f"  [{instance_id}] {msg}", flush=True)

    # Neutral names: the repo dir is also mounted inside the container at its own
    # path, and a model that searches the filesystem will find it, so neither it
    # nor the container may carry the instance id (whose number is the upstream
    # PR number). The host log lines keep the id.
    workspace = uuid.uuid4().hex[:12]
    repo_dir = os.path.join(workdir, workspace)
    container = f"nod-gen-{workspace}"
    if os.path.exists(repo_dir):
        shutil.rmtree(repo_dir)

    log(f"starting {inst['image']} ...")
    if not start_container(inst["image"], container, repo_dir, log=log):
        shutil.rmtree(repo_dir, ignore_errors=True)
        return empty
    # The images add a "SWE-bench" commit on top of the base commit that only
    # flips file modes, so compare blob ids rather than commit ids. (core.fileMode
    # doesn't apply to commit-to-commit diffs.)
    raw = _run(["git", "-C", repo_dir, "diff", "--raw", "--no-abbrev", inst["base_commit"], "HEAD"])
    if raw.returncode != 0 or any(
        line.split()[2] != line.split()[3] for line in raw.stdout.splitlines()
    ):
        log(f"! /testbed content differs from base commit {inst['base_commit'][:10]}")
    base_tree = snapshot_tree(repo_dir)

    agent = build_bench_agent(
        model=model,
        base_url=base_url,
        max_turns=max_turns,
        traces_dir=traces_dir,
        shell_argv=container_shell_argv(container),
        provider=provider,
        reasoning_effort=reasoning_effort,
    )

    prompt = PROMPT_TEMPLATE.format(problem=inst["problem_statement"])
    prev = os.getcwd()
    os.chdir(repo_dir)
    try:
        agent.send(prompt, session_id=instance_id)
    except Exception as exc:  # noqa: BLE001 - one bad instance shouldn't kill the run
        log(f"! agent error: {exc}")
    finally:
        os.chdir(prev)

    patch = make_patch(repo_dir, base_tree)
    _run(["docker", "rm", "-f", container])
    shutil.rmtree(repo_dir, ignore_errors=True)
    log(f"patch: {len(patch)} chars")
    return {
        "instance_id": instance_id,
        "model_patch": patch,
        "model_name_or_path": f"nod-{model}",
    }


def solve_all(rows: list[dict], solve, workers: int):
    """Yield (instance, prediction) as each instance finishes, up to `workers` at once.

    Workers are processes rather than threads because solve_instance chdirs into
    the instance's repo so nod's file tools resolve relative paths there, and the
    working directory is per process.
    """
    if workers <= 1:
        for inst in rows:
            yield inst, solve(inst)
        return
    with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        futures = {pool.submit(solve, inst): inst for inst in rows}
        for future in as_completed(futures):
            inst = futures[future]
            try:
                yield inst, future.result()
            except ProviderError:
                for pending in futures:
                    pending.cancel()
                raise
            except Exception as exc:  # noqa: BLE001 - one dead worker shouldn't kill the run
                print(f"  [{inst['instance_id']}] ! worker failed: {exc}", flush=True)
                yield inst, {"instance_id": inst["instance_id"], "model_patch": "", "model_name_or_path": ""}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate SWE-bench predictions with nod.")
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--split", default="test")
    parser.add_argument("--count", type=int, default=5, help="How many instances (ignored if --instance-ids given).")
    parser.add_argument("--instance-ids", nargs="*", help="Specific instance ids to run.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--provider", default=DEFAULT_PROVIDER, help="OpenRouter provider to pin, fallbacks off ('' lets OpenRouter choose).")
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT, help="OpenRouter reasoning effort, e.g. low/medium/high; 'default' (or '') sends none, leaving the model's default.")
    parser.add_argument("--max-turns", type=int, default=100)
    parser.add_argument("--output", default="bench/swebench/predictions.jsonl")
    parser.add_argument("--workdir", default=None, help="Where each instance's /testbed is copied (default: a temp dir).")
    parser.add_argument("--traces-dir", default=None, help="Write full per-instance chat traces as JSON files.")
    parser.add_argument("--workers", type=int, default=1, help="Instances to solve at once, each in its own process and container.")
    parser.add_argument("--dry-run", action="store_true", help="List the selected instances and exit (no model calls).")
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be at least 1")

    rows = load_instances(args.dataset, args.split, args.count, args.instance_ids)
    if not rows:
        sys.exit("error: no instances selected.")

    print(f"Selected {len(rows)} instance(s):")
    for r in rows:
        print(f"  - {r['instance_id']} ({r['image']})")
    if args.dry_run:
        return 0

    if not os.environ.get("OPENROUTER_API_KEY"):
        sys.exit("error: set OPENROUTER_API_KEY before running (no --dry-run).")
    if _run(["docker", "info"], timeout=60).returncode != 0:
        sys.exit("error: docker is not available; generation runs in each instance's eval image.")

    workdir = args.workdir or tempfile.mkdtemp(prefix="work-")
    os.makedirs(workdir, exist_ok=True)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    solve = functools.partial(
        solve_instance,
        model=args.model,
        base_url=args.base_url,
        max_turns=args.max_turns,
        workdir=workdir,
        traces_dir=args.traces_dir,
        provider=args.provider,
        reasoning_effort=args.reasoning_effort,
    )
    print(
        f"Solving with {args.workers} worker(s), model {args.model} via "
        f"{args.provider or 'any provider'}, reasoning effort {args.reasoning_effort or 'default'}.",
        flush=True,
    )
    started = time.monotonic()
    with out_path.open("w", encoding="utf-8") as fh:
        try:
            # Written as each finishes, so a failed run keeps what completed.
            for done, (inst, pred) in enumerate(solve_all(rows, solve, args.workers), 1):
                pred["model_name_or_path"] = f"nod-{args.model}"
                fh.write(json.dumps(pred) + "\n")
                fh.flush()
                print(f"[{done}/{len(rows)}] {inst['instance_id']} done", flush=True)
        except ProviderError as exc:
            sys.exit(f"provider error (check key/model): {exc}")

    elapsed = time.monotonic() - started
    print(f"\nWrote {len(rows)} predictions to {out_path} in {elapsed:.0f}s.")
    print("Grade them with the official harness -- see bench/swebench/README.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
