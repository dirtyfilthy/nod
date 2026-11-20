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
  says..."), not quoting or answering the thought. But the thought was
  redundant: Nod's own reasoning had already noted the docstring bug one step
  before the thought arrived, and its reasoning after the thought doesn't
  mention it. The subconscious formed it after entry 6 (the file reads) and
  never saw that reasoning, because an assistant message is only persisted
  together with its tool results, so the subconscious runs a round behind.
- demo-3, demo-4, same hurried prompt with `--off`: both flagged the quantity
  bug unprompted. So demo-2 shows the mechanism, not a benefit.

Fixing the lag. Timestamps from demo-2 showed two causes: the file only gets
Nod's reply once its tool calls finish, and, the bigger one, the subconscious
thinks about as long as Nod does, so the two reason about the same view in
parallel. Now `Subconscious.watch` wraps Nod's provider so tool-calling
replies are visible the moment they arrive, and a finished thought waits for
any call Nod has in flight. If Nod produced anything meanwhile, the thought is
dropped (`stale` in the log) and the subconscious looks again at once with the
new reasoning in view. Waiting costs no delivery time, since a thought only
reaches Nod at its next call anyway.

- demo-5 (only the first half of the fix, stale-dropping on the file view):
  a thought got through ("you'll need Decimal(str(x))"), but Nod's in-flight
  reasoning was already there; it answered "Right —". That's what led to
  waiting on in-flight calls.
- demo-6, demo-7, demo-8 (full fix): each had one thought dropped as stale
  (the same `Decimal(str(...))` / float-subtotal points Nod reached on its
  own), and the relook said `nothing`. The fix does its job.
- demo-8 also: after Nod's final answer the subconscious said "they asked for
  a one-line fix in a hurry and got ... an essay — trim the lecture". That woke
  Nod. Its reasoning first read the thought as the user ("The user is reacting
  — or rather, the italic thought is my own intuition"), and its reply began
  "Fair hit. Leading with the fix:", as if answering a complaint. A thought
  after the final answer turns into a visible message to the user, so the
  inner voice leaks. Options: don't wake on thoughts, hold them until the
  user's next message, or tell Nod a woken turn may just be a second look.
- demo-9: never spoke.

**Verdict:** the plumbing works and it's cheap. The demo is too easy to tell
whether it helps; it needs tasks where Nod actually goes wrong without it.
Next: try it on SWE-bench instances Nod fails (docs/NOD_FAILURES.md), long chat
sessions where an early requirement gets lost, a stronger `--sub-model`, and
check whether thoughts ever lead Nod astray.
