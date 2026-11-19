"""Break down a bench run's prompt tokens, cache hits and cost from its traces.

    python bench/swebench/token_usage.py RESULTS_DIR [RESULTS_DIR ...]

Each RESULTS_DIR is a nodgrader results directory with a `traces/` folder of
per-instance JSONL sessions. Every assistant entry's `meta.usage` carries
`prompt_tokens`, `cached_tokens`, `completion_tokens` and `cost`; the script
sums those per task and per run, and fits the effective per-token prices
(uncached prompt, cached prompt, completion) from the costs by least squares,
so it works for any provider without a price table. A "miss" is a call whose
cached prefix is shorter than the previous call's whole prompt, i.e. something
earlier in the request changed and broke the provider's prefix cache.

The write-up of the first runs it was used on is docs/prompt-caching.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Cached prefixes are counted in blocks (128 tokens on DeepInfra), so a
# shortfall smaller than this is rounding, not a miss.
MISS_SLACK = 256


def usages(path: Path) -> list[dict]:
    out = []
    for line in path.read_text().splitlines():
        entry = json.loads(line)
        if entry.get("role") != "assistant":
            continue
        meta = entry.get("meta") or {}
        # Empty replies the agent asked again for are dropped from the
        # conversation, but their calls were made first and billed.
        for usage in [(m or {}).get("usage") or {} for m in meta.get("empty_replies") or []] + [
            meta.get("usage") or {}
        ]:
            if "prompt_tokens" in usage:
                out.append(usage)
    return out


def fit_prices(calls: list[dict]) -> tuple[float, float, float]:
    """Least-squares $/token for (uncached prompt, cached prompt, completion)."""
    rows = [
        (u["prompt_tokens"] - (u.get("cached_tokens") or 0), u.get("cached_tokens") or 0,
         u["completion_tokens"], u.get("cost") or 0.0)
        for u in calls
    ]
    # Normal equations, solved by Gauss-Jordan; three unknowns needs no numpy.
    m = [[sum(r[i] * r[j] for r in rows) for j in range(3)] + [sum(r[i] * r[3] for r in rows)]
         for i in range(3)]
    for i in range(3):
        if not m[i][i]:
            return 0.0, 0.0, 0.0
        m[i] = [x / m[i][i] for x in m[i]]
        for k in range(3):
            if k != i:
                m[k] = [a - m[k][i] * b for a, b in zip(m[k], m[i])]
    return m[0][3], m[1][3], m[2][3]


def misses(calls: list[dict]) -> list[tuple[int, int]]:
    """(call index, tokens of the previous prompt that came back uncached)."""
    out = []
    for i in range(1, len(calls)):
        lost = calls[i - 1]["prompt_tokens"] - (calls[i].get("cached_tokens") or 0)
        if lost > MISS_SLACK:
            out.append((i, lost))
    return out


def report(results_dir: Path) -> None:
    tasks = {p.stem: usages(p) for p in sorted((results_dir / "traces").glob("*.jsonl"))}
    tasks = {name: calls for name, calls in tasks.items() if calls}
    every = [u for calls in tasks.values() for u in calls]
    if not every:
        print(f"{results_dir.name}: no usage in traces")
        return
    p_unc, p_cached, p_out = fit_prices(every)
    print(f"\n=== {results_dir.name}")
    print(f"fitted $/Mtok: uncached {p_unc * 1e6:.4f}, cached {p_cached * 1e6:.4f}, completion {p_out * 1e6:.4f}")
    print(f"{'task':38} {'calls':>5} {'prompt':>7} {'hit%':>5} {'first':>6} {'last':>7} {'out':>6} "
          f"{'cost$':>6}  unc/cch/out%  misses")
    total = {"calls": 0, "prompt": 0, "cached": 0, "out": 0, "cost": 0.0, "misses": 0}
    for name, calls in tasks.items():
        prompt = sum(u["prompt_tokens"] for u in calls)
        cached = sum(u.get("cached_tokens") or 0 for u in calls)
        out = sum(u["completion_tokens"] for u in calls)
        cost = sum(u.get("cost") or 0.0 for u in calls)
        parts = ((prompt - cached) * p_unc, cached * p_cached, out * p_out)
        split = "/".join(f"{x / (sum(parts) or 1) * 100:.0f}" for x in parts)
        lost = misses(calls)
        print(f"{name[:38]:38} {len(calls):5} {prompt / 1e6:6.2f}M {cached / prompt * 100:5.1f} "
              f"{calls[0]['prompt_tokens']:6} {calls[-1]['prompt_tokens']:7} {out:6} {cost:6.3f}  "
              f"{split:12}  {' '.join(f'#{i}:{n}' for i, n in lost)}")
        for key, value in (("calls", len(calls)), ("prompt", prompt), ("cached", cached),
                           ("out", out), ("cost", cost), ("misses", len(lost))):
            total[key] += value
    unc = total["prompt"] - total["cached"]
    parts = (unc * p_unc, total["cached"] * p_cached, total["out"] * p_out)
    print(f"TOTAL {total['calls']} calls, prompt {total['prompt'] / 1e6:.2f}M "
          f"(cached {total['cached'] / total['prompt'] * 100:.1f}%, uncached {unc / 1e6:.2f}M), "
          f"completion {total['out'] / 1e6:.2f}M, cost ${total['cost']:.3f} = "
          f"uncached ${parts[0]:.3f} + cached ${parts[1]:.3f} + completion ${parts[2]:.3f}; "
          f"{total['misses']} cache misses; "
          f"same run with no cache ${total['prompt'] * p_unc + parts[2]:.2f}")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    for arg in argv:
        report(Path(arg).expanduser())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
