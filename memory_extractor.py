#!/usr/bin/env python3
"""SessionEnd hook: distill a finished session's transcript into the Obsidian
memory vault via a headless `claude -p` call (uses existing subscription auth,
no ANTHROPIC_API_KEY needed).

SessionEnd hooks get roughly a 1.5s execution budget from Claude Code (raising
a hook's own `timeout` only lifts this to a 60s hard cap), but the `claude -p`
extraction call below routinely takes longer than that. So `main()` only does
the cheap part synchronously (parse the transcript, decide whether it's worth
extracting) and hands the LLM call + note-writing off to a fully detached
background process via `--extract`, so the hook itself returns almost
immediately instead of being cancelled before it can do anything.

Which vault a session lands in is resolved per session from the directory it ran
in, so one install can feed several vaults (e.g. a personal one and a work one)
without mixing them. See `resolve_vault()` for the precedence and
`config.example.json` for the config file this reads.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

CONFIG_PATH = Path.home() / ".claude" / "memory-extractor.json"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
FALLBACK_VAULT = Path.home() / "Obsidian" / "Claude" / "claude-memory"
DEFAULT_MODEL = "haiku"

# Rebound once per run from the resolved config, before any note is written.
# Module-level rather than threaded through every call because the write helpers
# below all target one vault for the lifetime of a single extraction.
VAULT_PATH = FALLBACK_VAULT


def log(msg: str) -> None:
    VAULT_PATH.mkdir(parents=True, exist_ok=True)
    with (VAULT_PATH / ".extractor.log").open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")


def load_config() -> dict:
    """Missing or malformed config is not fatal: the defaults below still give a
    working single-vault install, which is what a fresh clone should do."""
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        log(f"config at {CONFIG_PATH} unreadable ({e}); falling back to defaults")
        return {}
    return config if isinstance(config, dict) else {}


def resolve_vault(cwd: str, config: dict) -> Path:
    """Vault for a session that ran in `cwd`, most specific match first:
    the CLAUDE_MEMORY_VAULT env var, then the longest matching `routes` prefix,
    then `default_vault`. Longest-match so a route can carve an exception out of
    a broader route without ordering mattering."""
    override = os.environ.get("CLAUDE_MEMORY_VAULT")
    if override:
        return Path(override).expanduser()

    best_prefix, best_vault = None, None
    if cwd:
        cwd_path = Path(cwd).expanduser().resolve()
        for route in config.get("routes", []):
            if not isinstance(route, dict) or not route.get("prefix") or not route.get("vault"):
                continue
            prefix = Path(route["prefix"]).expanduser().resolve()
            if cwd_path != prefix and not cwd_path.is_relative_to(prefix):
                continue
            if best_prefix is None or len(prefix.parts) > len(best_prefix.parts):
                best_prefix, best_vault = prefix, Path(route["vault"]).expanduser()
    if best_vault:
        return best_vault

    return Path(config.get("default_vault", str(FALLBACK_VAULT))).expanduser()


def resolve_project_name(cwd: str) -> str:
    """Repo root name, not the leaf directory name: a session that ran in a
    subdirectory belongs to the same project as one that ran at the root, and
    without this every `cd` into a subdirectory invents a new project."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).name
    except (subprocess.SubprocessError, OSError):
        pass
    return Path(cwd).name or "unknown"


def find_session_transcript(session_id: str) -> Path | None:
    for project_folder in CLAUDE_PROJECTS_DIR.iterdir():
        if not project_folder.is_dir():
            continue
        for jsonl_file in project_folder.glob("*.jsonl"):
            if session_id in jsonl_file.stem:
                return jsonl_file
    return None


def parse_transcript(raw: str) -> str:
    lines = []
    for line in raw.strip().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") not in ("user", "assistant") or entry.get("isSidechain"):
            continue
        message = entry.get("message") or {}
        role = message.get("role", "")
        content = message.get("content", "")
        if isinstance(content, list):
            text_parts = [b.get("text", "") for b in content if b.get("type") == "text"]
            content = "\n".join(t for t in text_parts if t)
        if role and content:
            lines.append(f"{role.upper()}: {content[:2000]}")
    return "\n\n".join(lines)


