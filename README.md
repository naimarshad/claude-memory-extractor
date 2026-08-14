# claude-memory-extractor

A `SessionEnd` hook for Claude Code that distills each finished session's transcript into a shared Obsidian vault at `~/Obsidian/Claude/claude-memory/`, so decisions, mistakes, open plans, and dos/don'ts survive across sessions and machines instead of living only in that one transcript.

## How it works

- Registered on the `SessionEnd` hook event (fires once, when a session actually ends — not `Stop`, which fires after every turn).
- Reads the session transcript, extracts the user/assistant text (skipping tool calls, thinking blocks, and subagent sidechains), and hands it to a headless `claude -p ... --output-format json --model haiku --allowedTools ""` call for structured extraction.
- That headless call rides on the same OAuth/subscription auth as an interactive `claude` session — no `ANTHROPIC_API_KEY` or separate API billing required, and no tool access is granted to the extraction call.
- Writes one note per extracted item into `Context/`, `Plans/`, `Decisions/`, `Mistakes/`, `DosDonts/`, or `Incidents/`, plus a per-session summary in `Sessions/`, then regenerates `Index.md`. `Mistakes/` is a reusable rule ("don't do X again"); `Incidents/` is a dated postmortem of something that actually broke in a running system (outage, bad deploy, data loss).
- `VAULT_PATH` in `memory_extractor.py` resolves as `Path.home() / "Obsidian" / "Claude" / "claude-memory"` — any machine whose `~/Obsidian` points at the same underlying vault (synced or mounted) converges on the same notes, keyed by the `project:` frontmatter field rather than by machine.
- Failures (bad JSON, a failed `claude -p` call, a missing transcript) are logged to `.extractor.log` in the vault and the hook exits quietly rather than breaking the session — see that file first if notes stop appearing.

## Install

```bash
./install.sh
```

Idempotent — safe to re-run. It will:
1. Symlink `~/.claude/hooks/memory_extractor.py` to this directory's copy (so a `git pull` here updates the live hook with no reinstall step).
2. Merge the `SessionEnd` hook registration into `~/.claude/settings.json` via `jq`, without touching any other keys already there.
3. Symlink `~/.claude/skills/memory-recall` to this directory's `skills/memory-recall`, so `/memory-recall <query>` (or Claude auto-invoking it) is available in every session.
4. Append a short "check the vault" pointer to `~/.claude/CLAUDE.md`, guarded by a marker comment so it's only added once.

Requires `jq`, `python3`, and `claude` on `PATH`.

## Recall

`/memory-recall <what to look for>` searches the vault scoped to the current project (falling back to the whole vault if nothing turns up), reads the matching notes in full, and answers from them — see `skills/memory-recall/SKILL.md`. Claude also auto-invokes it when it senses a question needs prior-session history, without the slash command. This is grep-based, not semantic search — good enough at the vault's current size, but it won't find conceptually-related notes that don't share a keyword.

## Where this runs

- **This machine (naeem, host):** installed manually into `~/.claude/`.
- **`personal-claude-env`** (the always-on dev-container agent, see `compose-files/dev-container/`): `entrypoint.sh` runs this installer as the `node` user on every container start, since that container's `$HOME` is a fresh/persisted volume rather than a checkout of this repo's dotfiles. Its `~/Obsidian` is bind-mounted separately from this repo's — see that directory's `CLAUDE.md` for the exact mount and how it relates to this vault.
