# Background jobs: design note

Status: **proposed, not implemented.** Checked against the code on
2026-10-27; the corrections from that review are folded in below.

A design for letting Nod start work in the background, carry on with other
work, and be told automatically when each piece finishes, fails or times out.
A *job* is generic: anything Nod starts that runs off the main loop, has a
deadline, can be cancelled and ends with a text result. Two kinds are planned:

- **shell**: a command running in its own process group;
- **agent**: a subagent working on a task in its own child session.

Written down now so the shape is settled before anyone builds it.

---

## Why `run_shell` can't do this today

`run_shell` is `subprocess.run(..., stdout=PIPE, timeout=60)`
(`src/nod/builtins.py`; the bench raises the timeout to 180s). That has two
consequences:

- **A bare `&` hangs.** A backgrounded child inherits the stdout pipe, and
  `subprocess.run` waits for EOF on it. So `long_task &` blocks until the
  timeout. The timeout returns whatever output was captured so far, but it
  kills only the shell: the backgrounded child keeps running and its later
  output is lost. Verified:

  ```
  'sleep 5 &'                          TIMEOUT after 3.0s
  'nohup sleep 5 > /dev/null 2>&1 &'   returned in 0.01s
  ```

- **Nothing reports back.** Even a correctly detached job
  (`nohup setsid cmd > log 2>&1 < /dev/null &`) is invisible to the harness.
  The model has to remember it and poll it, usually with `sleep` loops, which
  burn turns.

There is no way at all to hand a task to a subagent. The harness should own
both.

## The hard part: waking the agent

Running something in the background is easy. The hard part is that the loop in
`agent.py` is synchronous and only runs when something calls `Agent.send`.
A job can finish at two different moments, and each needs its own delivery
path:

1. **Mid-turn.** The agent is still calling tools. The event can go into the
   conversation before the next provider call.
2. **Idle.** The turn ended ("I'll check when it's done"). Nobody is calling
   `send`, so the harness has to call it itself.

## Design

### 1. A job manager and job kinds

Everything that doesn't depend on what the job *is* lives in one `Jobs`
manager. A kind only knows how to start, check and stop its own jobs:

```python
@dataclass
class JobResult:
    status: str          # "done" | "failed" | "timed_out" | "cancelled" | "lost"
    summary: str         # e.g. "exit code 3", "final reply", the exception
    body: str            # short tail or result, already truncated
    full_output: str     # where the rest lives: a log path or a session id

class JobHandle(Protocol):
    def poll(self) -> JobResult | None   # None while still running
    def cancel(self, reason: str) -> None   # reason is "cancelled" or "timed_out"

class JobKind(Protocol):
    name: str                            # "shell", "agent"
    def start(self, job_id: int, session_id: str, **spec) -> JobHandle
```

The `Jobs` manager owns:

- **Ids and a per-session job table.** Ids are small integers per session so
  the model can say `kill_job(3)`. They continue from the job record across
  restarts rather than starting at 1 again: child sessions are named by id and
  `Memory` is append-only, so a reused id would append a new job's transcript
  onto an old one's file.
- **Deadlines.** Every job has one: a default per kind with a hard cap, and
  never a job with no limit. On expiry the manager calls
  `cancel("timed_out")`.
- **Concurrency limits** per kind and per session. Agent jobs get a much lower
  limit than shell jobs because they spend money (see Guardrails). Limits are
  per session (running plus queued), not per turn: the manager can't see turn
  boundaries without a hook from `Agent`, and the per-session cap bounds the
  same spend.
- **One reaper thread** that polls running jobs and, when one ends, posts a
  `<job-finished>` event to that session's inbox (see 4) and records the event
  id the inbox returns on the job. That id is how `wait_jobs` finds the event
  and takes it out of the inbox.
- **A job record on disk** (`<jobs_dir>/<session>/jobs.jsonl`: id, kind,
  pid or child session, start time, deadline, owning Nod pid). This is what
  lets a restarted Nod find jobs started by a dead process and report them as
  `lost` instead of forgetting them silently, and where the next id comes
  from. `jobs_dir` defaults to `/var/tmp/nod-jobs`; the bench points it at
  the scratch dir (see the shell kind).
- **Cancelling every job on shutdown** (`atexit` plus `SIGTERM`/`SIGINT`
  handlers). This doesn't run if Nod is `SIGKILL`ed, which is why the job
  record exists; the shell kind's process groups outlive Nod in that case.

Every kind uses the same status words, so the event format is the same:

```
<job-finished id=3 kind=shell status=timed_out duration=1800s>
command: pytest -x tests/
timed out after 1800 s; process group killed
--- last 50 lines (full log: /var/tmp/nod-jobs/default/3.log) ---
...
</job-finished>
```

