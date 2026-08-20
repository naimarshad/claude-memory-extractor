// Command session-start is a Claude Code SessionStart hook. It injects the
// DosDonts notes for the current project into the session's context, so the
// standing rules for a project are present from the first turn rather than
// depending on the assistant remembering to go and read them.
//
// Vault routing is deliberately NOT reimplemented here. It shells out to
// `memory_extractor.py --resolve-vault`, so the rules for which directory feeds
// which vault keep exactly one implementation. Duplicating them in a second
// language is how the personal and work vaults would eventually disagree, which
// is the one failure this whole setup exists to prevent.
//
// Failure is never fatal. A SessionStart hook that errors out or emits garbage
// can disrupt starting a session, so every error path logs and exits 0 with no
// output. The log is the vault's own .extractor.log, so there is one place to
// look rather than two.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// maxBytes caps how much gets injected. The DosDonts set grows by a note or two
// per session, so an uncapped hook would quietly inflate every session's context
// until someone noticed the bill. Newest notes win; older ones drop off the end
// and the injected text says so rather than truncating in silence.
const maxBytes = 32000

type hookInput struct {
	CWD    string `json:"cwd"`
	Source string `json:"source"`
}

type hookSpecificOutput struct {
	HookEventName     string `json:"hookEventName"`
	AdditionalContext string `json:"additionalContext"`
}

type hookOutput struct {
	HookSpecificOutput hookSpecificOutput `json:"hookSpecificOutput"`
}

type note struct {
	title string
	body  string
	mtime time.Time
	size  int
}

// vaultLog appends to the vault's shared log. Best-effort: if this fails there
// is nothing useful left to do, and it must not turn into a second failure.
func vaultLog(vault, msg string) {
	if vault == "" {
		fmt.Fprintln(os.Stderr, "session-start:", msg)
		return
	}
	f, err := os.OpenFile(filepath.Join(vault, ".extractor.log"),
		os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		fmt.Fprintln(os.Stderr, "session-start:", msg)
		return
	}
	defer f.Close()
	fmt.Fprintf(f, "[%s] session-start: %s\n", time.Now().Format("2006-01-02T15:04:05"), msg)
}

func home() string {
	h, err := os.UserHomeDir()
	if err != nil {
		return os.Getenv("HOME")
	}
	return h
}

// resolveVault asks the extractor which vault this directory feeds. See the
// package comment for why this is a subprocess and not a reimplementation.
func resolveVault(cwd string) (string, error) {
	script := filepath.Join(home(), ".claude", "hooks", "memory_extractor.py")
	out, err := exec.Command("python3", script, "--resolve-vault", cwd).Output()
	if err != nil {
		return "", fmt.Errorf("resolve-vault failed: %w", err)
	}
	v := strings.TrimSpace(string(out))
	if v == "" {
		return "", fmt.Errorf("resolve-vault returned nothing")
	}
	return v, nil
}

// projectName mirrors the extractor's own rule: the git repo root's name, not
// the leaf directory, so a session in a subdirectory belongs to the same project
// as one at the root. Kept in sync with resolve_project_name() in the Python.
func projectName(cwd string) string {
	out, err := exec.Command("git", "-C", cwd, "rev-parse", "--show-toplevel").Output()
	if err == nil {
		if root := strings.TrimSpace(string(out)); root != "" {
			return filepath.Base(root)
		}
	}
	if base := filepath.Base(cwd); base != "" && base != "." && base != string(filepath.Separator) {
		return base
	}
	return "unknown"
}

// splitFrontmatter returns the frontmatter block and the remaining body. A file
// without a leading `---` fence has no frontmatter and is returned unchanged.
func splitFrontmatter(text string) (frontmatter, body string) {
	if !strings.HasPrefix(text, "---\n") {
		return "", text
	}
	rest := text[len("---\n"):]
	end := strings.Index(rest, "\n---")
	if end < 0 {
		return "", text
	}
	frontmatter = rest[:end]
	body = rest[end+len("\n---"):]
	return frontmatter, strings.TrimLeft(body, "\n")
}

