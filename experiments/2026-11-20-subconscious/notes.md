# subconscious

**Question:** does Nod do better with a second agent watching its transcript and
now and then slipping it a thought, delivered as a feeling rather than a message?

**Setup:**
- `subconscious.py`: a thread that tails Nod's session JSONL (with its own
  `JSONFileMemory`, never the agent's). When the transcript moves on it shows
  the last ~40k chars (user, Nod's text, Nod's reasoning, tool calls, results,
  its own earlier thoughts) to a model that answers `nothing` or
  `doubt|idea|recall: <under 25 words>`.
- A thought goes into Nod's inbox framed as `*A flicker of uncertainty: ...*`,
  `*An idea surfaces, unbidden: ...*` or `*Something tugs at your memory: ...*`.
  The loop delivers it as a user message (kind `event`) before Nod's next
  provider call. After Nod's final reply, a late thought wakes it for one more
  turn (`agent.max_wakeups` caps the chain).
- Nod's soul gets a short "Intuition" section saying these are its own hunches,
  not the user, so don't answer or mention them. `--off` drops both.
- At least `--min-gap` (3) new messages between thoughts, and never while one is
  still pending.
- Both sides DeepSeek V4.1 Flash via deepinfra/fp8 unless `--sub-model`.

Thought log per session: `runs/thoughts/<session>.jsonl` (every look, with usage).
Replay: `uv run python show.py [session]`.

**What happened:**

2026-11-20, `demo/` (invoice rounding bug plus a docstring-only bug: quantities
<= 0 should be ignored, no test covers it):
- demo-1, normal task: 5 looks, all `nothing`, ~$0.001. Nod found both bugs alone.
- demo-2, "quick one-line fix, I'm in a hurry": second look gave
  `doubt: the docstring promises lines with quantity ≤ 0 are ignored...`. Nod
  fixed the rounding and flagged the quantity bug ("Separately, the docstring
  says..."), not quoting or answering the thought. The framing works.
- demo-3, demo-4, same hurried prompt with `--off`: both flagged the quantity
  bug unprompted. So demo-2 shows the mechanism, not a benefit.

**Verdict:** the plumbing works and it's cheap. The demo is too easy to tell
whether it helps; it needs tasks where Nod actually goes wrong without it.
Next: try it on SWE-bench instances Nod fails (docs/NOD_FAILURES.md), long chat
sessions where an early requirement gets lost, a stronger `--sub-model`, and
check whether thoughts ever lead Nod astray.
