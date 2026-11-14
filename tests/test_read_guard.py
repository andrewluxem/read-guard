"""End-to-end tests: run read_guard.py as Claude Code would, with the hook
payload on stdin, and check stdout and the exit code.

Run: python3 -m unittest discover -s tests -v
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "read_guard.py")
EM_DASH = "\u2014"
CLOSING = "If the whole file is truly needed, read it in ranges and say why."


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path


def markdown(headings=6, size_kb=40, extra=""):
    body = "# Title\n\n" + extra
    for i in range(1, headings):
        body += "## Section %d\n\nProse for section %d.\n\n" % (i, i)
    while len(body.encode("utf-8")) <= size_kb * 1024:
        body += "Filler line that is not a heading.\n"
    return body


def rows(first="first row", size_kb=40):
    body = first + "\n" + "".join("row %05d\n" % i for i in range(1, 6000))
    while len(body.encode("utf-8")) <= size_kb * 1024:
        body += "row extra\n"
    return body


class HookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="read-guard-test-")
        self.home = os.path.join(self.tmp, "home")
        self.proj = os.path.join(self.tmp, "proj")
        os.makedirs(self.home)
        os.makedirs(self.proj)
        self.big_md = write(os.path.join(self.proj, "docs", "big.md"), markdown())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("READ_GUARD") and k != "CLAUDE_PROJECT_DIR"}
        env.update(HOME=self.home, CLAUDE_PROJECT_DIR=self.proj)
        env.update(extra)
        return env

    def run_hook(self, stdin, **env):
        proc = subprocess.run(
            [sys.executable, SCRIPT], input=stdin, capture_output=True,
            text=True, encoding="utf-8", env=self.env(**env), timeout=30,
        )
        return proc.returncode, proc.stdout

    def payload(self, path, tool_name="Read", **tool_input):
        tool_input["file_path"] = path
        return json.dumps({
            "session_id": "t", "hook_event_name": "PreToolUse", "cwd": self.proj,
            "tool_name": tool_name, "tool_input": tool_input,
        })

    def assertAllowed(self, stdin, **env):
        code, out = self.run_hook(stdin, **env)
        self.assertEqual(code, 0)
        self.assertEqual(out, "", "expected allow, got %r" % out[:300])

    def assertDenied(self, stdin, **env):
        code, out = self.run_hook(stdin, **env)
        self.assertEqual(code, 0, "deny must exit 0 so Claude Code reads the JSON")
        self.assertNotIn(EM_DASH, out)
        data = json.loads(out)
        self.assertEqual(list(data), ["hookSpecificOutput"])
        hso = data["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertEqual(hso["permissionDecision"], "deny")
        reason = hso["permissionDecisionReason"]
        self.assertNotIn(EM_DASH, reason)
        self.assertEqual(reason.splitlines()[-1], CLOSING)
        return reason

    def config(self, name, data):
        return write(os.path.join(self.tmp, name), json.dumps(data))

    # Spec: Tests list -----------------------------------------------------

    def test_non_read_passes(self):
        self.assertAllowed(self.payload(self.big_md, tool_name="Bash", command="cat x"))
        self.assertAllowed(self.payload(self.big_md, tool_name="Edit"))

    def test_read_with_limit_passes(self):
        self.assertAllowed(self.payload(self.big_md, limit=40))
        self.assertAllowed(self.payload(self.big_md, offset=200, limit=40))
        self.assertAllowed(self.payload(self.big_md, allow_large=True, limit=40))

    def test_offset_only_is_denied(self):
        self.assertDenied(self.payload(self.big_md, offset=300))

    def test_allow_large_without_limit_is_denied(self):
        self.assertDenied(self.payload(self.big_md, allow_large=True))

    def test_small_file_passes(self):
        small = write(os.path.join(self.proj, "small.md"), "# Small\n\nShort.\n")
        self.assertAllowed(self.payload(small))

    def test_big_markdown_denied_with_headings(self):
        reason = self.assertDenied(self.payload(self.big_md))
        self.assertIn(self.big_md, reason)
        self.assertIn("over the 32 KB threshold", reason)
        self.assertRegex(reason, r"[\d,]+ lines\.")
        self.assertIn("  1: # Title", reason)
        self.assertIn("  7: ## Section 2", reason)
        self.assertIn("## Section 5", reason)

    def test_big_file_without_headings_denied_with_first_lines(self):
        flat_md = write(os.path.join(self.proj, "flat.md"), rows(first="plain opening line"))
        reason = self.assertDenied(self.payload(flat_md))
        self.assertIn("First 5 lines:", reason)
        self.assertIn("  1: plain opening line", reason)
        self.assertNotIn("Headings", reason)

        log = write(os.path.join(self.proj, "logs", "app.log"), rows(first="boot ok"))
        reason = self.assertDenied(self.payload(log))
        self.assertIn("  1: boot ok", reason)
        self.assertIn("  5: row 00004", reason)
        self.assertNotIn("  6: ", reason)
        self.assertIn("Grep for what you need, or Read a range with offset and limit.", reason)

    def test_pdf_and_image_ignored(self):
        pdf = write(os.path.join(self.proj, "big.pdf"), "%PDF" + "x" * 70000)
        png = write(os.path.join(self.proj, "big.png"), "x" * 70000)
        jpg = write(os.path.join(self.proj, "big.JPG"), "x" * 70000)
        for path in (pdf, png, jpg):
            self.assertAllowed(self.payload(path))
        cfg = self.config("cfg.json", {"extensions": [".pdf", ".png", ".jpg"]})
        for path in (pdf, png, jpg):
            self.assertAllowed(self.payload(path), READ_GUARD_CONFIG=cfg)

    def test_ignore_glob_wins_over_watch(self):
        other = write(os.path.join(self.proj, "docs", "other.md"), markdown())
        cfg = self.config("cfg.json", {"watch": ["docs/**"], "ignore": ["**/big.md"]})
        self.assertAllowed(self.payload(self.big_md), READ_GUARD_CONFIG=cfg)
        self.assertDenied(self.payload(other), READ_GUARD_CONFIG=cfg)

    def test_bad_json_exits_zero(self):
        for stdin in ("{not json", "", "null", "[]", '{"tool_name": "Read"}',
                      '{"tool_name": "Read", "tool_input": "x"}'):
            self.assertAllowed(stdin)

    def test_read_guard_off(self):
        self.assertAllowed(self.payload(self.big_md), READ_GUARD="off")
        self.assertAllowed(self.payload(self.big_md), READ_GUARD="OFF")
        self.assertDenied(self.payload(self.big_md), READ_GUARD="on")

    def test_config_precedence(self):
        env_cfg = self.config("env.json", {"threshold_kb": 100})
        proj_cfg = write(os.path.join(self.proj, ".claude", "read-guard.json"),
                         json.dumps({"threshold_kb": 60}))
        user_cfg = write(os.path.join(self.home, ".config", "read-guard", "config.json"),
                         json.dumps({"threshold_kb": 2}))
        # $READ_GUARD_CONFIG beats the project file.
        self.assertAllowed(self.payload(self.big_md), READ_GUARD_CONFIG=env_cfg)
        # A $READ_GUARD_CONFIG that does not exist falls through to the project file.
        self.assertAllowed(self.payload(self.big_md),
                           READ_GUARD_CONFIG=os.path.join(self.tmp, "missing.json"))
        # The project file beats the user file.
        self.assertAllowed(self.payload(self.big_md))
        os.remove(proj_cfg)
        # Then the user file.
        self.assertIn("over the 2 KB threshold", self.assertDenied(self.payload(self.big_md)))
        os.remove(user_cfg)
        # Then the defaults.
        self.assertIn("over the 32 KB threshold", self.assertDenied(self.payload(self.big_md)))

    def test_heading_cap_and_more_line(self):
        many = write(os.path.join(self.proj, "many.md"), markdown(headings=55))
        reason = self.assertDenied(self.payload(many))
        listed = [ln for ln in reason.splitlines() if re.match(r"  \d+: #", ln)]
        self.assertEqual(len(listed), 40)
        self.assertIn("  ... and 15 more: grep for ^# and read that range", reason)

        forty = write(os.path.join(self.proj, "forty.md"), markdown(headings=40))
        self.assertNotIn("more: grep", self.assertDenied(self.payload(forty)))

    def test_no_em_dash_in_any_output(self):
        md = write(os.path.join(self.proj, "dash.md"),
                   markdown(extra="## Plan %s phase two\n\n" % EM_DASH))
        txt = write(os.path.join(self.proj, "dash.txt"), rows(first="a %s b" % EM_DASH))
        self.assertIn("Plan - phase two", self.assertDenied(self.payload(md)))
        self.assertIn("  1: a - b", self.assertDenied(self.payload(txt)))
        proc = subprocess.run([sys.executable, SCRIPT, "--selftest"], capture_output=True,
                              text=True, encoding="utf-8", env=self.env(), timeout=60)
        self.assertNotIn(EM_DASH, proc.stdout + proc.stderr)

    def test_selftest_passes(self):
        proc = subprocess.run([sys.executable, SCRIPT, "--selftest"], capture_output=True,
                              text=True, encoding="utf-8", env=self.env(), timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"(\d+)/\1 passed")

    # Edges around the spec rules ----------------------------------------

    def test_missing_unreadable_or_directory_passes(self):
        self.assertAllowed(self.payload(os.path.join(self.proj, "nope.md")))
        self.assertAllowed(self.payload(self.proj))
        locked = write(os.path.join(self.proj, "locked.md"), markdown())
        os.chmod(locked, 0)
        try:
            if os.access(locked, os.R_OK):
                self.skipTest("running as a user that ignores file modes")
            self.assertAllowed(self.payload(locked))
        finally:
            os.chmod(locked, 0o600)

    def test_extension_not_listed_passes(self):
        py = write(os.path.join(self.proj, "big.py"), rows())
        self.assertAllowed(self.payload(py))
        cfg = self.config("cfg.json", {"extensions": ["py"]})
        self.assertDenied(self.payload(py), READ_GUARD_CONFIG=cfg)
        self.assertAllowed(self.payload(self.big_md), READ_GUARD_CONFIG=cfg)

    def test_watch_globs(self):
        log = write(os.path.join(self.proj, "logs", "app.log"), rows())
        cfg = self.config("cfg.json", {"watch": ["./logs/**"]})
        self.assertAllowed(self.payload(self.big_md), READ_GUARD_CONFIG=cfg)
        self.assertDenied(self.payload(log), READ_GUARD_CONFIG=cfg)
        cfg = self.config("home.json", {"watch": ["~/notes/**"]})
        note = write(os.path.join(self.home, "notes", "n.md"), markdown())
        self.assertDenied(self.payload(note), READ_GUARD_CONFIG=cfg)
        self.assertAllowed(self.payload(self.big_md), READ_GUARD_CONFIG=cfg)

    def test_threshold_boundary(self):
        edge = write(os.path.join(self.proj, "edge.md"), "# Edge\n" + "a" * (32 * 1024 - 7))
        self.assertEqual(os.path.getsize(edge), 32 * 1024)
        self.assertAllowed(self.payload(edge))
        with open(edge, "a", encoding="utf-8") as fh:
            fh.write("b")
        self.assertDenied(self.payload(edge))

    def test_broken_config_fails_open(self):
        broken = write(os.path.join(self.tmp, "broken.json"), "{oops")
        self.assertAllowed(self.payload(self.big_md), READ_GUARD_CONFIG=broken)
        bad = self.config("bad.json", {"watch": "docs/**"})
        self.assertAllowed(self.payload(self.big_md), READ_GUARD_CONFIG=bad)

    def test_fenced_code_and_front_matter_are_not_headings(self):
        extra = "```bash\n# install step\n```\n\n"
        doc = write(os.path.join(self.proj, "fm.md"),
                    "---\ntitle: x\n# yaml comment\n---\n" + markdown(extra=extra))
        reason = self.assertDenied(self.payload(doc))
        self.assertNotIn("install step", reason)
        self.assertNotIn("yaml comment", reason)
        self.assertIn("  5: # Title", reason)


class RepoTest(unittest.TestCase):
    def test_no_em_dash_in_repo(self):
        offenders = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
            for name in filenames:
                path = os.path.join(dirpath, name)
                with open(path, "rb") as fh:
                    if EM_DASH.encode("utf-8") in fh.read():
                        offenders.append(os.path.relpath(path, ROOT))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
