# Nod

An agent of his own. East of Eden, off the map.

A deliberately thin agent core: rent the brain (any OpenAI-compatible LLM endpoint), build the scaffold yourself. Every moving part hides behind a small interface so you can swap it in one line. The "millions of skills" live in a separate [`nod-extras`](https://github.com/dirtyfilthy/nod-extras) repo, not here.

## What's in the core

| Part | File | Default | Swap by |
| --- | --- | --- | --- |
| The brain | `provider.py` | OpenRouter via `httpx` | passing any `Provider` |
| Conversation | `messages.py` | dataclasses | n/a |
| Tools | `tools.py` | `ToolRegistry` | registering `Tool`s |
| Skills | `skills.py` | markdown + `read_skill` | adding `SKILL.md` files |
| Memory | `memory.py` | append-only JSONL files | passing any `Memory` |
| Inbox | `inbox.py` | in-memory event list | passing any `Inbox` |
| I/O | `gateway.py` / `server.py` | CLI or HTTP | passing any `Gateway` |

The whole agent is a thin synchronous loop in `agent.py`: ask the brain, run any tools it asked for, repeat until it answers. Before each call to the brain it empties the session's inbox: anything posted there while it wasn't looking (a background job finishing) becomes a user message. A gateway can also `wake` an idle agent to run a turn on pending events alone.

## Quickstart

```sh
pip install -e .
export OPENROUTER_API_KEY=sk-or-...
python -m nod
```

That drops you into a CLI chat. `cp config.example.toml config.toml` first if you want to change the model or paths. To try another model without touching the config, pass `--model` (before any subcommand): `nod --model openai/gpt-5 run "..."`. `--max-turns N` does the same for the config's `max_turns`.

## One shot

To hand Nod one task and get the answer back without a chat, use `run`:

```sh
nod run "summarise the TODOs in src/ into TODO.md"
nod run -f task.md                       # instructions from a file
nod run -f task.md "skip the tests"      # text on the line goes after the file's
git diff | nod run -f - "review this"    # '-' reads stdin
```

Nod works through the task as it would in a chat turn and prints its final reply to stdout. Each run gets a fresh session, whose id is printed to stderr (`[session run-...]`); pick it up with `nod chat --session <id>`, or pass `--session` to `run` to continue an existing one.

For a script that drives `nod run`, the exit status and the end of stderr are the contract:

| Exit | Meaning |
| --- | --- |
| 0 | Nod replied. A turn cut short by `max_turns` also exits 0; its reply is `(stopped: hit the max_turns limit before finishing)`. |
| 1 | The turn failed, usually the provider. Stderr has an `[error] ...` line. |
| 2 | Nothing ran: no instructions, an unreadable file, or a config, API key or sandbox problem. |

Stderr ends with two lines, on a failure too: `[usage {"calls": 4, "prompt_tokens": 9120, "completion_tokens": 310, "cost": 0.0021}]`, then `[session <id>]`. The usage is what this process spent with the provider, compaction and subagent calls included; `cost` is the sum of what OpenRouter reported and stays 0 for an endpoint that reports none.

The sandbox flags go before `run` as usual (`nod --sandbox run ...`). With nod-extras loaded, jobs still running when the reply comes back are killed at exit, so a task that starts them should wait for them.

## Running as a service

`python -m nod` (or `nod chat`) is the interactive REPL. To run Nod as a long-lived HTTP service instead, use the `serve` subcommand:

```sh
export OPENROUTER_API_KEY=sk-or-...
export NOD_API_TOKEN=$(openssl rand -hex 32)
nod serve            # binds 127.0.0.1:8080 by default
```

The service is built on the standard library alone (`http.server`), so it adds no dependencies. It holds one `Agent` in memory and exposes a tiny JSON API:

- `POST /chat` with `{"message": "...", "session": "..."}` returns `{"session": "...", "reply": "..."}`. The `session` (default `api`) maps onto the `Memory` layer, so conversations stay isolated and survive restarts. Every request must carry `Authorization: Bearer $NOD_API_TOKEN`.
- `GET /healthz` is an unauthenticated liveness probe that makes no LLM call.

When something is posted to a session's inbox between requests (a background job ending), the server runs a turn on its own. That reply has nowhere to go, so it is attached to the next `POST /chat` response for the session as `"wakeups": [...]`; the field is absent when there is nothing to attach.

The service refuses to start without `NOD_API_TOKEN`, since the built-in tools give it the full reach of the process user. It binds loopback by default; put it behind a reverse proxy or SSH tunnel to reach it remotely, and change the address with `host`/`port` in `config.toml` or `--host`/`--port`. It shuts down cleanly on `SIGTERM`, so `systemctl stop`/`restart` is graceful rather than a hard kill. Each request prints one concise access-log line to stdout (client, method, path, status, session, duration) for `journalctl -u nod` -- never the token or message body.

It's a new `HTTPGateway` (see `server.py`) alongside the `CLIGateway`; the agent core is untouched, and the whole thing is ~150 lines of stdlib. A ready-to-edit systemd unit and env-file template live in `deploy/`.

## Running in Docker

To run all of Nod in a container (rather than only its tools; see "Sandboxing the tools" below for that), use the `Dockerfile` and its wrapper script:

```sh
deploy/docker/nod-docker.sh build     # adds ../nod-extras (job tools, skills) if it's there
NOD_ENV_FILE=/etc/nod/nod.env deploy/docker/nod-docker.sh serve
deploy/docker/nod-docker.sh chat      # or a CLI chat in a throwaway container
```

The env file is the same one the systemd unit uses (`OPENROUTER_API_KEY`, `NOD_API_TOKEN`). The image runs Nod as an unprivileged `nod` user, with Nod's code and venv owned by root so the agent can't edit its own runtime. Sessions, job logs and the agent's working directory live on the `nod-data` volume under `/var/lib/nod`. The image's config is `deploy/docker/container.toml`; point `NOD_CONFIG` at your own file to mount one over it. The script publishes the port on `127.0.0.1` only and runs every container with `--cap-drop ALL`, `no-new-privileges`, memory and process limits, and `--init` to reap the agent's orphaned processes. Add whatever the agent's shell should have to the `apt-get install` line.

The container protects the host from the agent, not anything inside the container from it. The API key is in the container's environment, the agent needs outbound network for the provider and `http_get`, and it can read and rewrite its own session history. Use a dedicated OpenRouter key with a spending limit, or sandbox only the tools instead.

## Sandboxing the tools

To keep Nod on the host but run every tool in a container, turn the sandbox on:

```sh
nod --sandbox                                   # tools see only the current dir, at /workspace
nod --workspace ~/src/proj --mount ~/datasets:ro --no-network
nod --mount ~/notes:/notes serve                # works the same for the service
```

Every built-in tool call (`read_file`, `cud_file`, `list_dir`, `run_shell`, `http_get`) then runs inside one Docker container that Nod starts at launch and removes when it exits. Subagents and background shell jobs from nod-extras use it too. The tools are the host's own code, mounted read-only and run through `python -m nod.toolhost`, so they behave exactly as they do on the host but see only what is mounted:

- the workspace (`--workspace`, default the current dir) at `/workspace`, which is also their working directory;
- each `--mount HOST[:CONTAINER][:ro]`, at the same path as on the host unless you give one;
- the job logs, read-only, when nod-extras is loaded.

`--no-network` gives the container no network at all, so `http_get` and anything the shell fetches fail; the model is still reached from the host. The container runs as your user (files it writes stay yours) with every capability dropped, `no-new-privileges`, and memory and process limits. The API key and the session history stay on the host, out of the tools' reach, and so do `SOUL.md` and the config unless they sit in the workspace (with the default workspace, that means not running from the directory that holds them).

The same settings live under `[sandbox]` in `config.toml` (see `config.example.toml`); flags add to them. The image (`src/nod/sandbox.Dockerfile`: Python, git, curl) is built the first time it's needed. To give the tools more, build your own `FROM` it and set `image`. If Nod is killed hard, the container is left behind; `docker rm -f $(docker ps -q --filter label=nod.sandbox)` clears it. Skills stay on the host: `read_skill` reads Nod's own skill files and runs no command.

## Built-in tools

The core ships five small tools: `read_file`, `cud_file`, `list_dir`, `run_shell`, and `http_get`. `read_file` returns paginated line ranges with offsets, and `cud_file` creates, updates, and deletes files with SEARCH/REPLACE blocks. They are deliberately plain and registered in `cli.py` so you can remove or replace any of them in one place.

Anything richer belongs in the sibling `nod-extras` repo. The first thing there is background jobs: with `nod-extras` on the path (`uv run --with-editable ../nod-extras nod`), `build_agent` adds `start_shell_job`, `spawn_agent`, `list_jobs`, `kill_job` and `wait_jobs`. A shell job runs a command in its own process group with its output in a log file; an agent job hands a task to a subagent in a child session. Every job has a deadline and can be cancelled, and when it ends a `<job-finished>` event is posted to the session's inbox, so Nod hears about it at its next turn, or is woken for one. Job logs and the job record live under `jobs_dir` (default `/var/tmp/nod-jobs`), and a restarted Nod reports the jobs a dead one left as `lost`. The design is in `docs/background-jobs.md`.

## Security model

Nod assumes it runs on a dedicated VPS, not your home box. The host is the sandbox. `run_shell`, `cud_file`, and `http_get` have the full reach of the process user by design, so the blast radius is whatever you put on that VPS. Isolation is operational, not enforced in code: run Nod on a disposable machine, as a non-root user, with no secrets on the box beyond what it needs. To narrow what the model can do, trim the tool list in `cli.py`, or run the tools in a container with `--sandbox` (above).

Skills are plain markdown with a tiny frontmatter, discovered from the directories in `skills_dirs`:

```markdown
---
name: deploy-checklist
description: Steps to run before shipping to production.
---

1. Run the tests.
2. Check the migration is reversible.
...
```

Nod sees only the `name` and `description` of every skill up front (cheap, scales to thousands). When a task matches, he calls the built-in `read_skill` tool to pull the full text on demand.

## Swapping a part

Everything is constructor-injected. To use a different brain, pass a different `Provider`:

```python
agent = Agent(provider=MyProvider(), registry=registry, memory=memory, skills=skills, soul=soul)
```

The same pattern applies to `Memory` and `Gateway`. See the `Protocol` definitions in each module.

## Related repos

- [nod-extras](https://github.com/dirtyfilthy/nod-extras): background jobs, subagents and skills, the parts that don't belong in the thin core.
- [nod-poll](https://github.com/dirtyfilthy/nod-poll): ask a panel of models the same question through Nod, each in a network-less sandbox, then tally the points they agree on.

## License

MIT. See [LICENSE](LICENSE).
