#!/usr/bin/env python3
"""read-guard: a Claude Code PreToolUse hook for the Read tool.

Denies a Read of a large text file that has no limit, or a limit over the
line cap, and gives Claude a map of the file instead (Markdown headings with
line numbers, or the first lines), so it can read only the range it needs.
A ranged Read whose lines add up to more than the threshold is denied too,
with the range's byte size and a limit that fits.

Python 3.9+, standard library only. Fails open: if anything unexpected
happens, the Read goes ahead. Never writes to any settings file.

As a hook:   Claude Code pipes the PreToolUse payload JSON to stdin.
Self-check:  python3 read_guard.py --selftest
"""

from __future__ import annotations

import json
import math
import os
import re
import sys

__version__ = "0.2.0"

DEFAULT_THRESHOLD_KB = 32
# Largest `limit` allowed on a file over the threshold. Config key: max_lines.
DEFAULT_MAX_LINES = 400
DEFAULT_WATCH = ("**/*",)
DEFAULT_EXTENSIONS = (
    ".md", ".markdown", ".txt", ".rst", ".org", ".log",
    ".csv", ".tsv", ".json", ".yaml", ".yml",
)
MARKDOWN_EXTENSIONS = (".md", ".markdown")
# Never guarded, even if a config lists them: Claude Code reads PDFs and
# images through their own paths with their own limits.
NEVER_GUARDED = frozenset((
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".tif", ".tiff", ".ico", ".heic", ".heif", ".avif",
))
MAX_HEADINGS = 40
FIRST_LINES = 5
MAX_LINE_CHARS = 160
EM_DASH = "\u2014"

# macOS and Windows file systems are case-insensitive by default, so globs
# should not care whether a path arrives as ~/Developer or ~/developer.
_CASE_FLAGS = re.IGNORECASE if sys.platform in ("darwin", "win32") else 0


class ConfigError(ValueError):
    """A config file exists but cannot be used. The hook then fails open."""


# ---------------------------------------------------------------- config

def _home(env):
    return env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~")


def _expand_user(path, home):
    if path == "~" or path.startswith("~/"):
        return home + path[1:]
    return path


def config_paths(env, project_root, home):
    """Candidate config files in precedence order. First one found wins."""
    paths = []
    explicit = env.get("READ_GUARD_CONFIG")
    if explicit:
        paths.append(_expand_user(explicit, home))
    paths.append(os.path.join(project_root, ".claude", "read-guard.json"))
    paths.append(os.path.join(home, ".config", "read-guard", "config.json"))
    return paths


def load_config(env, project_root, home):
    """Return (config, source_path). source_path is None for defaults."""
    for path in config_paths(env, project_root, home):
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                return parse_config(json.load(fh)), path
    return parse_config({}), None


def _str_list(data, key, default):
    value = data.get(key, default)
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise ConfigError("%s must be a list of strings" % key)
    return list(value)


def _normalize_ext(ext):
    ext = ext.strip().lower()
    return ext if ext.startswith(".") else "." + ext


def parse_config(data):
    if not isinstance(data, dict):
        raise ConfigError("config must be a JSON object")
    threshold = data.get("threshold_kb", DEFAULT_THRESHOLD_KB)
    if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
            or not math.isfinite(threshold) or threshold < 0):
        raise ConfigError("threshold_kb must be a non-negative number")
    max_lines = data.get("max_lines", DEFAULT_MAX_LINES)
    if isinstance(max_lines, bool) or not isinstance(max_lines, int) or max_lines < 1:
        raise ConfigError("max_lines must be a positive integer")
    return {
        "threshold_kb": threshold,
        "max_lines": max_lines,
        "watch": _str_list(data, "watch", list(DEFAULT_WATCH)),
        "ignore": _str_list(data, "ignore", []),
        "extensions": [_normalize_ext(e) for e in
                       _str_list(data, "extensions", list(DEFAULT_EXTENSIONS))],
    }


# ----------------------------------------------------------------- globs

_GLOB_META = re.compile(r"([*?\[])")


def _escape_glob(text):
    return _GLOB_META.sub(r"[\1]", text)


