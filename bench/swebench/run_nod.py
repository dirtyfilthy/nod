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
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from nod.agent import Agent
from nod.builtins import SHELL_TIMEOUT_SECONDS, builtin_tools
from nod.config import Config
from nod.memory import InMemoryMemory, JSONFileMemory
from nod.provider import OpenAICompatibleProvider, ProviderError
from nod.skills import SkillLoader
from nod.tools import ToolRegistry

# The SWE-bench org's copy names each instance's published eval image in an
# `image` field; the older princeton-nlp mirror doesn't, and swebench>=5 can't
# grade it.
DEFAULT_DATASET = "SWE-bench/SWE-bench_Lite"
DEFAULT_MODEL = "deepseek/deepseek-v3.2"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
CONTAINER_WORKDIR = "/testbed"
CONDA_ACTIVATE = "source /opt/miniconda3/bin/activate && conda activate testbed"

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
- Make the minimal change that fixes the issue. Edit source files with
  cud_file SEARCH/REPLACE blocks. Do NOT edit the test files.
- You may run the project's tests with run_shell to check your work.
- Do NOT run `git commit`, `git add`, or `git push`. Just leave your edits in the
  working tree; the harness captures the diff.
- If you create any temporary or scratch files while investigating (helper
  scripts, notes, repros), DELETE them before finishing so they don't leak into
  the patch. Only the actual fix should remain in the working tree.
- When the fix is complete, reply with a one-line summary and stop calling tools.

Environment:
- The repository is at /testbed inside this instance's SWE-bench Docker image.
  The right Python, the project's dependencies, and the package itself are
  already installed in the `testbed` conda env.
- run_shell runs inside that container with the env active and /testbed as the
  working directory, so `python` and `python -m pytest` just work.
- read_file, cud_file and list_dir see the same files. Give them paths relative
  to the repository root (e.g. `pkg/module.py`), not `/testbed/...`.
- Verify your fix by running the relevant existing tests, e.g.
  `python -m pytest path/to/test_file.py -x -q`, or the project's own runner
  (Django: `./tests/runtests.py <module>`). Shell commands time out after
  {shell_timeout} seconds, so run targeted tests, not the whole suite.
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