`timed_out` and `cancelled` are statuses in their own right, never inferred
from a signal exit code such as -15.

### 2. Kinds

| | shell | agent |
|---|---|---|
| Start | `Popen(..., start_new_session=True)`, stdout and stderr to a log file | A thread runs `Agent.send(task, session_id="<parent>/job-<id>")` on a child agent |
| `done` result | Exit code and the last ~50 lines of the log | The child's final reply, truncated |
| Full output | The log path; readable while running with `read_file` / `tail` | The child's session JSONL, which `Memory` already persists |
| Cancel | `SIGTERM` to the group, ~5s, then `SIGKILL` | Cooperative: a flag checked before each provider call |
| After a restart | `lost`; the process may still be running | `lost`; its transcript survives |

**Shell kind.**

- The log file, not a pipe, avoids the hang above and needs no reader thread.
- Logs go under the directory `run_shell` already uses for truncated output
  (`shell_output_dir`). On the bench the file tools run on the host and `tail`
  runs in the container, so a log under `/var/tmp` is readable by one and not
  the other; the scratch dir is bind-mounted and visible to both.
- Cap the log size (e.g. 50 MB). Past the cap, the kind stops the job with
  `failed: log exceeded N MB` rather than filling the disk.
- **Commands routed through `shell_argv`** (the bench runs them via
  `docker exec`) break the process-group kill. `killpg` kills the `docker exec`
  client on the host, and the process inside the container keeps running. The
  bench's foreground `run_shell` avoids this by wrapping each command in
  `timeout -k 5 <secs>` inside the container, and the same wrapper can enforce
  a background job's deadline. Cancelling early still needs a way in: the kind
  starts such jobs as `setsid sh -c '...' & echo $!` inside the container,
  records that pid, and kills its group with a second `docker exec`. So
  `builtin_tools(shell_argv=...)` needs a matching kill hook, not just an
  argv.

**Agent kind.**

- **Child agent.** A factory builds the child from the parent's parts: the
  same provider and memory, a task-focused soul, and a trimmed tool list.
  Children get no `spawn_agent` (no nesting), so one parent's spend is bounded
  by its own limits. Whether children may start shell jobs is a factory
  choice; the default is no.
- **Cancellation without touching core.** Python can't kill a thread. The kind
  wraps the child's provider in one that raises `JobCancelled` before each
  `complete` once a flag is set. The exception leaves `send`, and everything up
  to the last tool round has already been persisted, so the transcript stays
  readable. Timeouts use the same flag. A provider call or a long `run_shell`
  that is already running finishes first, so a cancel can take up to one
  provider call or one shell timeout to land.
- **Result.** The child's final reply is the result. If the child hit
  `max_turns`, the status is still `done` but the body starts with the
  "(stopped: hit the max_turns limit...)" note, so the parent can tell.
- **Concurrency inside one process.** `Agent.send` keeps its state in locals,
  each session has its own JSONL file, and the provider opens a fresh
  `httpx.Client` per call, so several sessions can run at once. `SkillLoader`
  is the one shared part that isn't safe: `reload` empties `_by_name` and
  refills it on every access, so a child calling `read_skill` mid-reload gets
  "no such skill" and the parent can get an empty index for a turn. Build the
  new dict and swap it in one assignment before shipping this.
- **Shared filesystem.** A subagent works in the same directory with the same
  file tools as its parent. For `cud_file` this is already safe enough:
  - A process-wide lock (`_CUD_LOCK` in `builtins.py`) runs one `cud_file`
    call at a time. Two agents can't lose each other's edits between a read
    and a write, and a multi-block edit can't be interleaved with another.
  - Updates are SEARCH/REPLACE anchored on exact text, and creating a file
    fails if it exists. So an edit made from a stale view fails with
    `SEARCH text not found` instead of clobbering. The error tells the
    model to re-read the file.

  There are deliberately no per-file leases held across turns: they would
  need expiry, release on cancel and model cooperation, and the anchored
  format already handles stale views. **Shell edits are not covered.** A
  `sed -i` or a formatter run by one agent still races with the other's
  edits; coordinating that is left to the parent's instructions.

### 3. Tools

Each kind gets its own start tool; managing jobs is shared:

| Tool | Behaviour |
|---|---|
| `start_shell_job(command, timeout=None)` | Returns at once: `started job 3 (shell), log: /var/tmp/nod-jobs/.../3.log` |
| `spawn_agent(task, timeout=None)` | Returns at once: `started job 4 (agent), session: default/job-4` |
| `list_jobs()` | Id, kind, status, age and a one-line description of each job in this session. |
| `kill_job(id)` | Cancels the job and reports it. Its `cancelled` event is still delivered. |
| `wait_jobs(ids, max_seconds)` | Blocks until any listed job ends or `max_seconds` passes, then returns their events. |

