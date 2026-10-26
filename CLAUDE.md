# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Nod is a deliberately thin agent core: rent the brain (any OpenAI-compatible endpoint, OpenRouter by default), build the scaffold yourself. Keeping it small and readable is the design goal, so prefer the plain option over the clever one. The runtime dependency is `httpx` and nothing else; the HTTP server is stdlib `http.server` on purpose. The "millions of skills" and anything richer than the five built-in tools belong in the sibling `nod-extras` repo, not here. `SOUL.md` is Nod's persona and system prompt, not docs for contributors.

## Commands

The project uses `uv` (Python >=3.11). There's no linter or formatter configured.

```sh
uv run --extra dev pytest -q                                   # full suite
uv run --extra dev pytest -q tests/test_tools.py::test_dispatch_unknown_tool   # one test
uv run nod                         # interactive CLI chat (needs OPENROUTER_API_KEY)
NOD_API_TOKEN=... uv run nod serve # HTTP service on 127.0.0.1:8080
uv run python bench/swebench/run_nod.py --dry-run --count 5    # list SWE-bench instances, no spend
```

Tests never call a real model: `tests/conftest.py` has `FakeProvider`, which plays back scripted replies (or callables that inspect the messages the loop sends) and records every `complete` call.

## Architecture

- **The loop** (`agent.py`): `Agent.send` loads the session from `Memory`, compacts it if needed, appends the user message, then repeats: call the provider, run every tool call through the `ToolRegistry`, persist. It stops when a reply has no tool calls or `max_turns` is hit, and in the second case it appends a "(stopped: hit the max_turns limit...)" note. The system prompt is assembled fresh each turn from the soul plus the skills index and is never stored. `send` takes the session's lock (one per session, owned by `Agent`), delivers pending inbox events before the user text, and hands off to `_run_turn`. Before every provider call `_run_turn` appends the inbox's pending events as user messages with `kind="event"`, persists, and only then acknowledges them, so a failing provider call never loses one. `Agent.wake(session_id)` runs a turn with pending events as its only input and returns `None` when nothing is pending or `max_wakeups` consecutive wake-ups have run without a user `send` (which resets the count). `current_session()` is a context variable set for the duration of a turn, for tools that need to know their session.
- **Inbox** (`inbox.py`): `post`, `pending`, `ack` and `subscribe` per session; in memory. It is the general channel for anything asynchronous; jobs are one source. Gateways subscribe to it and call `agent.wake` when a post lands while the agent is idle. An event not yet acknowledged when the process dies is lost by design.
- **Wiring** (`cli.py:build_agent`): this is the only place that picks concrete implementations. Every part is constructor-injected and defined by a `Protocol` (`Provider`, `Memory`, `Gateway`). To swap a part, change it here rather than adding config flags.
- **Tools** (`tools.py`, `builtins.py`): a `Tool` is a name, a JSON schema and a function that returns a string. `ToolRegistry.dispatch` converts unknown tools, bad arguments and exceptions into `"error: ..."` strings for the model to read, so a tool failure never crashes the loop. `builtin_tools(shell_argv=...)` lets the caller change how `run_shell` executes commands; the bench uses this to run them inside a Docker container. Limits (60s shell timeout, output truncation, 1024-line reads) are constants at the top of `builtins.py`.
- **Memory** (`memory.py`): sessions are **append-only JSONL** files (`<session>.jsonl`). Two kinds of entry exist: `message` and `compaction`. `load` resolves the latest compaction into a synthetic user message (`kind="compaction_summary"`) followed by the messages from `first_kept_entry_id` onward. Raw history is never deleted. Messages get ids on first persist, and `persist_messages` skips ids it has already written, so callers pass the whole list every time. Legacy `.json` sessions are migrated when loaded.
- **Compaction** (`compaction.py`): runs at the start of a turn and again after each tool round. It triggers when the estimated tokens exceed `context_window - reserve_tokens`, keeps about `keep_recent_tokens` of recent context and asks the provider for a structured summary, updating the previous summary in place when there is one. `Agent._compact` persists the checkpoint through `Memory.persist_compaction`.
- **Skills** (`skills.py`): progressive disclosure. Only `name: description` lines go into the system prompt, and the model calls the `read_skill` tool for the full body. Directories are rescanned on every access. The first definition of a name wins, and `config.py` appends the bundled `src/nod/skills/` last, so user skills override bundled ones. The frontmatter parser handles plain `key: value` lines only (no YAML dependency).
- **Gateways**: `CLIGateway` (`gateway.py`) and `HTTPGateway` (`server.py`). The CLI feeds user lines (from a reader thread) and inbox wake-ups into one queue and handles whichever comes first. The server is a `ThreadingHTTPServer` holding one shared `Agent`; the agent's own session lock serializes concurrent requests to one session. A wake-up on the server runs on a thread of its own and its reply is attached to the next `POST /chat` response for that session as `wakeups`. It refuses to start without `NOD_API_TOKEN`.
- **Config** (`config.py`): defaults, then an optional `config.toml` from the cwd or `-c`, with compaction keys under a `[compaction]` table. Relative paths resolve against the config file's directory. Secrets (`OPENROUTER_API_KEY`, `NOD_API_TOKEN`) come only from the environment.
- **Background jobs** live in the sibling `nod-extras` repo (`nod_extras/jobs.py`), wired in by `cli.py:build_agent` through a soft import: with it on the path (`uv run --with-editable ../nod-extras nod`) the job tools are registered, without it Nod runs on the built-ins. `Jobs` owns ids, deadlines, per-kind running limits, the reaper thread and the on-disk record under `jobs_dir`; the `shell` and `agent` kinds know how to start, poll and cancel their own. A subagent is built by `child_agent` in `cli.py` from the parent's parts with `SUBAGENT_SOUL` and the plain tools, never `spawn_agent`. The design note is `docs/background-jobs.md`; run the extras tests from here with `PYTHONPATH=../nod-extras/src uv run --extra dev pytest --rootdir ../nod-extras ../nod-extras/tests` until this branch is on `main`.

## Security model

The host is the sandbox: `run_shell`, `cud_file` and `http_get` have the full reach of the process user by design, and Nod is meant to run on a disposable VPS. Isolation is operational, not enforced in code, so don't add in-code sandboxing unless asked. Narrowing what the model can do means trimming the tool list in `cli.py`.

## SWE-bench (`bench/swebench/`)

`run_nod.py` imports nod as a library and builds its own agent (`build_bench_agent`) with a SWE-focused `SWE_SOUL` instead of `SOUL.md`, `max_turns` 100 and `run_shell` routed into the instance's eval container. File tools edit a host copy of `/testbed` that is bind-mounted into the container. The patch is `git diff` against a snapshot of the base tree. Grading is a separate step that uses the official `swebench` harness; don't reimplement scoring. `tests/test_swebench_runner.py` loads the script by path through `importlib`, so `bench/` is not a package.

Real runs happen on the ephemeral `nodgrader` Hetzner box, driven from the sibling `../myinfra` repo (`scripts/nodgrader_bench.sh`; see `.cursor/skills/run-nodgrader/SKILL.md`). It costs money while it's up, so always tear it down afterwards. When reading results, a high `empty_patch` count means Nod hit its turn or time limit, not that its fixes were wrong. Observed failure modes and the lessons drawn from them are logged in `docs/NOD_FAILURES.md`, and `docs/swebench-generation-environment.md` explains why running tests in the eval image is fair.