def cap_transcript(text: str, limit: int = 50000) -> str:
    if len(text) <= limit:
        return text
    head, tail = text[: limit // 2], text[-limit // 2 :]
    return f"{head}\n\n...[truncated]...\n\n{tail}"


def safe_title(title: str) -> str:
    return re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "-")[:80] or "untitled"


def normalize_tag(tag: str) -> str:
    """lowercase-hyphenated, so 'k0s-prod', 'k0s_prod', 'K0S Prod' all collapse
    to the same tag instead of fragmenting grep-based recall across variants."""
    tag = re.sub(r"[\s_]+", "-", tag.strip().lower())
    tag = re.sub(r"[^a-z0-9-]", "", tag)
    return tag.strip("-")


def write_note(folder: str, date_str: str, session_short: str, project: str, title: str, content: str, tags: list) -> None:
    folder_path = VAULT_PATH / folder
    folder_path.mkdir(parents=True, exist_ok=True)
    filepath = folder_path / f"{date_str}-{session_short}-{safe_title(title)}.md"
    tags = sorted({t for t in (normalize_tag(t) for t in tags) if t})
    tag_str = " ".join(f"#{t}" for t in tags) if tags else ""
    frontmatter = (
        f"---\ndate: {date_str}\nproject: {project}\nsession: {session_short}\ntags: [{', '.join(tags)}]\n---\n\n"
    )
    filepath.write_text(frontmatter + content + (f"\n\n{tag_str}" if tag_str else ""), encoding="utf-8")


def collect_project_memory(project_name: str, max_titles: int = 60, max_tags: int = 40) -> tuple[list, list]:
    """Existing note titles and tags already recorded for this project, so the
    extraction prompt can skip re-extracting known facts and reuse existing
    tags instead of inventing near-duplicate variants."""
    categories = ["Context", "Plans", "Decisions", "Mistakes", "DosDonts", "Incidents"]
    entries = []
    tag_counts: dict[str, int] = {}
    for category in categories:
        folder = VAULT_PATH / category
        if not folder.is_dir():
            continue
        for f in folder.glob("*.md"):
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            fm_match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
            if not fm_match:
                continue
            frontmatter = fm_match.group(1)
            proj_match = re.search(r"^project:\s*(.+)$", frontmatter, re.MULTILINE)
            if not proj_match or proj_match.group(1).strip() != project_name:
                continue
            title_match = re.search(r"^## (.+)$", text, re.MULTILINE)
            title = title_match.group(1).strip() if title_match else f.stem
            entries.append((f.stat().st_mtime, category, title))
            tags_match = re.search(r"^tags:\s*\[(.*?)\]", frontmatter, re.MULTILINE)
            if tags_match:
                for tag in tags_match.group(1).split(","):
                    tag = tag.strip()
                    if tag:
                        tag_counts[tag] = tag_counts.get(tag, 0) + 1
    entries.sort(key=lambda e: e[0], reverse=True)
    titles = [f"[{category}] {title}" for _, category, title in entries[:max_titles]]
    top_tags = sorted(tag_counts, key=lambda t: tag_counts[t], reverse=True)[:max_tags]
    return titles, top_tags


