#!/usr/bin/env bash
# Idempotently wires up the SessionEnd memory-extraction hook for the current
# user's Claude Code config. Safe to re-run on every container start or shell
# login: symlinks the hook, merges the SessionEnd registration into
# settings.json without touching other keys, refreshes the vault-check pointer
# in the global CLAUDE.md, and seeds a routing config only if none exists yet.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${HOME}/.claude"
HOOKS_DIR="${CLAUDE_DIR}/hooks"
SKILLS_DIR="${CLAUDE_DIR}/skills"
SETTINGS_FILE="${CLAUDE_DIR}/settings.json"
CONFIG_FILE="${CLAUDE_DIR}/memory-extractor.json"
GLOBAL_MD="${CLAUDE_DIR}/CLAUDE.md"
BEGIN_MARKER="<!-- claude-memory-extractor: vault-check -->"
END_MARKER="<!-- /claude-memory-extractor: vault-check -->"

command -v jq >/dev/null || { echo "install.sh: jq not found in PATH" >&2; exit 1; }
command -v python3 >/dev/null || { echo "install.sh: python3 not found in PATH" >&2; exit 1; }

mkdir -p "$HOOKS_DIR"
ln -sf "${SCRIPT_DIR}/memory_extractor.py" "${HOOKS_DIR}/memory_extractor.py"

mkdir -p "$SKILLS_DIR"
ln -sfn "${SCRIPT_DIR}/skills/memory-recall" "${SKILLS_DIR}/memory-recall"

# Copied, never symlinked: vault routing is per-machine, so a `git pull` here
# must not overwrite what this machine has been configured to do.
if [ ! -f "$CONFIG_FILE" ]; then
  cp "${SCRIPT_DIR}/config.example.json" "$CONFIG_FILE"
  echo "install.sh: wrote starter config to ${CONFIG_FILE}; edit it to point at your vault(s)" >&2
fi

[ -f "$SETTINGS_FILE" ] || echo '{}' > "$SETTINGS_FILE"
if ! jq -e '.hooks.SessionEnd' "$SETTINGS_FILE" >/dev/null 2>&1; then
  tmp="$(mktemp)"
  jq --slurpfile frag "${SCRIPT_DIR}/hooks.session-end.json" \
     '.hooks.SessionEnd = $frag[0].SessionEnd' \
     "$SETTINGS_FILE" > "$tmp" && mv "$tmp" "$SETTINGS_FILE"
fi

touch "$GLOBAL_MD"
# Rewritten on every run rather than appended once, so edits to the snippet
# actually reach machines that already installed an older copy. Blocks written
# before the end marker existed ran to EOF, so those get absorbed too.
tmp="$(mktemp)"
awk -v b="$BEGIN_MARKER" -v e="$END_MARKER" '
  index($0, b) == 1 { skip = 1; next }
  skip && index($0, e) == 1 { skip = 0; next }
  !skip { lines[n++] = $0 }
  END { while (n > 0 && lines[n-1] == "") n--; for (i = 0; i < n; i++) print lines[i] }
' "$GLOBAL_MD" > "$tmp"
{
  [ -s "$tmp" ] && echo ""
  echo "$BEGIN_MARKER"
  cat "${SCRIPT_DIR}/global-claude-md-snippet.md"
  echo "$END_MARKER"
} >> "$tmp"
mv "$tmp" "$GLOBAL_MD"
