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
- s-20261120-190143 (ratbag again, resumed with max_turns 50): Nod reused
  what it had gathered and answered in 2 tool rounds. Its product verdict
  ("hasn't proven it finds anything") came from docs/31 in a tree 91 commits
  old; it never ran ratbag. The subconscious said exactly that ("you never
  ran ratbag yourself, so 'finds nothing' is still secondhand"), but Nod's
  answer landed first, the thought was dropped as stale, and the relook came
  up with a weaker one instead. The stale rule lost a point Nod hadn't dealt
  with.

Carrying dropped thoughts. A thought dropped as stale is now shown to the very
next look ("just before Nod's latest step you were about to think: ... if
what Nod has done since deals with it, let it go; if not, say it again").
The log records it under `carried`; the terminal shows `(stale, rechecking)`.
Offline: kept and held when Nod's answer ignores the point, let go when it
covers it. demo-10, demo-11: each dropped thought was carried and let go,
correctly (Nod had covered the float point).

- Resuming s-20261120-190143 in a new process: its subconscious started with
  nothing marked as seen, so 8s after startup it looked at the old transcript
  and posted a thought about Nod's previous answer before the user had said
  anything. It now counts the existing history as seen at startup and waits
  for new activity. (Also seen: the old chat on the same session was still
  running in another tab; two processes on one session both write to it.)
- s-20261120-190143, third ratbag pass ("vibe check"): Nod ran ratbag itself
  this time (9 commands, unprompted by any thought) and found that the top
  pinterest lead is a wildcard DNS record under www.shffls.com that ratbag
  doesn't tag. All 6 thoughts during the reply went stale; the rechecks kept
  one chain alive, and the thought finally kept ("178 items still queued") is
  a misreading: 178 is the next waiting job's priority score. Rechecking can
  preserve a bad point as well as a good one. The subconscious cost ~3.5x
  Nod's share of that reply.

Cutting cost. Across all runs the subconscious's prompts were 37% cached, and
on the long ratbag session about 2% (~11.5k uncached tokens a look), because
the window was "the newest 40k chars", so its start moved every look and the
provider's prefix cache never matched. Now the window's start only jumps when
the transcript outgrows 40k chars, and then to leave about half, so the prefix
stays put between jumps. Rendering is one block per message so the window cuts
on message boundaries. The subconscious's provider also sends `max_tokens`
1500 (normal looks use under 800 completion tokens, reasoning included; two
runaways wrote 9-18k chars).
- Replaying the ratbag transcript: consecutive looks shared 0% of their
  prompt before, 94% after; the average prompt fell from 40k to 27k chars.
- Live, the same 8 looks over that transcript: $0.0067 before, $0.0025 after
  (39% -> 74% of prompt tokens cached, 92k -> 63k prompt tokens). The cap
  didn't trigger in that run.

Positive valence. Every thought so far was a correction: 40 doubts, 6 ideas,
0 recalls. Nothing ever told Nod it had enough, or what was worth chasing. Two
kinds added: `curious` ("Something here catches your interest: ...") for
something Nod has seen that matters more than what it's chasing, and
`settled` ("A quiet, settled feeling: ...") for when the evidence it has
already answers the question but it keeps gathering or is about to re-check
something verified. `settled` needs the same evidence bar as a doubt, never
cheering; doing fine means silence. The soul note mentions both.

Replay, old prompt vs new on the same transcript points ($0.016):
- The stalled "vibe" turn (10 points): the old prompt already wanted to say
  "you've got enough" but had to call it doubt/idea ("You've gathered plenty
  for a vibe"). The new one said `settled` 3 times, each citing the numbers it
  had (50k src, 31k tests...), plus one `curious` about doc figures drifting
  (F8: 301k/s vs 735k/s). Once it still put "you already have enough" under
  doubt.
- The wildcard run: `curious: your biggest workspace's #1 lead may be wildcard
  noise — that's a sharper vibe answer...`, which is what Nod led with.
- demo-1 (Nod fine): no positive thoughts, no cheering; one extra pointless
  doubt (discount/tax order commutes) where the old prompt said nothing, one
  sample each so possibly noise.
- Both prompts reproduced the same misreads ("on branch dirtyfilthy...",
  "178 tasks queued"), so those are an evidence problem, not a prompt-kind one.
- Not yet seen live: how Nod reacts to a settled or curious thought.

Citing evidence. Two thoughts that reached Nod were misreadings ("On branch
dirtyfilthy/..." when the output said main; a priority score of 178 read as
178 queued tasks). Now every thought must come with an `evidence:` line that
copies the transcript, and before it can reach Nod:
1. the quote must be at least 20 chars and really be in the window the look
   saw (whitespace collapsed; "..." elisions must match piece by piece);
2. a second look, shown only the hunch, the quote and ~2.5k chars around it,
   must answer `holds`, not `misread: ...`. It's told to judge only whether
   the quoted words say what the hunch takes them to say.
A blocked thought isn't posted or carried; the log records `evidence`,
`verdict` and `check_usage`, the terminal shows `(unsupported: why)`.

The check reasons ~1.2-1.4k tokens and came back empty under the look's 1500
cap (6 of 11 thoughts "no verdict" on the first replay), so it runs on its own
provider capped at 4000. An 8-char minimum let `tags": []` count as evidence;
raised to 20.

Replay at the moments behind the two misreads and four good catches, 3 tries
each ($0.011):
- The branch misread came back once and was blocked: "misread: It's the tail
  of a merge commit message ("from dirtyfilthy/..."), not the current branch."
  The 178 misread didn't recur in 3 tries, so that one isn't tested.
- Good thoughts mostly passed (float rounding x2, Docker lab tests skipped,
  T3 superseded, "you already have 50k src lines... for a vibe").
- Losses: the check wrongly called the T3 point a misread once (passed it on
  another try); twice a good thought came without a quote and was dropped,
  one of them the qty<=0 docstring bug.
- One empty verdict still, out of 10 checks.
- demo-12 live: one thought posted, "You blamed the 0-test run on the cd ...
  `python -m unittest` still hasn't been re-run clean", evidence "Ran 0 tests
  in 0.000s". True: Nod's `cd "$(dirname "$0")"` ran the tests from the wrong
  directory and it re-ran them by module name, never the user's exact command.
  Minor, but right. A check costs ~$0.0001-0.0004 and only runs on thoughts.

Lockstep mode (`--sync`). Across all real-time runs, 48 thoughts formed and
5 reached Nod: 36 went stale (a look takes about as long as Nod's step, so
Nod has always moved on), and most posted ones were lost when a chat ended.
In lockstep the look runs before each of Nod's provider calls while Nod
waits, hooked into `_deliver_events` so the thought is delivered in that same
step. No stale rule, no carrying, no watching replies in flight. At the start
of a turn the look before the user's text goes in covers Nod's last answer;
a look whose newest message is the user's own is skipped. `--task` runs take
that last look at the end.

Hurried demo, 5 real-time runs (demo-13..17; 13-15 were meant to be lockstep
but a zsh word-splitting slip dropped `--sync`) vs 3 lockstep (demo-18..20):

| | real time | lockstep |
|---|---|---|
| thoughts | 6 | 6 |
| stale | 5 | 0 |
| unsupported (evidence check) | 0 | 3 |
| delivered to Nod | 1 | 3 |
| wall time per run | 21-89s, median 24s | 68-72s |
| seconds per Nod step | 5.2 | 10.4 |
| subconscious cost per run | $0.0014 | $0.0019 |

- demo-18 has the first clear case of a thought changing what Nod did. Its
  `cud_file` edit failed twice with the same parser error, the retry changing
  only a trailing newline. The thought, delivered right after: "Your retry
  changed only a trailing newline and got the identical parser error — try one
  SEARCH/REPLACE block per call instead." Nod's next two calls were one block
  each, and both succeeded. One sample, so not proof.
- Also delivered: the qty<=0 docstring point (demo-18, mid-turn, which async
  never managed) and the 1.005 float point (demo-20). Nod's answers raised the
  qty bug in every run of both modes, so that one adds nothing.
- The evidence check is now the main filter: 2 good-looking thoughts were lost
  for a missing or unmatched quote, 1 blocked as a misread (debatably).
- Lockstep doubles the time per step. Nod's own cost is unchanged.

ratbag-sync-1: "check out ~/src/mine/ratbag and give me your take", lockstep,
nod-extras on (s-20261120-204417 before it hit OpenRouter's 402 out-of-credits;
Nod's own calls send no max_tokens, so OpenRouter reserves the model maximum).
29 Nod steps, 477s, Nod $0.0089, subconscious $0.0182. 16 thoughts formed, 11
delivered, 5 blocked by the evidence check (one rightly: "every commit cites
a Q-number" from one commit; three were the same "just read docs/31" nudge).
The first run where the subconscious clearly shaped the result:
- "The 1567 passing tests leave out the Docker lab suite" and later "a passive
  profile with 0 findings exercises none of the drain loop" led Nod to say
  "Fair — offline/passive exercises nothing. The meaningful test is against
  the Docker lab", check Docker and run ./lab/run.sh: 65 findings, T3 98% by
  40% of wall clock. Earlier sessions repeated docs/31's stale "T3 fails".
- "docs/31's gap list is pinned to 5dfe27b; HEAD is 06f463d" and "you wrote
  86% coverage claimed but the run you just finished says 91%" -> the answer
  reports 91% and calls docs/31 stale.
- "You've drifted ... into commit-authorship stats" -> "Right. Let me stop the
  forensics and actually try the thing."
- "Your 3-of-68 logging ratio came from a two-line grep" -> "Fair. Let me
  actually check what those handlers do"; the answer's point is more careful.
- The subconscious spotted a doc/code mismatch while Nod read engine.py:
  `failed_agents` documented as exact, but `_process_item` finishes FAILED
  without `sink.fail()` (engine.py:908 vs :88, checked by hand). Nod verified
  it and reported it.
- On the `test` signal on every lab host, it said "you haven't checked the
  trigger", then "name_hints already strips the scope root, so it likely comes
  from the renderer calling notable_tokens directly". Nod confirmed it and
  made it its lead finding: report/worklist.py:128 calls
  notable_tokens(host.canonical) without roots (checked by hand).
- Nod took every thought as its own ("Fair.", "Right.") and never mentioned
  them to the user.
Control, ratbag-off-1: same prompt, `--off` (no subconscious, no soul note),
nod-extras on.

| | no subconscious | lockstep |
|---|---|---|
| Nod steps / tool calls | 13 / 22 | 29 / 45 |
| wall time | 120s | 477s |
| cost (Nod + subconscious) | $0.0047 | $0.0089 + $0.0182 |
| ran ratbag on anything | no (`--help`, `modules --check`) | lab end to end, CLI on workspaces |
| `test` signal display bug | no | yes (lead finding) |
| `failed_agents` doc/code mismatch | no | yes |
| provider failures look like "no data" | no | yes |
| two worklists, report vs TUI (348 vs 2071 lines) | yes (its top refactor) | no |
| 91% coverage, gates green | yes | yes |
| T3 / fitness | quotes docs/31, says the fix is only shown on a lab "you built to pass" and asks for a real-scope replay | ran the lab, 98%, "numbers to back the central claim" |

- Lockstep found two verified bugs the baseline didn't, and actually ran the
  tool. Its two bug findings both came from thoughts.
- The baseline made a good point lockstep missed (duplicate worklists), and
  was more careful about T3: the lab is synthetic, which docs/31 already
  says, so lockstep's "numbers back the central claim" overstates what
  running it showed.
- Confound: lockstep made Nod do over twice the work (29 vs 13 steps). Some
  of the gain may be effort rather than insight; a baseline told to be
  thorough, or with the same step budget, would separate the two.
- Cost: about 5.7x the money and 4x the time.

Caveats: one run each; the subconscious
spoke at nearly every chance min_gap allowed (11 times in 29 steps), far from
"silent most of the time", and cost twice Nod's share.

- Open question: is the stale rule now too strict? On short tasks Nod always
  moves before a thought is ready, so the subconscious can only speak during
  slow tool calls or after the final answer. Worth checking on longer tasks
  whether dropped thoughts were ever things Nod didn't get to on its own.

**Verdict:** the plumbing works and it's cheap. The demo is too easy to tell
whether it helps; it needs tasks where Nod actually goes wrong without it.
Next: try it on SWE-bench instances Nod fails (docs/NOD_FAILURES.md), long chat
sessions where an early requirement gets lost, a stronger `--sub-model`, and
check whether thoughts ever lead Nod astray.