def update_index() -> None:
    lines = ["# Claude Memory Index", "", f"_Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_", ""]
    for folder in ["Context", "Plans", "Decisions", "Mistakes", "DosDonts", "Incidents", "Sessions"]:
        folder_path = VAULT_PATH / folder
        lines.append(f"## {folder}")
        lines.append("")
        if folder_path.is_dir():
            files = sorted(folder_path.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
            for f in files[:100]:
                title = f.stem
                lines.append(f"- [{title}]({folder}/{f.name})")
        lines.append("")
    VAULT_PATH.mkdir(parents=True, exist_ok=True)
    (VAULT_PATH / "Index.md").write_text("\n".join(lines), encoding="utf-8")


def extract_and_write(session_id: str, project_name: str, transcript_text: str, model: str) -> None:
    """Runs in the detached background process. Does the slow LLM call and
    writes the resulting notes into the vault."""
    existing_titles, existing_tags = collect_project_memory(project_name)
    existing_titles_block = (
        "\n".join(f"- {t}" for t in existing_titles) if existing_titles else "(none yet)"
    )
    existing_tags_block = ", ".join(existing_tags) if existing_tags else "(none yet)"

    prompt = f"""You are analyzing a Claude Code session transcript to extract durable, reusable knowledge for a cross-session memory vault. Project: {project_name}.

ALREADY IN THE VAULT FOR THIS PROJECT (most recent first, do not re-extract these. Only include an item if it is new information, or a material update/correction/reversal of one of these, in which case say so explicitly in the detail so it reads as an update, not a duplicate):
{existing_titles_block}

TAGS ALREADY USED FOR THIS PROJECT (reuse these instead of inventing a new spelling or variant of the same concept, e.g. don't add "k0s_prod" if "k0s-prod" is already here; still add a new tag if none of these fit):
{existing_tags_block}

TRANSCRIPT:
{cap_transcript(transcript_text)}

Extract structured insights in this exact JSON format (no markdown fences, valid JSON only):
{{
  "context": [{{"title": "short title", "detail": "project-specific fact worth remembering", "tags": ["tag1"]}}],
  "plans": [{{"title": "short title", "detail": "what is planned or still open", "tags": ["tag1"]}}],
  "decisions": [{{"title": "short title", "decision": "what was decided", "reasoning": "why", "tags": ["tag1"]}}],
  "mistakes": [{{"title": "short title", "error": "what went wrong", "fix": "how it was resolved or avoided", "tags": ["tag1"]}}],
  "dos_donts": [{{"title": "short title", "rule": "a do or don't rule", "why": "the reason behind it", "tags": ["tag1"]}}],
  "incidents": [{{"title": "short title", "what_happened": "the outage/breakage and its user-visible impact", "root_cause": "why it happened", "resolution": "how it was fixed and any timeline", "tags": ["tag1"]}}],
  "session_summary": "2-3 sentence summary of what this session accomplished"
}}

"mistakes" is for a reusable rule ("don't do X again"). "incidents" is for a dated postmortem of something that actually broke in a running system (an outage, a bad deploy, data loss). Only use it for real breakage, not routine bugs caught during development.

Only include items that are genuinely reusable or important for future sessions on this project. Omit a category entirely (empty list) if nothing qualifies. Do not invent information not present in the transcript."""

    try:
        result = subprocess.run(
            # disableAllHooks is required here: this call is itself a Claude Code
            # session, so without it, SessionEnd would fire again when it exits and
            # recursively re-invoke this same extraction hook on its own transcript.
            ["claude", "-p", prompt, "--output-format", "json", "--model", model,
             "--allowedTools", "", "--settings", '{"disableAllHooks": true}'],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        log(f"session {session_id}: subprocess failed: {e}")
        return

    if result.returncode != 0:
        log(f"session {session_id}: claude -p exited {result.returncode}: {result.stderr[:500]}")
        return

    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        log(f"session {session_id}: bad envelope JSON: {e}: {result.stdout[:500]}")
        return

    if envelope.get("is_error"):
        log(f"session {session_id}: claude -p reported is_error: {envelope}")
        return

    insights_raw = re.sub(r"```(?:json)?\n?", "", envelope.get("result", "")).strip()
    try:
        insights, _ = json.JSONDecoder().raw_decode(insights_raw)
    except json.JSONDecodeError as e:
        log(f"session {session_id}: bad insights JSON: {e}: {insights_raw[:500]}")
        return

    date_str = datetime.now().strftime("%Y-%m-%d")
    time_str = datetime.now().strftime("%H%M")
    session_short = session_id[:8]
    note_count = 0

    if insights.get("context"):
        for item in insights["context"]:
            write_note("Context", date_str, session_short, project_name, item["title"],
                       f"## {item['title']}\n\n{item['detail']}", item.get("tags", []) + ["context"])
            note_count += 1

    if insights.get("plans"):
        for item in insights["plans"]:
            write_note("Plans", date_str, session_short, project_name, item["title"],
                       f"## {item['title']}\n\n{item['detail']}", item.get("tags", []) + ["plan"])
            note_count += 1

    if insights.get("decisions"):
        for item in insights["decisions"]:
            write_note("Decisions", date_str, session_short, project_name, item["title"],
                       f"## {item['title']}\n\n**Decision:** {item['decision']}\n\n**Reasoning:** {item['reasoning']}",
                       item.get("tags", []) + ["decision"])
            note_count += 1

    if insights.get("mistakes"):
        for item in insights["mistakes"]:
            write_note("Mistakes", date_str, session_short, project_name, item["title"],
                       f"## {item['title']}\n\n**Error:** {item['error']}\n\n**Fix:** {item['fix']}",
                       item.get("tags", []) + ["mistake"])
            note_count += 1

    if insights.get("dos_donts"):
        for item in insights["dos_donts"]:
            write_note("DosDonts", date_str, session_short, project_name, item["title"],
                       f"## {item['title']}\n\n**Rule:** {item['rule']}\n\n**Why:** {item['why']}",
                       item.get("tags", []) + ["dos-donts"])
            note_count += 1

    if insights.get("incidents"):
        for item in insights["incidents"]:
            write_note("Incidents", date_str, session_short, project_name, item["title"],
                       f"## {item['title']}\n\n**What happened:** {item['what_happened']}\n\n"
                       f"**Root cause:** {item['root_cause']}\n\n**Resolution:** {item['resolution']}",
                       item.get("tags", []) + ["incident"])
            note_count += 1

    sessions_dir = VAULT_PATH / "Sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"{date_str}-{time_str}-{project_name}.md").write_text(
        f"---\ndate: {date_str}\nproject: {project_name}\nsession: {session_short}\n---\n\n"
        f"## Session Summary\n\n{insights.get('session_summary', 'No summary generated.')}\n",
        encoding="utf-8",
    )

    update_index()
    log(f"session {session_id}: wrote {note_count} note(s) + session summary")


def run_extract_mode(payload_path_arg: str) -> None:
    global VAULT_PATH
    payload_path = Path(payload_path_arg)
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        # Resolved by the parent hook process, not re-resolved here: the cwd is
        # gone by now, and both halves must agree on one vault.
        VAULT_PATH = Path(payload["vault"])
        extract_and_write(payload["session_id"], payload["project_name"],
                          payload["transcript_text"], payload.get("model", DEFAULT_MODEL))
    finally:
        payload_path.unlink(missing_ok=True)


def main() -> None:
    global VAULT_PATH

    if len(sys.argv) > 2 and sys.argv[1] == "--extract":
        run_extract_mode(sys.argv[2])
        return

    # Used by the memory-recall skill so vault routing has exactly one
    # implementation instead of being restated in the skill's grep paths.
    if len(sys.argv) > 1 and sys.argv[1] == "--resolve-vault":
        cwd = sys.argv[2] if len(sys.argv) > 2 else os.getcwd()
        print(resolve_vault(cwd, load_config()))
        return

    # Index.md is otherwise only rewritten as a side effect of an extraction, so
    # this is how you repair it after moving notes between vaults by hand.
    if len(sys.argv) > 1 and sys.argv[1] == "--reindex":
        VAULT_PATH = (Path(sys.argv[2]).expanduser() if len(sys.argv) > 2
                      else resolve_vault(os.getcwd(), load_config()))
        update_index()
        print(f"reindexed {VAULT_PATH}")
        return

    try:
        hook_input = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        return

    session_id = hook_input.get("session_id", "")
    project_dir = hook_input.get("cwd", "")
    if not session_id:
        return

    config = load_config()
    VAULT_PATH = resolve_vault(project_dir, config)

    transcript_path = hook_input.get("transcript_path")
    transcript_file = Path(transcript_path) if transcript_path else None
    if not transcript_file or not transcript_file.is_file():
        transcript_file = find_session_transcript(session_id)
    if not transcript_file:
        # Routine for subagent/sidechain sessions: their turns live inline in
        # the parent session's transcript, not in a standalone file. Not an
        # error, so not worth logging.
        return

    raw_transcript = transcript_file.read_text(encoding="utf-8")
    transcript_text = parse_transcript(raw_transcript)
    if len(transcript_text) < 200:
        return  # too little signal to be worth an extraction call

    payload = {
        "session_id": session_id,
        "project_name": resolve_project_name(project_dir),
        "transcript_text": transcript_text,
        "vault": str(VAULT_PATH),
        "model": config.get("model", DEFAULT_MODEL),
    }
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(payload, f)
            payload_path = f.name
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--extract", payload_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        log(f"session {session_id}: failed to spawn background extractor: {e}")


if __name__ == "__main__":
    main()
