"""Inshellisense JSON bridge and blue, emoji-free popup."""
import curses
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from terminal_ai.completion import (
    Completion, CompletionWorker, matching_completions, parse_completions,
    replace_completion, safe_to_complete,
)
from terminal_ai.core import load_config
from terminal_ai.ui import App
from test_appearance import FakeWindow


class CompletionBridgeTests(unittest.TestCase):
    def test_parse_valid_and_deduplicate(self):
        payload = json.dumps({"suggestions": [
            {"name": "git", "type": "subcommand", "description": "Version control"},
            {"name": "git", "type": "subcommand"},
            {"name": "Downloads", "insertValue": "Downloads/", "type": "folder"},
            {"name": "\x1b[31msecret\x1b[0m", "type": "file"},
        ]})
        out = parse_completions(payload)
        self.assertEqual([s.name for s in out], ["git", "Downloads", "secret"])
        self.assertEqual([s.kind for s in out], ["subcommand", "folder", "file"])
        self.assertEqual(out[1].insert, "Downloads/")

    def test_unrelated_folder_suggestions_do_not_replace_executable(self):
        options = (Completion("reports", "reports", "folder"),
                   Completion("tests", "tests", "folder"),
                   Completion("python3.15", "python3.15", "command"))
        self.assertEqual([x.name for x in matching_completions("python3", options)],
                         ["python3.15"])
        self.assertEqual(matching_completions("python3", options[:2]), ())

    def test_arguments_appear_after_space_and_match_path_prefix(self):
        options = (Completion("Downloads", "Downloads/", "folder"),
                   Completion("Documents", "Documents/", "folder"))
        self.assertEqual(matching_completions("cd ", options), options)
        self.assertEqual([x.name for x in matching_completions("cd ~/Do", options)],
                         ["Documents"])
        self.assertEqual([x.name for x in matching_completions("cd ~/Down", options)],
                         ["Downloads"])

    def test_matching_commands_remain_available_without_trailing_space(self):
        options = (Completion("git", "git", "command"),
                   Completion("git-flow", "git-flow", "command"),
                   Completion("reports", "reports", "folder"))
        self.assertEqual([x.name for x in matching_completions("gi", options)],
                         ["git", "git-flow"])

    def test_malformed_untrusted_response_cannot_crash(self):
        self.assertEqual(parse_completions("invalid"), ())
        self.assertEqual(parse_completions('{"suggestions":null}'), ())
        self.assertEqual(parse_completions('{"suggestions":[1,{},null]}'), ())

    def test_text_is_sanitized_and_bounded(self):
        item = {"name": "x\n\x1b[2J" + "a" * 500,
                "insertValue": "x\x00\n" + "a" * 500,
                "type": "option", "description": "\x1b[31mHello\x1b[0m"}
        suggestion = parse_completions(json.dumps({"suggestions": [item]}))[0]
        self.assertTrue(len(suggestion.name) <= 120)
        self.assertTrue(len(suggestion.insert) <= 200)
        self.assertFalse(any(ord(ch) < 32 for ch in suggestion.insert))
        self.assertEqual(suggestion.description, "Hello")

    def test_replacement_only_edits_current_unfinished_token(self):
        s = Completion("git", "git", "command")
        self.assertEqual(replace_completion("gi", s), "git")
        self.assertEqual(replace_completion("echo gi", s), "echo git")
        self.assertEqual(replace_completion("cd ", Completion("foo", "foo/", "folder")), "cd foo/")

    def test_sensitive_commands_are_not_sent_to_completer(self):
        self.assertFalse(safe_to_complete("export API_KEY=123"))
        self.assertFalse(safe_to_complete("password: abc"))
        self.assertFalse(safe_to_complete("hello\nrm -rf /"))
        self.assertFalse(safe_to_complete(" "))
        self.assertTrue(safe_to_complete("git status"))

    def test_command_is_explicit_argv_and_never_shell(self):
        fake = subprocess.CompletedProcess(["is", "complete", "gi"], 0,
                                           '{"suggestions":[{"name":"git"}]}', "")
        with patch("terminal_ai.completion.subprocess.run", return_value=fake) as run:
            worker = CompletionWorker(binary="/opt/homebrew/bin/is")
            try:
                worker.request("gi", Path("/tmp"))
                worker._future.result(timeout=3)
                out = worker.collect("gi", Path("/tmp"))
                self.assertEqual(out[0].name, "git")
                run.assert_called_once()
                args, kwargs = run.call_args
                self.assertEqual(args[0], ["/opt/homebrew/bin/is", "complete", "gi"])
                self.assertFalse(kwargs.get("shell", False))
                self.assertEqual(kwargs["timeout"], 2)
            finally:
                worker.close()

    def test_stale_results_are_discarded(self):
        fake = subprocess.CompletedProcess(["is"], 0,
                                           '{"suggestions":[{"name":"git"}]}', "")
        with patch("terminal_ai.completion.subprocess.run", return_value=fake):
            worker = CompletionWorker(binary="/opt/homebrew/bin/is")
            try:
                worker.request("gi", Path("/tmp"))
                worker._future.result(timeout=3)
                self.assertIsNone(worker.collect("gith", Path("/tmp")))
            finally:
                worker.close()