Per-kind start tools are deliberate. A single `start_job(kind, spec)` with a
union schema looks tidier, but weaker models fill union schemas in badly, and
separate tools let `build_agent` leave out subagents just by not registering
`spawn_agent`. The core `run_shell` stays as it is.

`wait_jobs` fits the synchronous loop well: start three jobs, do other work,
then wait instead of looping on `sleep`. It is the main way to use subagents
(fan out, then gather). Two constraints:

- It **takes its events out of the inbox**, so the same event isn't injected
  again before the next provider call. The inbox has no notion of jobs, so
  `wait_jobs` can't ask it for "job 3's event"; instead the reaper records the
  event id each `post` returns on the job, and `wait_jobs` acknowledges those
  ids (see 4). Each event is consumed exactly once, by whichever path gets to
  it first; the session lock serializes the two paths.
- `max_seconds` is capped (e.g. 300s). Under HTTP, a blocked `wait_jobs`
  holds both the request and the session lock, and the cap has to stay below
  the client's timeout.

### 4. Delivery

**Mid-turn: an `Inbox` the loop empties before each provider call.** The
inbox is a list of `(event_id, text)` per session with three operations:
`post` returns the new event's id, `pending` lists what hasn't been
acknowledged, and `ack` removes by id. Before each `provider.complete`,
including the first one in a `send`, `Agent` appends each pending event as a
user message, persists, and only then acknowledges:

```python
events = self.inbox.pending(session_id)
for event in events:
    messages.append(Message(role="user", content=event.text, kind="event"))
self.memory.persist_messages(session_id, messages)
self.inbox.ack(session_id, [e.id for e in events])
```

Three details of that order matter:

- **Persist before ack, and no destructive drain.** The loop today persists
  only after a tool round, and a `ProviderError` leaves `send` without
  persisting. A drain followed by a failed provider call would lose the event
  for good. Acknowledging after the persist means a failed call leaves the
  event in the inbox for the next attempt. The same persist closes an existing
  gap: a user message whose first provider call fails is currently never
  written either.
- **Events before the user's text.** At the start of a `send`, pending events
  are appended *before* the new user message. They happened before the user
  spoke, and it avoids two adjacent user messages for providers that mind.
- **`kind="event"` on the stored message.** The provider still sees a plain
  user message (`to_provider_dict` only special-cases `compaction_summary`),
  but transcripts and the bench analysis can tell a job event from something
  the user typed.

It has to be a `user` message, not a `tool_result`. The original tool call
was already answered with "started job 3", and providers require every tool
call to have exactly one matching result.

An event that arrives during the turn's *last* provider call, the one with no
tool calls, stays in the inbox. In phase 1 it is delivered at the start of the
next `send`; phase 2 delivers it with a wake-up.

**Idle: gateways that start a turn on their own.** A wake-up is not
`send(event_text)`. If it were, an event delivered mid-turn by a running
`send` would be delivered a second time by the wake-up queued behind it.
Instead the gateway calls `agent.wake(session_id)`, which takes the session's
lock and runs a turn only if the inbox has anything pending. Two core changes
make that possible:

- **The session lock moves into `Agent`.** Today the only per-session lock is
  `_SessionLocks` in `server.py`, and the CLI has none. A wake-up can only be
  serialized against a user request if both take the same lock, so `Agent`
  owns one lock per session, `send` and `wake` take it, and the HTTP handler
  drops its own. Every inbox access for a session then happens under that
  lock, which is what lets `wait_jobs` and the loop share the inbox without a
  race of their own.
