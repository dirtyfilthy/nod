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
| I/O | `gateway.py` | CLI | passing any `Gateway` |

The whole agent is a thin synchronous loop in `agent.py`: ask the brain, run any tools it asked for, repeat until it answers.

## Quickstart

```sh
pip install -e .
export OPENROUTER_API_KEY=sk-or-...
python -m nod
```

That drops you into a CLI chat. `cp config.example.toml config.toml` first if you want to change the model or paths.

## Adding a skill

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