def start_container(image: str, name: str, host_dir: str) -> bool:
    """Start `name` from `image` with its /testbed mirrored at host_dir.

    /testbed is copied out rather than replaced with a fresh clone because the
    image's copy carries the build artifacts of the installed package (compiled
    extensions, egg-info). The copy is mounted both at /testbed, where the
    testbed env expects the package, and at its own host path, so absolute host
    paths from the file tools also resolve inside the container.
    """
    _run(["docker", "rm", "-f", name])
    if _run(["docker", "image", "inspect", image]).returncode != 0:
        print(f"  pulling {image} ...", flush=True)
        pull = _run(["docker", "pull", image], timeout=3600)
        if pull.returncode != 0:
            print(f"  ! pull failed: {pull.stderr.strip()[:200]}", flush=True)
            return False
    steps = [
        ["docker", "create", "--name", name, image],
        ["docker", "cp", f"{name}:{CONTAINER_WORKDIR}", host_dir],
        ["docker", "rm", name],
        ["docker", "run", "-d", "--name", name,
         "-v", f"{host_dir}:{CONTAINER_WORKDIR}", "-v", f"{host_dir}:{host_dir}",
         "-w", CONTAINER_WORKDIR, image, "tail", "-f", "/dev/null"],
    ]
    for cmd in steps:
        result = _run(cmd, timeout=1800)
        if result.returncode != 0:
            print(f"  ! {' '.join(cmd[:2])} failed: {result.stderr.strip()[:200]}", flush=True)
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
) -> Agent:
    """Build the default nod instance used for SWE-bench generation."""
    cfg = Config(model=model, base_url=base_url, max_turns=max_turns)
    provider = OpenAICompatibleProvider(model=cfg.model, base_url=cfg.base_url)
    skills = SkillLoader(cfg.resolved_skills_dirs())
    return Agent(
        provider=provider,
        registry=ToolRegistry(builtin_tools(shell_argv=shell_argv)),
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
) -> dict:
    instance_id = inst["instance_id"]
    empty = {"instance_id": instance_id, "model_patch": "", "model_name_or_path": f"nod-{model}"}

    repo_dir = os.path.join(workdir, instance_id.replace("/", "__"))
    container = f"nod-gen.{instance_id.lower()}"
    if os.path.exists(repo_dir):
        shutil.rmtree(repo_dir)

    print(f"  starting {inst['image']} ...", flush=True)
    if not start_container(inst["image"], container, repo_dir):
        shutil.rmtree(repo_dir, ignore_errors=True)
        return empty
    # The images add a "SWE-bench" commit on top of the base commit that only
    # flips file modes, so compare blob ids rather than commit ids. (core.fileMode
    # doesn't apply to commit-to-commit diffs.)
    raw = _run(["git", "-C", repo_dir, "diff", "--raw", "--no-abbrev", inst["base_commit"], "HEAD"])
    if raw.returncode != 0 or any(
        line.split()[2] != line.split()[3] for line in raw.stdout.splitlines()
    ):
        print(f"  ! /testbed content differs from base commit {inst['base_commit'][:10]}", flush=True)
    base_tree = snapshot_tree(repo_dir)

    agent = build_bench_agent(
        model=model,
        base_url=base_url,
        max_turns=max_turns,
        traces_dir=traces_dir,
        shell_argv=container_shell_argv(container),
    )

    prompt = PROMPT_TEMPLATE.format(problem=inst["problem_statement"])
    prev = os.getcwd()
    os.chdir(repo_dir)
    try:
        agent.send(prompt, session_id=instance_id)
    except Exception as exc:  # noqa: BLE001 - one bad instance shouldn't kill the run
        print(f"  ! agent error: {exc}", flush=True)
    finally:
        os.chdir(prev)

    patch = make_patch(repo_dir, base_tree)
    _run(["docker", "rm", "-f", container])
    shutil.rmtree(repo_dir, ignore_errors=True)
    print(f"  patch: {len(patch)} chars", flush=True)
    return {
        "instance_id": instance_id,
        "model_patch": patch,
        "model_name_or_path": f"nod-{model}",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate SWE-bench predictions with nod.")
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--split", default="test")
    parser.add_argument("--count", type=int, default=5, help="How many instances (ignored if --instance-ids given).")
    parser.add_argument("--instance-ids", nargs="*", help="Specific instance ids to run.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--max-turns", type=int, default=50)
    parser.add_argument("--output", default="bench/swebench/predictions.jsonl")
    parser.add_argument("--workdir", default=None, help="Where each instance's /testbed is copied (default: a temp dir).")
    parser.add_argument("--traces-dir", default=None, help="Write full per-instance chat traces as JSON files.")
    parser.add_argument("--dry-run", action="store_true", help="List the selected instances and exit (no model calls).")
    args = parser.parse_args(argv)

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

    workdir = args.workdir or tempfile.mkdtemp(prefix="nod-swebench-")
    os.makedirs(workdir, exist_ok=True)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    with out_path.open("w", encoding="utf-8") as fh:
        for i, inst in enumerate(rows, 1):
            print(f"[{i}/{len(rows)}] {inst['instance_id']}", flush=True)
            try:
                pred = solve_instance(
                    inst,
                    model=args.model,
                    base_url=args.base_url,
                    max_turns=args.max_turns,
                    workdir=workdir,
                    traces_dir=args.traces_dir,
                )
            except ProviderError as exc:
                sys.exit(f"provider error (check key/model): {exc}")
            fh.write(json.dumps(pred) + "\n")
            fh.flush()

    elapsed = time.monotonic() - started
    print(f"\nWrote {len(rows)} predictions to {out_path} in {elapsed:.0f}s.")
    print("Grade them with the official harness -- see bench/swebench/README.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
