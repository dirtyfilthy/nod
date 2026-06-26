---
name: create-skill
description: Create a new nod skill in the standard SKILL.md format. Use when the user asks to add, write, or scaffold a skill, or when a reusable procedure keeps coming up that nod should apply consistently across sessions. Only make a skill for work that repeats and has stable steps; do not create one for a one-off task, a single fix, or anything involving secrets.
---

# Create Skill

Use this skill when adding a reusable capability to nod.

## Format

A skill is a directory containing a `SKILL.md` file. The file starts with simple frontmatter:

```markdown
---
name: my-skill-name
description: Short sentence describing when to use this skill.
---

# My Skill

Instructions the agent should follow when this skill is loaded.
```

## Guidance

- Use a short, kebab-case `name`.
- Write the `description` as activation guidance: when should nod use this?
- Keep the body practical and procedural. Include steps, constraints, examples, and expected outputs.
- Prefer one focused skill over a broad catch-all skill.
- Do not put secrets in skills.

Nod shows the skill name and description in the system prompt. When a task matches, nod can call `read_skill` with the skill name to load the full body.