def glob_to_regex(pattern):
    """Translate a glob to a regex. `*` and `?` stay inside one path segment,
    `**` crosses segments, and `**/` also matches zero directories."""
    out = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**", i):
                i += 2
                if i < n and pattern[i] == "/":
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out.append(re.escape(c))
            else:
                body = pattern[i + 1:end].replace("\\", "\\\\")
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body + "]")
                i = end + 1
                continue
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def anchor_glob(pattern, project_root, home):
    """Make a glob absolute.

    `/abs/...` and `~/...` are absolute. `./x` is relative to the project
    root. Anything else matches at any depth, as if prefixed with `**/`.
    """
    if pattern == "~" or pattern.startswith("~/"):
        return _escape_glob(home.replace(os.sep, "/").rstrip("/")) + pattern[1:]
    if pattern.startswith("/"):
        return pattern
    if pattern.startswith("./"):
        return _escape_glob(project_root.replace(os.sep, "/").rstrip("/")) + pattern[1:]
    if pattern.startswith("**"):
        return pattern
    return "**/" + pattern


def _path_forms(path):
    forms = []
    for p in (os.path.abspath(path), os.path.realpath(path)):
        p = p.replace(os.sep, "/")
        if p not in forms:
            forms.append(p)
    return forms


def matches_any(path, patterns, project_root, home):
    forms = _path_forms(path)
    for pattern in patterns:
        rx = re.compile(glob_to_regex(anchor_glob(pattern, project_root, home)), _CASE_FLAGS)
        if any(rx.fullmatch(f) for f in forms):
            return True
    return False


# --------------------------------------------------------------- scanning

_HEADING = re.compile(r" {0,3}#{1,6}(?:[ \t]|$)")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")


class _MarkdownHeadings:
    """Line-by-line ATX heading detector that skips YAML front matter and
    fenced code blocks, so a `# comment` inside a code fence is not a heading."""

    def __init__(self):
        self.line = 0
        self.front_matter = False
        self.fence = None

    def is_heading(self, text):
        self.line += 1
        stripped = text.strip()
        if self.line == 1 and stripped == "---":
            self.front_matter = True
            return False
        if self.front_matter:
            if stripped in ("---", "..."):
                self.front_matter = False
            return False
        fence = _FENCE.match(text)
        if self.fence is not None:
            if (fence and fence.group(1)[0] == self.fence[0]
                    and len(fence.group(1)) >= len(self.fence)
                    and not text[fence.end():].strip()):
                self.fence = None
            return False
        if fence:
            self.fence = fence.group(1)
            return False
        return bool(_HEADING.match(text))


def _clip(text):
    text = text.rstrip("\r\n")
    if len(text) > MAX_LINE_CHARS:
        text = text[:MAX_LINE_CHARS - 3] + "..."
    return text


def _decode(raw, first):
    text = raw.decode("utf-8", "replace")
    return text.lstrip("﻿") if first else text


def scan_file(path, is_markdown):
    """Return (line_count, first_lines, headings, heading_total).

    first_lines and headings are lists of (line_number, text), already capped.
    """
    first_lines, headings, heading_total = [], [], 0
    with open(path, "rb") as fh:
        if is_markdown:
            detector = _MarkdownHeadings()
            line_count = 0
            for line_count, raw in enumerate(fh, 1):
                text = _decode(raw, line_count == 1).rstrip("\r\n")
                if len(first_lines) < FIRST_LINES:
                    first_lines.append((line_count, _clip(text)))
                if detector.is_heading(text):
                    heading_total += 1
                    if len(headings) < MAX_HEADINGS:
                        headings.append((line_count, _clip(text.strip())))
            return line_count, first_lines, headings, heading_total

        head = chunk = fh.read(1 << 16)
        newlines, total, ends_with_newline = 0, 0, True
        while chunk:
            newlines += chunk.count(b"\n")
            total += len(chunk)
            ends_with_newline = chunk.endswith(b"\n")
            chunk = fh.read(1 << 20)
    line_count = newlines + (0 if ends_with_newline or total == 0 else 1)
    for number, raw in enumerate(head.split(b"\n")[:min(FIRST_LINES, line_count)], 1):
        first_lines.append((number, _clip(_decode(raw, number == 1))))
    return line_count, first_lines, headings, heading_total


