# Prompt resend and caching

The question: SWE-bench Verified's 10 hard instances use about 35M prompt
tokens against 0.5M completion tokens. Is that ordinary agent-loop cost, or is
Nod wasting tokens?

Short answer: it's ordinary agent-loop cost, and the cache absorbs nearly all
of it. 98.6–98.8% of prompt tokens are served from the provider's prefix
cache, and no call in three runs lost its cached prefix. The resend is about a
third of the bill. Without the cache the same runs would cost about 12× as much.
None of the cheap fixes below pays for itself, so nothing in the loop needs to
change.

Data: `deepseek/deepseek-v4.1-flash-1017` pinned to `deepinfra/fp8`, reasoning
effort `high`, on the nodgrader runs `nod-verified-hard-20261029-024353` (no
extras), `nod-extras-verified-hard-20261029-002657` (extras, old jobs prompt)
and `nod-extras2-verified-hard-20261029-050833` (extras, new jobs prompt).
Figures are summed from the traces' `meta.usage`; reproduce them with
`python bench/swebench/token_usage.py RESULTS_DIR`.

## What one request contains

`Agent._run_turn` builds the system prompt once per turn (soul plus skills
index). Before each provider call it sends `[system] + messages` along with
`registry.schemas()`. `OpenAICompatibleProvider._payload` serialises that as
is. So every call carries:

- **A static prefix of about 2k tokens.** That is `SWE_SOUL` (5.3k chars,
  plus 1.5k for `JOBS_SOUL` with extras), the skills index (0.8k chars, 2
  skills) and the tool schemas (1.9k chars). Within a task it is byte-identical
  on every call: the prompt is built once per `_run_turn`, `SkillLoader` sorts
  its `rglob`, and tool order is registration order. It is about 4% of the
  prompt volume.