// frontmatterProject pulls the `project:` value, which is what scopes the
// injection to the repository actually being worked on.
func frontmatterProject(frontmatter string) string {
	for _, line := range strings.Split(frontmatter, "\n") {
		if v, ok := strings.CutPrefix(strings.TrimSpace(line), "project:"); ok {
			return strings.TrimSpace(v)
		}
	}
	return ""
}

// stripTagFooter drops the trailing `#tag #tag` line the extractor writes. It is
// navigation aid for Obsidian and pure noise in a prompt.
func stripTagFooter(body string) string {
	lines := strings.Split(strings.TrimRight(body, "\n"), "\n")
	for len(lines) > 0 {
		last := strings.TrimSpace(lines[len(lines)-1])
		if last == "" || (strings.HasPrefix(last, "#") && !strings.HasPrefix(last, "# ") && !strings.HasPrefix(last, "##")) {
			lines = lines[:len(lines)-1]
			continue
		}
		break
	}
	return strings.Join(lines, "\n")
}

func collect(vault, project string) ([]note, error) {
	dir := filepath.Join(vault, "DosDonts")
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	var notes []note
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".md") {
			continue
		}
		path := filepath.Join(dir, e.Name())
		raw, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		frontmatter, body := splitFrontmatter(string(raw))
		if frontmatterProject(frontmatter) != project {
			continue
		}
		body = stripTagFooter(body)
		if strings.TrimSpace(body) == "" {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		notes = append(notes, note{
			title: strings.TrimSuffix(e.Name(), ".md"),
			body:  body,
			mtime: info.ModTime(),
			size:  len(body),
		})
	}
	// Newest first, so the cap drops the oldest rules rather than an arbitrary set.
	sort.Slice(notes, func(i, j int) bool { return notes[i].mtime.After(notes[j].mtime) })
	return notes, nil
}

func render(project string, notes []note) (string, int) {
	var b strings.Builder
	fmt.Fprintf(&b, "Standing rules recorded for project %q in the memory vault. "+
		"These are your own past conclusions, so treat them as binding unless this "+
		"repository's CLAUDE.md or AGENTS.md says otherwise.\n\n", project)
	used, dropped := 0, 0
	for _, n := range notes {
		if used+n.size > maxBytes {
			dropped++
			continue
		}
		b.WriteString(n.body)
		b.WriteString("\n\n")
		used += n.size
	}
	if dropped > 0 {
		fmt.Fprintf(&b, "(%d older rule note(s) omitted to stay within the context budget. "+
			"Read them from the vault's DosDonts/ directory if a decision depends on them.)\n", dropped)
	}
	return b.String(), dropped
}

func main() {
	var in hookInput
	if err := json.NewDecoder(os.Stdin).Decode(&in); err != nil {
		vaultLog("", fmt.Sprintf("unreadable payload (%v); nothing injected", err))
		return
	}

	// Deliberately no fallback to os.Getwd(). The payload's cwd is what decides
	// which vault this session reads, and guessing it wrong injects one vault's
	// rules into the other's session, which is the single thing the personal and
	// work split exists to prevent. A malfunction should inject nothing and say
	// so, not quietly pick the default vault.
	cwd := in.CWD
	if cwd == "" {
		vaultLog("", "payload carried no cwd; nothing injected")
		return
	}

	vault, err := resolveVault(cwd)
	if err != nil {
		vaultLog("", fmt.Sprintf("%v; nothing injected", err))
		return
	}

	project := projectName(cwd)
	notes, err := collect(vault, project)
	if err != nil {
		// A vault with no DosDonts yet is the normal state for a new project,
		// not a problem worth logging on every single session start.
		if !os.IsNotExist(err) {
			vaultLog(vault, fmt.Sprintf("could not read DosDonts: %v", err))
		}
		return
	}
	if len(notes) == 0 {
		return
	}

	context, dropped := render(project, notes)
	out, err := json.Marshal(hookOutput{HookSpecificOutput: hookSpecificOutput{
		HookEventName:     "SessionStart",
		AdditionalContext: context,
	}})
	if err != nil {
		vaultLog(vault, fmt.Sprintf("could not marshal output: %v", err))
		return
	}
	os.Stdout.Write(out)
	vaultLog(vault, fmt.Sprintf("injected %d DosDonts note(s) for project %s (%d bytes, %d omitted)",
		len(notes)-dropped, project, len(context), dropped))
}
