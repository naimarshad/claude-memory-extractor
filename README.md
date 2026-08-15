# claude-memory-extractor

A `SessionEnd` hook for Claude Code that distills each finished session's transcript into an Obsidian vault, so decisions, mistakes, open plans, and dos/don'ts survive across sessions and machines instead of living only in that one transcript.

One install can feed several vaults, chosen per session from the directory it ran in, which is how a personal vault and a work vault stay separate on a machine that has both.

## How it works

- Registered on the `SessionEnd` hook event, which fires once, when a session actually ends. `Stop` fires after every turn and is the wrong event for this.
- Reads the session transcript, extracts the user/assistant text (skipping tool calls, thinking blocks, and subagent sidechains), and hands it to a headless `claude -p ... --output-format json --model haiku --allowedTools ""` call for structured extraction.
- That headless call rides on the same OAuth/subscription auth as an interactive `claude` session, so no `ANTHROPIC_API_KEY` or separate API billing is required, and no tool access is granted to the extraction call.
- It also runs under its own `--system-prompt`, replacing the default one. Under the default prompt the extraction call inherits `CLAUDE.md` and the auto-memory instructions, starts acting like an assistant working on the project, and keeps taking turns after it has emitted the JSON. Since `--output-format json` returns only the final turn, that trailing commentary is all that comes back and the extraction is lost.
- Writes one note per extracted item into `Context/`, `Plans/`, `Decisions/`, `Mistakes/`, `DosDonts/`, or `Incidents/`, plus a per-session summary in `Sessions/`, then regenerates `Index.md`. `Mistakes/` is a reusable rule ("don't do X again"); `Incidents/` is a dated postmortem of something that actually broke in a running system (outage, bad deploy, data loss).
- The vault is resolved per session by `resolve_vault()` from `~/.claude/memory-extractor.json` (see "Configuration" below). Any machine whose vault directory points at the same underlying storage (synced or mounted) converges on the same notes, keyed by the `project:` frontmatter field rather than by machine.
- `project:` is the **git repo root** name, not the leaf directory name, so a session run from a subdirectory is filed under the same project as one run from the root. Non-repo directories fall back to the directory name.
- Before calling the model, `collect_project_memory()` scans the vault for existing note titles and tags already recorded for the same project and feeds them back into the prompt. That stops the same recurring fact from being re-extracted as a fresh note every time it comes up, and steers tag choices toward reusing what's already there instead of inventing spelling variants. Tags are also normalized in code (lowercased, hyphenated) regardless of what the model outputs.
- Failures (bad JSON, a failed `claude -p` call, a missing transcript) are logged to `.extractor.log` in the vault and the hook exits quietly rather than breaking the session. Check that file first if notes stop appearing.
- When an extraction fails, its input payload is parked in `.extractor-failed/<session_id>.json` inside the vault instead of being deleted, and the log line prints the exact command to retry it: `memory_extractor.py --extract <path>`. The payload holds the already parsed transcript, so a retry does not depend on the original session's `.jsonl` still being around. A successful retry removes the file.

## Install

```bash
./install.sh
```

Idempotent and safe to re-run. It will:
1. Symlink `~/.claude/hooks/memory_extractor.py` to this directory's copy (so a `git pull` here updates the live hook with no reinstall step).
2. Merge the `SessionEnd` hook registration into `~/.claude/settings.json` via `jq`, without touching any other keys already there.
3. Symlink `~/.claude/skills/memory-recall` to this directory's `skills/memory-recall`, so `/memory-recall <query>` (or Claude auto-invoking it) is available in every session.
4. Copy `config.example.json` to `~/.claude/memory-extractor.json`, but only if that file does not already exist, so a re-run never clobbers a machine's routing.
5. Rewrite the "check the vault" block in `~/.claude/CLAUDE.md` between its marker comments, so snippet changes reach machines that installed an older copy.

Requires `jq`, `python3`, and `claude` on `PATH`.

## Configuration

`~/.claude/memory-extractor.json` is per machine and is never overwritten by an update:

```json
{
  "default_vault": "~/Obsidian/Claude/claude-memory",
  "routes": [
    { "prefix": "~/path/to/work/repos", "vault": "~/Obsidian/Work/claude-memory" }
  ],
  "model": "haiku"
}
```

- `default_vault` catches everything no route matches. On a machine where *all* work is one kind (a work laptop, say), point `default_vault` at that vault and leave `routes` empty, so nothing can leak into the wrong vault from a directory you forgot to route.
- `routes` match on directory prefix and apply to subdirectories too. Longest match wins, so a narrow route can carve an exception out of a broader one regardless of order.
- `CLAUDE_MEMORY_VAULT` overrides both for a single session.
- Missing or malformed config is not fatal: the built-in defaults still give a working single-vault install.

Two helper modes, both useful when setting a machine up:

```bash
python3 ~/.claude/hooks/memory_extractor.py --resolve-vault "$PWD"   # which vault would this session use
python3 ~/.claude/hooks/memory_extractor.py --reindex <vault-path>   # rebuild Index.md after moving notes by hand
```

## Recall

`/memory-recall <what to look for>` searches the vault scoped to the current project (falling back to the whole vault if nothing turns up), reads the matching notes in full, and answers from them. See `skills/memory-recall/SKILL.md` for the exact procedure. Claude also auto-invokes it when it senses a question needs prior-session history, without the slash command. This is grep-based, not semantic search. That is good enough at the vault's current size, but it won't find conceptually-related notes that don't share a keyword.

## Running it on more than one machine

Install per machine. The routing config is local to each machine, so where a session's notes land is a local decision rather than something this repo dictates. Point every machine's vault directory at the same underlying storage (a synced folder, a network mount) and the notes converge, keyed by the `project:` frontmatter field rather than by machine.

Two configurations worth knowing:

- **One machine, several vaults.** Set `default_vault` to the everyday vault and add a `routes` entry for the directory tree that belongs somewhere else, such as a work checkout on a personal machine.
- **A machine that is only one kind of work.** Point `default_vault` at that machine's vault and leave `routes` empty. A session started outside the directory you expected then still cannot file notes into the wrong vault, which is the failure a routes-only setup allows.

The extraction call uses whichever `claude` is authenticated on that machine, so machines on separate accounts or subscriptions need nothing shared between them beyond the vault itself.

In a container or anywhere `$HOME` is rebuilt, run `install.sh` from the start-up script. It is idempotent, and it will not overwrite an existing config.
