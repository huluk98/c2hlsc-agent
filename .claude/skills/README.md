# Project skills

Skills in this directory load automatically for anyone running Claude Code from
the repository root. Invoke a skill by name with a slash command (for example
`/grill-me`), or let the model trigger one whose `description` matches the task.

> **Note on `.agents/skills/`** — Claude Code discovers *project* skills under
> `.claude/skills/`, not `.agents/skills/`. The `coordinate-team-work` skill
> currently lives under `.agents/skills/` and therefore does not load in Claude
> Code sessions. Move or symlink it here if you want it available.

## Vendored skills

### `grill-me` / `grilling`

A relentless, round-based interview that stress-tests a plan or design. It maps
the work as a design tree, asks every currently-answerable question in one
numbered round (each with a recommended answer), waits for your answers, then
recomputes the frontier and asks the next round. It finishes only when no branch
is left silently assumed.

`grill-me` is a thin user-invoked wrapper (`disable-model-invocation: true`) that
delegates to `grilling`, which holds the method and can also trigger on its own.
Both files are copied verbatim from the upstream project.

- Upstream: <https://github.com/mattpocock/skills> (`skills/productivity/`)
- Copyright (c) 2026 Matt Pocock, MIT licensed. See `grilling/LICENSE`.

Useful here for pressure-testing a verification claim, an evaluation design, or
a paper framing before committing effort to it.

## Updating a vendored skill

```bash
base=https://raw.githubusercontent.com/mattpocock/skills/main/skills/productivity
for s in grill-me grilling; do
  curl -fsSL "$base/$s/SKILL.md" -o ".claude/skills/$s/SKILL.md"
done
```

## Installing these for yourself, outside this repo

Copy the same directories into `~/.claude/skills/` to make them available in
every project on your machine:

```bash
mkdir -p ~/.claude/skills
cp -r .claude/skills/grill-me .claude/skills/grilling ~/.claude/skills/
```