- **The whole history.** That is the issue, then for each call the assistant
  message *including its reasoning* (`reasoning_details` go back verbatim,
  because DeepSeek's thinking mode expects them inside one user turn) and its
  tool calls, followed by the tool results.

Each call adds about 1.4k tokens: the previous reply (about 750 tokens, of
which about 590 are reasoning) plus the new tool output (about 2.5k chars). So
the context grows roughly linearly, from 2.4–4.6k on the first call to
18–156k on the last. A task averages about 62 calls, and the average request is
about 56k tokens. Prompt volume is therefore roughly quadratic in the number of
calls. That accounts for the 35M.

What the resent volume is made of (weighted by how many later calls resend
each message, measured in characters, no-extras run):

| Part | Share of resent volume |
|---|---|
| Earlier reasoning | 40% (35–39% of cached tokens, measured in tokens) |
| `run_shell` output | 35% |
| `read_file` output | 17% |
| Tool-call arguments (mostly `cud_file` edits) | 6% |
| Issue text and static prefix | ~2% (chars) / ~4% (tokens) |

## What the cache saves

Fitting `cost = a·uncached + b·cached + c·completion` over all 1,944 calls
gives an exact fit (residual about 1e-15). The prices are **$0.098/M
uncached**, **$0.00294/M cached** (a 97% discount) and **$0.294/M completion**.

| Run | Calls | Prompt | Cache hit | Uncached | Completion (reasoning) | Cost | Uncached / cached / completion | No-cache cost |
|---|---|---|---|---|---|---|---|---|
| no extras | 613 | 34.55M | 98.6% | 0.48M | 0.46M (0.36M) | $0.283 | 17% / 35% / 48% | $3.52 |
| extras | 694 | 38.53M | 98.8% | 0.45M | 0.54M (0.42M) | $0.314 | 14% / 36% / 50% | $3.93 |
| extras2 | 637 | 36.75M | 98.8% | 0.45M | 0.48M (0.37M) | $0.291 | 15% / 37% / 48% | $3.74 |

Per task, the hit rate is 96–99% (93.5% on the shortest, `xarray-6992` with
16 calls). The uncached share of the cost runs from 10% on long tasks to 35% on
short ones. Completion is the largest line on every task (41–58%), and about
78% of completion tokens are reasoning.

The uncached tokens are only the new content. Each call's `cached_tokens` is
the previous call's prompt **plus its reply**, rounded down to DeepInfra's
128-token block. The reply comes back cached because the KV cache from
generating it is reused. So what gets billed at the full rate is that call's
new tool output plus the first call's issue. That is the floor: nothing in the
request can be made cheaper by reordering it.

## Things that could break the cache, checked

- **System prompt drift: none.** It is built once per turn, the skill order is
  sorted, and the runs have zero misses. (A miss means a call whose cached
  prefix is more than 256 tokens shorter than the previous call's prompt.)
  Across tasks, the first call of each task hits 1,024 cached tokens of the
  shared prefix. The rest of the prefix differs, probably from the
  per-instance scratch dir in `SWE_SOUL`. That is worth under $0.001 a run.
- **Inbox events: none.** `_deliver_events` appends `kind="event"` messages at
  the end, after the cached prefix. extras2 delivered 2 events, and both
  following calls hit fully. The subagent trace (`pylint-8898_job-1`) is a
  separate session and starts cold, as expected.
- **Compaction: not active in the bench.** `build_bench_agent` passes no
  `CompactionConfig`, and the largest context (156k) is well under the CLI's
  496k trigger anyway. When compaction does run (CLI or server), it breaks the
  cache once. The summarisation request is a fresh prompt and fully uncached,
  and the call after it re-reads the summary plus about 20k kept tokens. It is
  a one-off cost per compaction, not a leak.

## Cheap wins, costed

With cached tokens 33× cheaper than uncached, any change that rewrites history
has to save 33 cached calls' worth of tokens for every token it forces to be
re-read uncached. That rules out most of the usual ideas.

1. **Make the system prompt byte-stable.** It already is. Saving: 0.
2. **`cache_control` breakpoints.** DeepInfra and DeepSeek cache prefixes
   automatically, so there is nothing to gain here. It matters only for
   providers that need explicit breakpoints (Anthropic, some Gemini routes via
   OpenRouter). There the alternative is roughly the "no-cache cost" column:
   about $3.5–3.9 a run instead of $0.28–0.31. Check this when switching
   models (the GLM 5.3 `z-ai/fp8` run is the next data point) rather than
   adding it pre-emptively. If a model needs it, a single `cache_control` on
   the last message in `_payload` is the plain version.
3. **Lower the compaction threshold.** Simulated on the real per-call deltas,
   with a 20k keep, a 3k summary and 2.5k summary output:

   | Threshold | Compactions (3 runs) | Cost change |
   |---|---|---|
   | 128k | 1–2 | +3% to +6% |
   | 96k | 4–5 | +5% to +10% |
   | 64k | 11 | +11% to +13% |
   | 48k | 23 | +23% to +27% |

   It costs more at every threshold, before counting what the model loses with
   the context. Leave the bench without compaction and the CLI at its current
   threshold.
4. **Trim old tool output.** Tool output is about half the cached volume, so
   even deleting all of it for free would save about $0.05 a run (17%). It
   isn't free: trimming a message invalidates the cache from that message on.
   Trimming every call (a sliding window) makes everything after the cut
   uncached on every call, which is far worse than now. Trimming in batches is
   just compaction, and lands in the row above. Not worth it. The cap that
   matters, truncating huge outputs before they enter history, already exists
   in `builtins.py`.
5. **Stop resending earlier reasoning.** Reasoning is 35–39% of cached tokens,
   so this would save about $0.035–0.04 a run (about 13%). It is the biggest
   real saving on the list, but it goes against how DeepSeek's thinking mode
   is meant to run tool loops within one user turn: the model expects its
   earlier reasoning back until the next user message, and dropping it risks
   the resolve rate. Not recommended.
6. **Reasoning effort.** This is not a resend issue, but completion is the
   largest cost line (48%) and mostly reasoning. Dropping `--reasoning-effort`
   from `high` would cut it, but it is a quality knob, and the bench is the way
   to price it.

## Recommendation

Change nothing in the loop. The 35M prompt tokens are the expected quadratic
resend of a 60-call tool loop. The prefix cache hits on every call and turns
them into about $0.10 of a $0.28–0.31 run. The only lever above 15% is
dropping earlier reasoning, and it trades correctness for about 4 cents a run.
Re-run `token_usage.py` on any run with a new provider or model: a hit rate
well below about 95%, or misses showing up, is the signal that caching isn't
working there.
