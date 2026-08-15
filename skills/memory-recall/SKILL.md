---
name: memory-recall
description: Search the claude-memory Obsidian vault for past decisions, mistakes, incidents, plans, or context on the current project. Use whenever the user asks what was discussed, decided, or fixed before, or references a prior session on this project.
argument-hint: <what to look for>
---

Query: $ARGUMENTS

The vault holds notes distilled from past Claude Code sessions, one Markdown file per item, in `Context/`, `Plans/`, `Decisions/`, `Mistakes/`, `DosDonts/`, `Incidents/`, and `Sessions/`. Every note's frontmatter has a `project:` field set to the repo the session ran in, and a `machine:` field naming the machine that ran it.

`project:` is what scopes a search. `machine:` is context, not a filter: several machines write to one vault, and a decision made on one applies to the others unless the note says otherwise. It matters mainly when a note mentions a path, since paths differ per machine, and when you want to explain why a session's transcript is not available locally.

Which vault applies depends on where the session is running: one machine can feed several vaults (for example a personal one and a work one). Never assume a path, always resolve it.

To answer the query above:

1. Resolve the vault and project for the current directory:
   `VAULT="$(python3 ~/.claude/hooks/memory_extractor.py --resolve-vault "$PWD")"`
   `PROJECT="$(basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)")"`
2. Search for candidate notes, scoped to that project first, e.g.:
   `grep -rl "project: $PROJECT" "$VAULT"/{Context,Plans,Decisions,Mistakes,DosDonts,Incidents,Sessions} 2>/dev/null`
   then narrow with a case-insensitive keyword grep over those files for terms from the query. If nothing turns up under the current project, broaden to the whole vault, but stay inside `$VAULT`: other vaults on this machine are deliberately separate and must not be searched.
3. Read the full content of the notes that actually match (`cat` or the Read tool), not just filenames, since frontmatter and headers alone are rarely enough context.
4. If grep is inconclusive, fall back to skimming `"$VAULT"/Index.md` for likely titles.
5. Answer using only what the notes actually say. If nothing relevant is found, say so plainly rather than guessing. Do not invent history that isn't in the vault.
