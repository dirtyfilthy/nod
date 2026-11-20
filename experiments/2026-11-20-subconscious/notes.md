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

Fixing the leak. Thoughts no longer wake Nod. `InMemoryInbox.post` got a
`wake=False` option (a core change, on this branch only) that queues an event
without calling subscribers, and the subconscious always uses it. A thought
from mid-turn still arrives before Nod's next call; one that forms after the
final answer waits and surfaces at the start of the next turn, just before the
user's message. The soul note says so. `--task` runs print a held thought
instead of waking Nod. Offline: no subscriber calls, and the held thought sits
right before the next user message.

- chat-1, chat-2, chat-3: the REPL driven through stdin with the hurried task
  and follow-ups ("anything else?", "do your top suggestion"). No spontaneous
  messages, no leaks. But no thought reached Nod either: all 6 that formed
  were dropped as stale, and each relook said `nothing`. The ones I checked
  were things Nod went on to say itself (chat-1: "no test covers that
  quantity≤0 clause"; Nod's answer: "none of them covering the documented
  `qty <= 0` behavior"). The held-after-final-answer path never came up live.
- s-20261120-185110 and s-20261120-185410 (your chats, the second a review of
  ~/src/mine/ratbag): run with `--with-editable nod-extras`, which loaded the
  main checkout's nod, so every thought that passed the stale check crashed on
  `post(wake=False)`. Two were lost: "you didn't actually check — that's a
  claim dressed as a status report" and, the first real catch, "your own
  function-length output listed four over 200 lines (849, 439, 409, 288) —
  the answer says one" (true; Nod's answer said "one over 200" and then cited
  `create_app` at 849 lines). Four ratbag thoughts were dropped as stale; three
  about `.env`/`src/scratch` being tracked were rightly dropped (Nod had run
  `git ls-files`/`git check-ignore`), one about citing a superseded T3 result
  may have been a fair correction. lab.py now puts this checkout's `src/`
  first on the path and refuses to run if nod was imported from elsewhere.
- Twice now (demo-9, ratbag) the subconscious answered with ~9-18k chars of
  invented transcript instead of one line. Parsed as nothing, but costly.
- Open question: is the stale rule now too strict? On short tasks Nod always
  moves before a thought is ready, so the subconscious can only speak during
  slow tool calls or after the final answer. Worth checking on longer tasks
  whether dropped thoughts were ever things Nod didn't get to on its own.

**Verdict:** the plumbing works and it's cheap. The demo is too easy to tell
whether it helps; it needs tasks where Nod actually goes wrong without it.
Next: try it on SWE-bench instances Nod fails (docs/NOD_FAILURES.md), long chat
sessions where an early requirement gets lost, a stronger `--sub-model`, and
check whether thoughts ever lead Nod astray.