# ---------------------------------------------------------------- decision

def _fmt_number(value):
    return str(int(value)) if float(value).is_integer() else ("%g" % value)


def no_em_dash(text):
    return text.replace(EM_DASH, "-")


def build_reason(shown_path, path, size, threshold_kb, max_lines, limit, is_markdown):
    line_count, first_lines, headings, heading_total = scan_file(path, is_markdown)
    cap = format(max_lines, ",")
    if limit is None:
        header = "read-guard: blocked an unranged Read of %s" % shown_path
    else:
        header = ("read-guard: blocked a Read of %s with limit %s, over the %s-line cap"
                  % (shown_path, format(limit, ","), cap))
    out = [
        header,
        "Size %.1f KB (%s bytes), over the %s KB threshold. %s lines."
        % (size / 1024.0, format(size, ","), _fmt_number(threshold_kb), format(line_count, ",")),
        "Line cap: a Read of a file over the threshold needs a limit of %s or fewer." % cap,
    ]
    if is_markdown and headings:
        out.append("Headings (line: heading):")
        out.extend("  %d: %s" % item for item in headings)
        more = heading_total - len(headings)
        if more > 0:
            out.append("  ... and %d more: grep for ^# and read that range" % more)
        out.append("Read the section you need: offset at its heading line, "
                   "limit up to the next heading (%s at most)." % cap)
    else:
        out.append("First %d lines:" % len(first_lines))
        out.extend("  %d: %s" % item for item in first_lines)
        out.append("Grep for what you need, or Read a range with offset and limit "
                   "(limit %s or less)." % cap)
    out.append("If the whole file is truly needed, read it in ranges of up to %s lines "
               "and say why." % cap)
    return no_em_dash("\n".join(out))


def measure_range(path, start, limit, threshold):
    """Measure lines start .. start+limit-1 (1-based), line endings included.

    Returns (range_bytes, end_line, first_line_bytes, fit). end_line is the
    last line actually in the range (start - 1 when the range is past the
    end of the file); fit is how many lines from `start` stay within
    `threshold` bytes.
    """
    range_bytes, end_line, first_line_bytes, fit = 0, start - 1, 0, 0
    stop = start + limit
    with open(path, "rb") as fh:
        for number, raw in enumerate(fh, 1):
            if number < start:
                continue
            if number >= stop:
                break
            range_bytes += len(raw)
            end_line = number
            if number == start:
                first_line_bytes = len(raw)
            if range_bytes <= threshold:
                fit += 1
    return range_bytes, end_line, first_line_bytes, fit


def build_range_reason(shown_path, start, end, range_bytes, first_line_bytes, fit,
                       threshold_kb):
    first = format(start, ",")
    lines = ("line %s" % first) if end == start else ("lines %s-%s" % (first, format(end, ",")))
    out = [
        "read-guard: blocked a ranged Read of %s, %s" % (shown_path, lines),
        "Range size %.1f KB (%s bytes), over the %s KB threshold."
        % (range_bytes / 1024.0, format(range_bytes, ","), _fmt_number(threshold_kb)),
    ]
    if fit:
        out.append("Use a smaller limit: from line %s, a limit of %s or less stays within "
                   "the threshold." % (first, format(fit, ",")))
        out.append("If the whole range is truly needed, read it in smaller ranges and say why.")
    else:
        out.append("Line %s alone is %.1f KB, over the threshold, so no smaller limit fits. "
                   "Grep for what you need instead." % (first, first_line_bytes / 1024.0))
    return no_em_dash("\n".join(out))