- **A turn with no new user text.** `send` today is "append the user's text,
  then loop". It splits into `send(user_text)` and `_run_turn(session_id,
  messages)`; `wake` loads the session, appends the pending events as above,
  and calls `_run_turn` directly. The events are the turn's user input.

Delivery per gateway:

- **CLI (`CLIGateway`):** `input()` blocks, so it can't be woken. Feed user
  lines (from a reader thread) and inbox notifications into one queue, and
  handle whichever arrives first. Wake-up output is printed like a normal
  reply, which means it can land over a half-typed line; reprinting the prompt
  and the partial line afterwards is a nicety, not a requirement.
- **HTTP (`HTTPGateway`):** the reply from a wake-up needs somewhere to go.
  The default is to keep it and attach pending wake-up replies to the next
  response for that session, which adds one field to `POST /chat` and leaves
  the stdlib server as it is. A per-session webhook URL or an SSE stream would
  deliver sooner, but each is a new surface on a server that has two endpoints
  on purpose; leave them out until someone needs them.

### 5. Guardrails

- **Spend.** An agent job costs model calls, not just CPU. Limit agent jobs
  per session (e.g. 3 running, 10 running or queued), on top of the child's
  own `max_turns`. The cap is per session rather than per parent turn for the
  reason given under the job manager: the manager can't see turn boundaries.
- **Wake-up loops.** Each wake-up is a new turn with a fresh `max_turns`.
  Cap the number of automatic wake-ups in a row without user input (e.g. 5),
  or a job → wake → new job → wake cycle can run, and spend, forever.
  Subagents make this the expensive loop, so the cap is required, not
  optional, once the agent kind exists. When the cap is hit, `wake` does
  nothing: the events stay in the inbox and are delivered at the start of the
  next user `send`, which also resets the counter.
- **Restarts.** Jobs don't survive a restart. On startup, records left by a
  dead Nod process are reported as `lost` (with the pid, for shell jobs, so
  the model or the operator can check whether it is still running).
- **Untrusted output.** A log tail or a subagent's reply is tool output, not
  instructions. The `<job-finished>` tag and its framing should make that
  clear.
- **Persistence.** Notifications are ordinary user messages (with
  `kind="event"`), so they go into the session JSONL, and compaction and
  replay work without changes. The inbox itself is in memory: an event that
  hasn't been acknowledged when Nod dies is lost, which is fine for jobs
  because the job record reports them as `lost` anyway. Any other source that
  posts to the inbox (cron, email, webhooks) has to be able to re-post, or
  bring its own durable queue in front of the inbox.

## What goes in core vs `nod-extras`

Following the thin-core rule:

- **Core (small, general):**
  - an `Inbox` protocol (`post(session_id, text) -> event_id`,
    `pending(session_id) -> list[Event]`, `ack(session_id, event_ids)`)
    injected into `Agent`, with an in-memory default;
  - the append-persist-ack step before each provider call;
  - a per-session lock owned by `Agent`, which `send`, `wake` and the HTTP
    handler all take;
  - `send` split into `send` and `_run_turn`, plus `Agent.wake(session_id)`;
  - gateways that call `wake` when the inbox is posted to.

  That is under a hundred lines, and it serves any asynchronous source (cron,
  email, webhooks), not just jobs. The inbox is the general channel; jobs are the
  things Nod started itself and owns.
- **`nod-extras`:** the `Jobs` manager, the reaper, the job record, the shell
  and agent kinds, the child-agent factory and the job tools, wired in through
  `cli.py:build_agent`.

## Phasing

1. **Mid-turn, shell kind.** `Jobs`, the inbox step and the session lock in
   `Agent`, the shell kind, `start_shell_job` / `list_jobs` / `kill_job` /
   `wait_jobs`. Covers
   "run tests and lint in parallel while I edit" without touching the
   gateways.
2. **Agent kind.** The child factory, the cancelling provider wrapper,
   `spawn_agent` and the spend limits. Still mid-turn only, so subagents are
   used through `wait_jobs`.
3. **Idle wake-up.** The `send` / `_run_turn` split and `Agent.wake`, the
   CLI event queue, then HTTP delivery (replies attached to the next
   response), plus the limit on consecutive wake-ups.

## Related, cheaper win

When one model reply contains several tool calls, `Agent.send` dispatches
them one after another. Running them in a thread pool gives some parallelism
within a turn without any of the above. Results must still be appended in the
order of the calls. `cud_file` calls are already serialized by its lock, but
two `run_shell` calls running at once can clash on the same files, just like
subagents. It is also not free: models routinely put dependent calls in one
reply (an edit, then the tests), and that works today because the calls run
in order. Parallel dispatch only holds up if the soul tells the model that
calls in one reply run at the same time, so it is a soul change as well as a
loop change.

## Testing

Use `FakeProvider` with callables that inspect the messages they receive,
plus short real commands (`sleep 0.1; exit 3`, `sleep 5` with a 0.2s
timeout), to check:

- a start tool returns immediately, for both kinds;
- the event shows up as a `<job-finished>` user message before the next
  provider call;
- an event returned by `wait_jobs` is not injected again;
- a wake-up after the loop delivered the event mid-turn doesn't run a turn;
- a shell timeout kills the whole process group (no surviving children);
- cancelling an agent job stops it before its next provider call and leaves
  its child session readable;
- a child agent has no `spawn_agent` tool;
- job records from a dead pid are reported as `lost` on startup;
- the limit on consecutive wake-ups stops a self-renewing loop;
- an event survives a provider call that raises: it is in the transcript or
  still in the inbox, never neither;
- at the start of a `send`, a pending event precedes the new user message;
- a job started after a restart doesn't reuse an id from the job record.
