# read-guard

A Claude Code `PreToolUse` hook for the `Read` tool. When Claude tries to read a large text file whole, or with a `limit` over the line cap, read-guard denies the call and hands back a map of the file: Markdown headings with line numbers, or the first lines of anything else. Claude then reads only the range it needs.

One file, `read_guard.py`. Python 3.9+, standard library only.

## Why

An unranged `Read` pulls the whole file into context, up to 25k tokens per call by default, and Claude Code only refuses a whole file over 256 KB ([Claude Code tools reference](https://code.claude.com/docs/en/tools-reference), checked on v2.1.325, 2026-11-14). Everything between a normal handoff doc and that ceiling goes straight in. read-guard draws the line at 32 KB by default. Across the author's 14,411 Markdown files, measured 2026-11-14 (median 2.7 KB, p90 10.6 KB), that let 98% through untouched and caught 264 files. Claude Code would let 258 of those through, at an estimated 8k to 25k tokens per read (about 4 bytes per token).

## What Claude sees

Example, from a 34 KB test file:

```
read-guard: blocked an unranged Read of /work/notes/handoff.md
Size 34.1 KB (34,880 bytes), over the 32 KB threshold. 893 lines.
Line cap: a Read of a file over the threshold needs a limit of 400 or fewer.
Headings (line: heading):
  4: # Sample handoff
  6: ## Part 1
  14: ### Detail
  117: ## Part 2
  ...
Read the section you need: offset at its heading line, limit up to the next heading (400 at most).
If the whole file is truly needed, read it in ranges of up to 400 lines and say why.
```

A Read with a limit over the cap gets the same map under a different first line: `read-guard: blocked a Read of /work/notes/handoff.md with limit 100,000, over the 400-line cap`.

A ranged Read whose lines add up to more than the threshold is denied with the range's size and the largest `limit` that fits, then the same map. When one line alone is over the threshold, no `limit` fits, so the hint offers only bounded output. Example, from a 67 KB one-line JSON test file:

```
read-guard: blocked a ranged Read of /work/data/store.json, line 1
Range size 66.6 KB (68,206 bytes), over the 32 KB threshold.
Line 1 alone is 66.6 KB, over the threshold, so no smaller limit fits.
Size 66.6 KB (68,206 bytes), over the 32 KB threshold. 1 line.
Line cap: a Read of a file over the threshold needs a limit of 400 or fewer.
First line:
  1: {"version":3,"entries":[{"id":0,"title":"Item 0","body":"lorem ipsum lorem ipsum lorem ipsum "},{"id":1,"title":"Item 1","body":"lorem ipsum lorem ipsum lore...
Search it with a bounded match that prints at most 80 characters either side of each hit (replace TERM): grep -noE '.{0,80}TERM.{0,80}' /work/data/store.json | head -n 20
For a .json file, jq can pull out only the part you need, e.g. jq -c 'keys[:20]' /work/data/store.json | head -c 4000
Never print the whole line: no cat, sed, head -n, or grep without -o on this file.
```

The `jq` line appears only for `.json` files.

An unranged or over-cap Read of a file with any line over the threshold gets the same bounded hint in place of the grep-or-range hint, after a line naming the longest one (`Line 1 is 66.6 KB, over the threshold, so no Read can include it.`). The closing read-in-ranges line is left out, since no range that holds that line can pass. Markdown files with headings keep their section hint as well.

Headings are capped at 40, then `... and N more: grep for ^# and read that range`. Headings inside fenced code blocks and YAML front matter are skipped. Files without headings get their first 5 lines and a hint to grep or read a range, or the bounded hint when a line is over the threshold.

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
2. Allows a path that is missing, not a regular file, or unreadable.
3. Allows extensions outside the text list. PDFs and images are never guarded, even if you list them.
4. Allows a path that matches an `ignore` glob, or does not match any `watch` glob.
5. Allows a file at or under `threshold_kb`, whatever its `limit`.
6. Allows a ranged read that passes two checks. Line cap: `tool_input` has an integer `limit` of `max_lines` or less. Range size: the lines it asks for, from `offset` (or line 1) for `limit` lines, add up to `threshold_kb` or less, so a few very long lines cannot slip through under the cap. `offset` alone still reads to the end of the file, and so does `allow_large: true`, so both count as unranged. A `limit` that is not an integer counts as missing.
7. Otherwise (no `limit`, a `limit` over `max_lines`, or a range over `threshold_kb`) prints a JSON `permissionDecision: "deny"` with the reason above and exits 0. Claude Code shows the reason to Claude.
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
  "max_lines": 400,
  "watch": ["**/*"],
  "ignore": ["**/node_modules/**", "./CHANGELOG.md"],
  "extensions": [".md", ".markdown", ".txt", ".rst", ".org", ".log", ".csv", ".tsv", ".json", ".yaml", ".yml"]
}
```

| Key | Default | Meaning |
|---|---|---|
| `threshold_kb` | `32` | Files larger than this many KiB are guarded. |
| `max_lines` | `400` | Largest `limit` allowed on a guarded file. Must be a positive integer. |
| `watch` | `["**/*"]` | Only paths matching one of these globs are guarded. |
| `ignore` | `[]` | Paths matching any of these are never guarded. Wins over `watch`. |
| `extensions` | the list above | Text extensions to guard. Replaces the default list. |

Glob rules: `*` and `?` stay within one path segment, and `**` crosses segments. A pattern starting with `/` or `~/` is absolute. A pattern starting with `./` is relative to the project root. Any other pattern matches at any depth, so `docs/**` matches every `docs` folder. Matching is case-insensitive on macOS and Windows and is checked against both the path as given and its resolved real path.

Set `READ_GUARD=off` in the environment to disable the guard for a session.

## Limits

- Claude Code only. Cowork and chat have no hooks.
- It covers the band Claude Code lets through today: from 32 KB up to Claude Code's own 256 KB refusal, where each read can cost up to 25k tokens by default ([tools reference](https://code.claude.com/docs/en/tools-reference), checked on v2.1.325, 2026-11-14). It also covers `allow_large` reads, which skip that refusal.
- It cannot stop a Bash `cat`, `head` or `sed` of the same file.
- Files added with `@` in a prompt are attached without a tool call, so they never pass through the hook.
- It caps each Read, not the total. Many Reads of `max_lines` each can still walk a whole file.

## Tests

```sh
python3 -m unittest discover -s tests -v
python3 read_guard.py --selftest
```

CI runs both on Ubuntu with Python 3.9 and 3.13, and on macOS with Python 3.13.

**Prior art:** [Donchitos/Claude-Code-Game-Studios](https://github.com/Donchitos/Claude-Code-Game-Studios) (MIT). read-guard is written from scratch and shares no code with it.

## License

MIT. See [LICENSE](LICENSE).
