#!/usr/bin/env bash
# Idempotently wires up the SessionEnd memory-extraction hook for the current
# user's Claude Code config. Safe to re-run on every container start or shell
# login: symlinks the hook, merges the SessionEnd registration into
# settings.json without touching other keys, and appends the vault-check
# pointer to the global CLAUDE.md exactly once.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${HOME}/.claude"
HOOKS_DIR="${CLAUDE_DIR}/hooks"
SKILLS_DIR="${CLAUDE_DIR}/skills"
SETTINGS_FILE="${CLAUDE_DIR}/settings.json"
GLOBAL_MD="${CLAUDE_DIR}/CLAUDE.md"
MARKER="<!-- claude-memory-extractor: vault-check -->"

command -v jq >/dev/null || { echo "install.sh: jq not found in PATH" >&2; exit 1; }
command -v python3 >/dev/null || { echo "install.sh: python3 not found in PATH" >&2; exit 1; }

mkdir -p "$HOOKS_DIR"
ln -sf "${SCRIPT_DIR}/memory_extractor.py" "${HOOKS_DIR}/memory_extractor.py"

mkdir -p "$SKILLS_DIR"
ln -sfn "${SCRIPT_DIR}/skills/memory-recall" "${SKILLS_DIR}/memory-recall"

[ -f "$SETTINGS_FILE" ] || echo '{}' > "$SETTINGS_FILE"
if ! jq -e '.hooks.SessionEnd' "$SETTINGS_FILE" >/dev/null 2>&1; then
  tmp="$(mktemp)"
  jq --slurpfile frag "${SCRIPT_DIR}/hooks.session-end.json" \
     '.hooks.SessionEnd = $frag[0].SessionEnd' \
     "$SETTINGS_FILE" > "$tmp" && mv "$tmp" "$SETTINGS_FILE"
fi

touch "$GLOBAL_MD"
if ! grep -qF "$MARKER" "$GLOBAL_MD"; then
  {
    echo ""
    echo "$MARKER"
    cat "${SCRIPT_DIR}/global-claude-md-snippet.md"
  } >> "$GLOBAL_MD"
fi
