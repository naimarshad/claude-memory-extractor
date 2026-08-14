---
name: memory-recall
description: Search the claude-memory Obsidian vault (~/Obsidian/Claude/claude-memory) for past decisions, mistakes, incidents, plans, or context on the current project. Use whenever the user asks what was discussed, decided, or fixed before, or references a prior session on this project.
argument-hint: <what to look for>
---

Query: $ARGUMENTS

The vault at `~/Obsidian/Claude/claude-memory/` holds notes distilled from past Claude Code sessions, one Markdown file per item, in `Context/`, `Plans/`, `Decisions/`, `Mistakes/`, `DosDonts/`, `Incidents/`, and `Sessions/`. Every note's frontmatter has a `project:` field set to the directory name the session ran in.

To answer the query above:

1. Determine the current project name: `basename "$PWD"`.
2. Search for candidate notes, scoped to that project first, e.g.:
   `grep -rl "project: <name>" ~/Obsidian/Claude/claude-memory/{Context,Plans,Decisions,Mistakes,DosDonts,Incidents,Sessions} 2>/dev/null`
   then narrow with a case-insensitive keyword grep over those files for terms from the query. If nothing turns up under the current project, broaden to the whole vault — some context may have been logged under a related project directory.
3. Read the full content of the notes that actually match (`cat` or the Read tool), not just filenames — frontmatter and headers alone are rarely enough context.
4. If grep is inconclusive, fall back to skimming `~/Obsidian/Claude/claude-memory/Index.md` for likely titles.
5. Answer using only what the notes actually say. If nothing relevant is found, say so plainly rather than guessing — do not invent history that isn't in the vault.
