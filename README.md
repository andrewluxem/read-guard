# read-guard

A Claude Code `PreToolUse` hook for the `Read` tool. When Claude tries to read a large text file whole, read-guard denies the call and hands back a map of the file: Markdown headings with line numbers, or the first lines of anything else. Claude then reads only the range it needs.

One file, `read_guard.py`. Python 3.9+, standard library only.

## Why

An unranged `Read` pulls the whole file into context, up to about 25k tokens per call. Claude Code itself only refuses a whole file over 256 KB. Everything between a normal handoff doc and that ceiling goes straight in. read-guard draws the line at 32 KB by default. Across one developer's 14,411 Markdown files (median 2.7 KB, p90 10.6 KB), that let 98% through untouched and caught the 264 files where a whole read costs roughly 8k to 25k tokens.

## What Claude sees

```
read-guard: blocked an unranged Read of /work/notes/handoff.md
Size 34.1 KB (34,880 bytes), over the 32 KB threshold. 893 lines.
Headings (line: heading):
  4: # Sample handoff
  6: ## Part 1
  14: ### Detail
  117: ## Part 2
  ...
Read the section you need: offset at its heading line, limit up to the next heading.
If the whole file is truly needed, read it in ranges and say why.
```

Headings are capped at 40, then `... and N more: grep for ^# and read that range`. Headings inside fenced code blocks and YAML front matter are skipped. Files without headings get their first 5 lines and a hint to grep or read a range.

## Install

1. Put the script somewhere stable and check it runs:

   ```sh
   git clone https://github.com/andrewluxem/read-guard
   mkdir -p ~/.claude/hooks
   cp read-guard/read_guard.py ~/.claude/hooks/
   python3 ~/.claude/hooks/read_guard.py --selftest
   ```

2. Add the hook by hand to `~/.claude/settings.json` (all projects) or `<project>/.claude/settings.json` (one project). If the file already has a `hooks.PreToolUse` list, add this entry to it rather than replacing it.

   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "Read",
           "hooks": [
             {
               "type": "command",
               "command": "python3 \"$HOME/.claude/hooks/read_guard.py\"",
               "timeout": 10
             }
           ]
         }
       ]
     }
   }
   ```

   To run a copy that lives inside the project instead, use `"command": "python3 \"$CLAUDE_PROJECT_DIR/read_guard.py\""`.

3. Claude Code watches settings files and normally applies hook edits to a running session. Run `/hooks` to confirm the entry is there. If it is not, restart the session.

read-guard never writes to any settings file. There is no installer.

## Behavior

The hook reads the PreToolUse payload from stdin and, in order:

1. Allows anything that is not `Read`.
2. Allows a ranged read, meaning `tool_input` has `limit`. `offset` alone still reads to the end of the file, and so does `allow_large: true`, so both count as unranged.
3. Allows a path that is missing, not a regular file, or unreadable.
4. Allows extensions outside the text list. PDFs and images are never guarded, even if you list them.
5. Allows a path that matches an `ignore` glob, or does not match any `watch` glob.
6. Allows a file at or under `threshold_kb`.
7. Otherwise prints a JSON `permissionDecision: "deny"` with the reason above and exits 0. Claude Code shows the reason to Claude.
8. On anything unexpected (bad JSON, a broken config, an exception) it exits 0 and prints nothing. A guard that cannot run must not stop work.

## Config

The first file found wins:

1. `$READ_GUARD_CONFIG`
2. `<project>/.claude/read-guard.json`
3. `~/.config/read-guard/config.json`
4. Built-in defaults

The project root is `$CLAUDE_PROJECT_DIR`, else the session's working directory. Keys left out of a config file keep their defaults.

```json
{
  "threshold_kb": 32,
  "watch": ["**/*"],
  "ignore": ["**/node_modules/**", "./CHANGELOG.md"],
  "extensions": [".md", ".markdown", ".txt", ".rst", ".org", ".log", ".csv", ".tsv", ".json", ".yaml", ".yml"]
}
```

| Key | Default | Meaning |
|---|---|---|
| `threshold_kb` | `32` | Files larger than this many KiB are guarded. |
| `watch` | `["**/*"]` | Only paths matching one of these globs are guarded. |
| `ignore` | `[]` | Paths matching any of these are never guarded. Wins over `watch`. |
| `extensions` | the list above | Text extensions to guard. Replaces the default list. |

Glob rules: `*` and `?` stay within one path segment, and `**` crosses segments. A pattern starting with `/` or `~/` is absolute. A pattern starting with `./` is relative to the project root. Any other pattern matches at any depth, so `docs/**` matches every `docs` folder. Matching is case-insensitive on macOS and Windows and is checked against both the path as given and its resolved real path.

Set `READ_GUARD=off` in the environment to disable the guard for a session.

## Limits

- Claude Code only. Cowork and chat have no hooks.
- It covers the band Claude Code lets through today: from 32 KB up to Claude Code's own 256 KB refusal, where each read can cost up to about 25k tokens. It also covers `allow_large` reads, which skip that refusal.
- It cannot stop a Bash `cat`, `head` or `sed` of the same file.
- Files added with `@` in a prompt are attached without a tool call, so they never pass through the hook.
- Any `limit` counts as ranged, including a very large one.

## Tests

```sh
python3 -m unittest discover -s tests -v
python3 read_guard.py --selftest
```

CI runs both on Ubuntu with Python 3.9 and 3.13, and on macOS with Python 3.13.

**Prior art:** [Donchitos/Claude-Code-Game-Studios](https://github.com/Donchitos/Claude-Code-Game-Studios) (MIT). read-guard is written from scratch and shares no code with it.

## License

MIT. See [LICENSE](LICENSE).