def evaluate(raw, env):
    """Return the deny payload (a dict) if this Read should be blocked, else None.

    May raise on unexpected input; handle() turns any exception into "allow".
    """
    if env.get("READ_GUARD", "").strip().lower() == "off":
        return None
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("tool_name") != "Read":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    # offset alone, or allow_large alone, is still a read to the end of the
    # file, so only `limit` makes a Read ranged. A limit that is not an
    # integer counts as missing.
    limit = tool_input.get("limit")
    if isinstance(limit, bool) or not isinstance(limit, int):
        limit = None
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None

    home = _home(env)
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        cwd = os.getcwd()
    path = _expand_user(file_path, home)
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    path = os.path.normpath(path)
    if not os.path.isfile(path) or not os.access(path, os.R_OK):
        return None

    ext = os.path.splitext(path)[1].lower()
    if ext in NEVER_GUARDED:
        return None
    project_root = env.get("CLAUDE_PROJECT_DIR") or cwd
    config, _source = load_config(env, project_root, home)
    if ext not in config["extensions"]:
        return None
    if matches_any(path, config["ignore"], project_root, home):
        return None
    if not matches_any(path, config["watch"], project_root, home):
        return None

    size = os.path.getsize(path)
    threshold = config["threshold_kb"] * 1024
    if size <= threshold:
        return None
    if limit is not None and limit <= config["max_lines"]:
        # Under the line cap a range can still be huge when its lines are
        # long, so measure the bytes of the requested lines too. Read's
        # offset is a 1-based line number; missing or below 1 means line 1.
        offset = tool_input.get("offset")
        start = offset if (isinstance(offset, int) and not isinstance(offset, bool)
                           and offset > 1) else 1
        range_bytes, end, first_bytes, fit = measure_range(path, start, limit, threshold)
        if range_bytes <= threshold:
            return None
        reason = build_range_reason(file_path, start, end, range_bytes, first_bytes, fit,
                                    config["threshold_kb"])
    else:
        reason = build_reason(file_path, path, size, config["threshold_kb"],
                              config["max_lines"], limit, ext in MARKDOWN_EXTENSIONS)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def handle(raw, env):
    """Hook core: stdin text in, stdout text out ("" means allow). Never raises."""
    try:
        decision = evaluate(raw, env)
        return json.dumps(decision) if decision else ""
    except Exception:
        return ""


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--selftest"]:
        return selftest()
    try:
        out = handle(sys.stdin.read(), os.environ)
        if out:
            sys.stdout.write(out + "\n")
            sys.stdout.flush()
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------- selftest

def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path


def _big_markdown(headings=6, size_kb=40, extra=""):
    parts = ["# Title\n", "\n", extra]
    for i in range(1, headings):
        parts.append("## Section %d\n\nSome prose for section %d.\n\n" % (i, i))
    body = "".join(parts)
    filler = "Filler line that is not a heading.\n"
    while len(body.encode("utf-8")) <= size_kb * 1024:
        body += filler
    return body


def _no_headings_text(size_kb=40, first="Row one of the export"):
    lines = [first] + ["row %05d, value %d" % (i, i * 7) for i in range(1, 6000)]
    body = "\n".join(lines) + "\n"
    while len(body.encode("utf-8")) <= size_kb * 1024:
        body += "row extra\n"
    return body


def _payload(file_path, tool_name="Read", cwd=None, **tool_input):
    tool_input["file_path"] = file_path
    return json.dumps({
        "session_id": "selftest", "hook_event_name": "PreToolUse",
        "cwd": cwd or os.path.dirname(file_path),
        "tool_name": tool_name, "tool_input": tool_input,
    })


