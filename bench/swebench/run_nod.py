"""Generate SWE-bench predictions by letting nod fix each issue in a real repo.

This is the patch-generation half of a SWE-bench run. For each instance it:

  1. clones the repo and checks out the base commit,
  2. points nod at the issue text with its normal tools (read/write/run_shell),
  3. captures `git diff` as the predicted patch,
  4. writes one JSONL line in the official prediction format.

Grading is a *separate*, Docker-based step you run afterwards with the official
harness (see bench/swebench/README.md). This script never needs Docker; it only
needs git, network access, and an OpenRouter key for the model.

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
from nod.builtins import builtin_tools
from nod.config import Config
from nod.memory import InMemoryMemory, JSONFileMemory
from nod.provider import OpenAICompatibleProvider, ProviderError
from nod.skills import SkillLoader
from nod.tools import ToolRegistry

DEFAULT_DATASET = "princeton-nlp/SWE-bench_Lite"
DEFAULT_MODEL = "deepseek/deepseek-v3.2"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

# A SWE-focused persona. nod's default SOUL is a chat agent; for the benchmark we
# want a terse engineer that edits files and stops when the fix is in.
SWE_SOUL = """\
You are a senior software engineer working inside a git repository. Your job is
to resolve the reported issue by editing the code in the current working
directory.

Rules:
- The repository is already checked out at the relevant commit in your CWD.
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
"""

PROMPT_TEMPLATE = """\
Resolve the following issue in the repository in your current working directory.

--- ISSUE ---
{problem}
--- END ISSUE ---

Find the root cause, apply the minimal fix to the source (not the tests), and
stop when done.
"""


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )


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
    if instance_ids:
        wanted = set(instance_ids)
        rows = [r for r in rows if r["instance_id"] in wanted]
    else:
        rows = sorted(rows, key=lambda r: r["instance_id"])[:count]
    return rows


def make_patch(repo_dir: str) -> str:
    # Stage everything so new files show up, diff against HEAD, then unstage so we
    # leave no side effects. core.fileMode off avoids spurious permission diffs.
    _run(["git", "-C", repo_dir, "add", "-A"])
    diff = _run(["git", "-c", "core.fileMode=false", "-C", repo_dir, "diff", "--cached", "HEAD"])
    _run(["git", "-C", repo_dir, "reset"])
    return diff.stdout


def build_bench_agent(
    *,
    model: str,
    base_url: str,
    max_turns: int,
    traces_dir: str | None,
) -> Agent:
    """Build the default nod instance used for SWE-bench generation."""
    cfg = Config(model=model, base_url=base_url, max_turns=max_turns)
    provider = OpenAICompatibleProvider(model=cfg.model, base_url=cfg.base_url)
    skills = SkillLoader(cfg.resolved_skills_dirs())
    return Agent(
        provider=provider,
        registry=ToolRegistry(builtin_tools()),
        memory=JSONFileMemory(root=traces_dir) if traces_dir else InMemoryMemory(),
        skills=skills,
        soul=SWE_SOUL,
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
    repo = inst["repo"]
    base_commit = inst["base_commit"]
    repo_url = f"https://github.com/{repo}.git"

    clone_dir = os.path.join(workdir, instance_id.replace("/", "__"))
    if os.path.exists(clone_dir):
        shutil.rmtree(clone_dir)

    print(f"  cloning {repo} @ {base_commit[:10]} ...", flush=True)
    clone = _run(["git", "clone", "--quiet", repo_url, clone_dir], timeout=900)
    if clone.returncode != 0:
        print(f"  ! clone failed: {clone.stderr.strip()[:200]}", flush=True)
        return {"instance_id": instance_id, "model_patch": "", "model_name_or_path": f"nod-{model}"}
    co = _run(["git", "-C", clone_dir, "checkout", "--quiet", base_commit])
    if co.returncode != 0:
        print(f"  ! checkout failed: {co.stderr.strip()[:200]}", flush=True)
        return {"instance_id": instance_id, "model_patch": "", "model_name_or_path": f"nod-{model}"}

    agent = build_bench_agent(
        model=model,
        base_url=base_url,
        max_turns=max_turns,
        traces_dir=traces_dir,
    )

    prompt = PROMPT_TEMPLATE.format(problem=inst["problem_statement"])
    prev = os.getcwd()
    os.chdir(clone_dir)
    try:
        agent.send(prompt, session_id=instance_id)
    except Exception as exc:  # noqa: BLE001 - one bad instance shouldn't kill the run
        print(f"  ! agent error: {exc}", flush=True)
    finally:
        os.chdir(prev)

    patch = make_patch(clone_dir)
    shutil.rmtree(clone_dir, ignore_errors=True)
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
    parser.add_argument("--workdir", default=None, help="Where repos are cloned (default: a temp dir).")
    parser.add_argument("--traces-dir", default=None, help="Write full per-instance chat traces as JSON files.")
    parser.add_argument("--dry-run", action="store_true", help="List the selected instances and exit (no model calls).")
    args = parser.parse_args(argv)

    rows = load_instances(args.dataset, args.split, args.count, args.instance_ids)
    if not rows:
        sys.exit("error: no instances selected.")

    print(f"Selected {len(rows)} instance(s):")
    for r in rows:
        print(f"  - {r['instance_id']} ({r['repo']})")
    if args.dry_run:
        return 0

    if not os.environ.get("OPENROUTER_API_KEY"):
        sys.exit("error: set OPENROUTER_API_KEY before running (no --dry-run).")

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
    print("Grade them with the official harness (Docker required) -- see bench/swebench/README.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