class CompletionPopupTests(unittest.TestCase):
    def setUp(self):
        cfg = load_config(Path("/tmp/terminal-ai-completion-test-config-missing"))
        self.app = App(cfg)
        self.app.hostname = "MacBook-Air-Fabiian"
        self.app.cwd = Path.home()

    def tearDown(self):
        self.app.stop()

    def test_color_follows_prompt_and_no_emoji(self):
        self.app.line = "gi"
        self.app.suggestions = (Completion("git", "git", "command"),
                                Completion("git-flow", "git-flow", "subcommand"),
                                Completion("Downloads", "Downloads/", "folder"))
        screen = FakeWindow(24, 100)
        with patch("terminal_ai.ui.curses.color_pair", side_effect=lambda v: v * 256):
            self.app.render(screen)
        content = "".join(line for _, _, line, _ in screen.writes)
        self.assertIn("╭", content)
        self.assertIn("╯", content)
        self.assertIn("▱ Downloads", content)
        self.assertNotIn("📦", content)
        self.assertNotIn("📁", content)
        border = next(item for item in screen.writes if "╭" in item[2])
        self.assertEqual(border[3], 256)
        selected = next(item for item in screen.writes if "› git" in item[2])
        self.assertEqual(selected[3], 256 | curses.A_REVERSE)

    def test_arrows_select_and_tab_inserts_without_executing(self):
        self.app.line = "gi"
        self.app.suggestions = (Completion("git", "git", "command"),
                                Completion("git-flow", "git-flow", "subcommand"))
        with patch.object(self.app, "start") as start:
            self.app.key(curses.KEY_DOWN, 24, 100)
            self.assertEqual(self.app.suggestion_index, 1)
            self.app.key("\t", 24, 100)
            self.assertEqual(self.app.line, "git-flow")
            self.assertEqual(self.app.suggestions, ())
            self.assertFalse(self.app.pending)
            start.assert_not_called()

    def test_popup_does_not_obscure_prompt_when_short_window(self):
        self.app.line = "git "
        self.app.suggestions = (Completion("status", "status", "subcommand"),)
        screen = FakeWindow(9, 45)
        with patch("terminal_ai.ui.curses.color_pair", return_value=0):
            self.app.render(screen)
        self.assertTrue(any("MacBook-Air-Fabiian" in line
                            for _, _, line, _ in screen.writes))
        self.assertTrue(all(0 <= row < 9 for row, _, _, _ in screen.writes))


if __name__ == "__main__":
    unittest.main()