def _deny_reason(out):
    assert out, "expected a deny, got allow"
    data = json.loads(out)
    hso = data["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse", hso
    assert hso["permissionDecision"] == "deny", hso
    reason = hso["permissionDecisionReason"]
    assert isinstance(reason, str) and reason, "empty reason"
    return reason


def _selftest_cases(tmp):
    home = os.path.join(tmp, "home")
    proj = os.path.join(tmp, "proj")
    os.makedirs(home)
    os.makedirs(proj)
    env = {"HOME": home, "CLAUDE_PROJECT_DIR": proj}

    big_md = _write(os.path.join(proj, "docs", "big.md"), _big_markdown())
    small_md = _write(os.path.join(proj, "docs", "small.md"), "# Small\n\nShort.\n")
    flat_md = _write(os.path.join(proj, "docs", "flat.md"), _no_headings_text(first="No heading here"))
    big_log = _write(os.path.join(proj, "logs", "app.log"), _no_headings_text())
    big_pdf = _write(os.path.join(proj, "docs", "big.pdf"), "x" * (64 * 1024))
    big_png = _write(os.path.join(proj, "docs", "big.png"), "x" * (64 * 1024))

    def allow(out):
        assert out == "", "expected allow, got: %s" % out[:200]

    def c_non_read():
        allow(handle(_payload(big_md, tool_name="Bash", command="cat x"), env))

    def c_limit():
        allow(handle(_payload(big_md, limit=50), env))
        allow(handle(_payload(big_md, offset=10, limit=50), env))
        allow(handle(_payload(big_md, limit=DEFAULT_MAX_LINES), env))

    def c_limit_over_cap():
        reason = _deny_reason(handle(_payload(big_md, limit=DEFAULT_MAX_LINES + 1), env))
        assert "with limit 401, over the 400-line cap" in reason, reason[:300]
        reason = _deny_reason(handle(_payload(big_md, offset=10, limit=100000), env))
        assert "with limit 100,000, over the 400-line cap" in reason, reason[:300]
        assert "  1: # Title" in reason, "outline missing on over-cap deny"
        _deny_reason(handle(_payload(big_md, allow_large=True, limit=100000), env))

    def c_non_integer_limit():
        for bad in ("100000", "40", 40.0, True, [40], {"n": 40}):
            reason = _deny_reason(handle(_payload(big_md, limit=bad), env))
            assert "blocked an unranged Read" in reason, "limit %r: %s" % (bad, reason[:200])

    def c_max_lines_config():
        cfg = _write(os.path.join(tmp, "cfg-max.json"), json.dumps({"max_lines": 1000}))
        e = dict(env, READ_GUARD_CONFIG=cfg)
        # big_log has short lines: 1,000 of them stay under the byte threshold,
        # so this exercises the line cap alone.
        allow(handle(_payload(big_log, limit=1000), e))
        reason = _deny_reason(handle(_payload(big_log, limit=1001), e))
        assert "over the 1,000-line cap" in reason, reason[:300]
        assert "needs a limit of 1,000 or fewer" in reason, reason[:400]

    def c_offset_only():
        _deny_reason(handle(_payload(big_md, offset=100), env))

    def c_allow_large():
        _deny_reason(handle(_payload(big_md, allow_large=True), env))

    def c_small():
        allow(handle(_payload(small_md), env))
        allow(handle(_payload(small_md, limit=100000), env))

    def c_big_markdown():
        reason = _deny_reason(handle(_payload(big_md), env))
        assert big_md in reason, "path missing"
        assert "32 KB threshold" in reason, "threshold missing"
        assert " lines." in reason, "line count missing"
        assert "needs a limit of 400 or fewer" in reason, "line cap missing"
        assert "  1: # Title" in reason, "first heading missing"
        assert "## Section 5" in reason, "later heading missing"
        assert reason.splitlines()[-1] == ("If the whole file is truly needed, read it in "
                                           "ranges of up to 400 lines and say why."), "closing line"

    def c_code_fence_is_not_heading():
        p = _write(os.path.join(proj, "docs", "fenced.md"),
                   _big_markdown(extra="```bash\n# not a heading\n```\n\n"))
        reason = _deny_reason(handle(_payload(p), env))
        assert "not a heading" not in reason, "fenced comment listed as heading"

    def c_no_headings():
        reason = _deny_reason(handle(_payload(flat_md), env))
        assert "First 5 lines:" in reason and "  1: No heading here" in reason, reason[:300]
        reason = _deny_reason(handle(_payload(big_log), env))
        assert "  1: Row one of the export" in reason and "  5: row 00004" in reason, reason[:300]
        assert "Grep for what you need" in reason, "grep hint missing"

    def c_pdf_image():
        allow(handle(_payload(big_pdf), env))
        allow(handle(_payload(big_png), env))
        cfg = _write(os.path.join(tmp, "cfg-pdf.json"),
                     json.dumps({"extensions": [".pdf", ".png", ".md"]}))
        e = dict(env, READ_GUARD_CONFIG=cfg)
        allow(handle(_payload(big_pdf), e))
        allow(handle(_payload(big_png), e))

    def c_extension_not_listed():
        p = _write(os.path.join(proj, "src", "big.py"), _no_headings_text())
        allow(handle(_payload(p), env))

    def c_ignore_wins():
        cfg = _write(os.path.join(tmp, "cfg-ignore.json"),
                     json.dumps({"watch": ["docs/**"], "ignore": ["docs/big.md"]}))
        e = dict(env, READ_GUARD_CONFIG=cfg)
        allow(handle(_payload(big_md), e))
        _deny_reason(handle(_payload(flat_md), e))

    def c_watch_miss():
        cfg = _write(os.path.join(tmp, "cfg-watch.json"), json.dumps({"watch": ["./logs/**"]}))
        e = dict(env, READ_GUARD_CONFIG=cfg)
        allow(handle(_payload(big_md), e))
        _deny_reason(handle(_payload(big_log), e))

    def c_bad_json():
        allow(handle("{not json", env))
        allow(handle("", env))
        allow(handle("[1, 2]", env))

    def c_missing_file():
        allow(handle(_payload(os.path.join(proj, "nope.md")), env))
        allow(handle(_payload(proj), env))

    def c_off():
        allow(handle(_payload(big_md), dict(env, READ_GUARD="off")))

    def c_broken_config():
        cfg = _write(os.path.join(tmp, "cfg-broken.json"), "{oops")
        allow(handle(_payload(big_md), dict(env, READ_GUARD_CONFIG=cfg)))
        cfg = _write(os.path.join(tmp, "cfg-badtype.json"), json.dumps({"threshold_kb": "big"}))
        allow(handle(_payload(big_md), dict(env, READ_GUARD_CONFIG=cfg)))
        for bad in (0, -5, "400", True, 1.5):
            cfg = _write(os.path.join(tmp, "cfg-badmax.json"), json.dumps({"max_lines": bad}))
            allow(handle(_payload(big_md), dict(env, READ_GUARD_CONFIG=cfg)))

    def c_threshold_boundary():
        p = _write(os.path.join(proj, "docs", "edge.md"), "# Edge\n" + "a" * (32 * 1024 - 7))
        assert os.path.getsize(p) == 32 * 1024
        allow(handle(_payload(p), env))
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("b")
        _deny_reason(handle(_payload(p), env))

    def c_config_precedence():
        mid = _write(os.path.join(proj, "docs", "mid.md"), _big_markdown(size_kb=40))
        env_cfg = _write(os.path.join(tmp, "cfg-env.json"), json.dumps({"threshold_kb": 100}))
        proj_cfg = _write(os.path.join(proj, ".claude", "read-guard.json"),
                          json.dumps({"threshold_kb": 50}))
        user_cfg = _write(os.path.join(home, ".config", "read-guard", "config.json"),
                          json.dumps({"threshold_kb": 1}))
        try:
            e = dict(env, READ_GUARD_CONFIG=env_cfg)
            assert load_config(e, proj, home)[1] == env_cfg
            allow(handle(_payload(mid), e))
            missing = dict(env, READ_GUARD_CONFIG=os.path.join(tmp, "missing.json"))
            assert load_config(missing, proj, home)[1] == proj_cfg
            allow(handle(_payload(mid), env))
            os.remove(proj_cfg)
            assert load_config(env, proj, home)[1] == user_cfg
            assert "over the 1 KB threshold" in _deny_reason(handle(_payload(mid), env))
            os.remove(user_cfg)
            assert load_config(env, proj, home) == (parse_config({}), None)
            assert load_config(env, proj, home)[0]["threshold_kb"] == 32
        finally:
            for p in (proj_cfg, user_cfg):
                if os.path.exists(p):
                    os.remove(p)

    def c_heading_cap():
        p = _write(os.path.join(proj, "docs", "many.md"), _big_markdown(headings=55))
        reason = _deny_reason(handle(_payload(p), env))
        listed = [ln for ln in reason.splitlines() if re.match(r"  \d+: #", ln)]
        assert len(listed) == 40, "listed %d headings" % len(listed)
        assert "  ... and 15 more: grep for ^# and read that range" in reason, reason[-400:]

    def c_no_em_dash():
        md = _write(os.path.join(proj, "docs", "dash.md"),
                    _big_markdown(extra="## Plan %s phase two\n\n" % EM_DASH))
        txt = _write(os.path.join(proj, "docs", "dash.txt"),
                     _no_headings_text(first="Export %s raw rows" % EM_DASH))
        for p in (md, txt, big_md, flat_md, big_log):
            out = handle(_payload(p), env)
            assert EM_DASH not in out, "em dash in raw output"
            assert EM_DASH not in _deny_reason(out), "em dash in reason"
        assert "Plan - phase two" in _deny_reason(handle(_payload(md), env))

    def c_range_bytes():
        # 60 lines of exactly 1 KB: 40 of them is 40 KB, over the 32 KB threshold.
        kb = _write(os.path.join(proj, "logs", "kb-lines.log"), ("k" * 1023 + "\n") * 60)
        reason = _deny_reason(handle(_payload(kb, limit=40), env))
        assert "blocked a ranged Read of %s, lines 1-40\n" % kb in reason, reason[:300]
        assert "Range size 40.0 KB (40,960 bytes), over the 32 KB threshold." in reason, reason
        assert "from line 1, a limit of 32 or less" in reason, reason
        allow(handle(_payload(kb, limit=32), env))
        allow(handle(_payload(kb, offset=0, limit=32), env))
        reason = _deny_reason(handle(_payload(kb, offset=20, limit=40), env))
        assert "lines 20-59" in reason and "from line 20, a limit of 32" in reason, reason
        allow(handle(_payload(kb, offset=50, limit=40), env))
        # One line over the threshold: no limit fits, so point at Grep.
        one = _write(os.path.join(proj, "data", "one.json"),
                     '{"blob": "%s"}\n' % ("x" * (40 * 1024)))
        reason = _deny_reason(handle(_payload(one, limit=1), env))
        assert "blocked a ranged Read of %s, line 1\n" % one in reason, reason
        assert "no smaller limit fits" in reason and "Grep" in reason, reason
        # Short lines under the cap still pass.
        allow(handle(_payload(big_md, offset=10, limit=50), env))

    return [
        ("non-Read tool passes", c_non_read),
        ("Read with limit at or under the cap passes", c_limit),
        ("Read with limit over the cap is denied", c_limit_over_cap),
        ("non-integer limit counts as missing", c_non_integer_limit),
        ("max_lines from config sets the cap", c_max_lines_config),
        ("ranged Read over the byte threshold is denied", c_range_bytes),
        ("offset only on a big file is denied", c_offset_only),
        ("allow_large without limit is denied", c_allow_large),
        ("small file passes, with or without a huge limit", c_small),
        ("big Markdown denied with headings, valid deny JSON", c_big_markdown),
        ("code fence comment is not a heading", c_code_fence_is_not_heading),
        ("big file with no headings denied with first lines", c_no_headings),
        ("PDF and image ignored, even if configured", c_pdf_image),
        ("extension not in list passes", c_extension_not_listed),
        ("ignore glob wins over watch", c_ignore_wins),
        ("path outside watch globs passes", c_watch_miss),
        ("bad JSON allows", c_bad_json),
        ("missing file or directory allows", c_missing_file),
        ("READ_GUARD=off allows", c_off),
        ("broken config fails open", c_broken_config),
        ("size at threshold passes, one byte over is denied", c_threshold_boundary),
        ("config precedence: env, project, user, defaults", c_config_precedence),
        ("heading cap and '... and N more' line", c_heading_cap),
        ("no em dash in any output", c_no_em_dash),
    ]


def selftest():
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="read-guard-selftest-")
    results = []
    try:
        for name, case in _selftest_cases(tmp):
            try:
                case()
                results.append((name, None))
            except AssertionError as exc:
                results.append((name, str(exc) or "assertion failed"))
            except Exception as exc:
                results.append((name, "%s: %s" % (type(exc).__name__, exc)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for name, error in results:
        print(("ok    %s" % name) if error is None else ("FAIL  %s: %s" % (name, error)))
    failed = sum(1 for _, error in results if error)
    print("%d/%d passed" % (len(results) - failed, len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
