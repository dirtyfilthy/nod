# Nod

An agent of his own. East of Eden, off the map.

A deliberately thin agent core: rent the brain (any OpenAI-compatible LLM endpoint), build the scaffold yourself. Every moving part hides behind a small interface so you can swap it in one line. The "millions of skills" live in a separate `nod-extras` directory, not here.

## What's in the core

| Part | File | Default | Swap by |
| --- | --- | --- | --- |
| The brain | `provider.py` | OpenRouter via `httpx` | passing any `Provider` |
| Conversation | `messages.py` | dataclasses | n/a |
| Tools | `tools.py` | `ToolRegistry` | registering `Tool`s |
| Skills | `skills.py` | markdown + `read_skill` | adding `SKILL.md` files |
| Memory | `memory.py` | JSON files | passing any `Memory` |
| I/O | `gateway.py` / `server.py` | CLI or HTTP | passing any `Gateway` |

The whole agent is a thin synchronous loop in `agent.py`: ask the brain, run any tools it asked for, repeat until it answers.

## Quickstart

```sh
pip install -e .
export OPENROUTER_API_KEY=sk-or-...
python -m nod
```

That drops you into a CLI chat. `cp config.example.toml config.toml` first if you want to change the model or paths.

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

The service refuses to start without `NOD_API_TOKEN`, since the built-in tools give it the full reach of the process user. It binds loopback by default; put it behind a reverse proxy or SSH tunnel to reach it remotely, and change the address with `host`/`port` in `config.toml` or `--host`/`--port`. It shuts down cleanly on `SIGTERM`, so `systemctl stop`/`restart` is graceful rather than a hard kill. Each request prints one concise access-log line to stdout (client, method, path, status, session, duration) for `journalctl -u nod` -- never the token or message body.

It's a new `HTTPGateway` (see `server.py`) alongside the `CLIGateway`; the agent core is untouched, and the whole thing is ~150 lines of stdlib. A ready-to-edit systemd unit and env-file template live in `deploy/`.

## Built-in tools

The core ships five small tools: `read_file`, `cud_file`, `list_dir`, `run_shell`, and `http_get`. `read_file` returns paginated line ranges with offsets, and `cud_file` creates, updates, and deletes files with SEARCH/REPLACE blocks. They are deliberately plain and registered in `cli.py` so you can remove or replace any of them in one place.

Anything richer belongs in `nod-extras` later.

## Security model

Nod assumes it runs on a dedicated VPS, not your home box. The host is the sandbox. `run_shell`, `cud_file`, and `http_get` have the full reach of the process user by design, so the blast radius is whatever you put on that VPS. Isolation is operational, not enforced in code: run Nod on a disposable machine, as a non-root user, with no secrets on the box beyond what it needs. To narrow what the model can do, trim the tool list in `cli.py`.

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
