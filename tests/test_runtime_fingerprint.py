import subprocess
import unittest
from unittest.mock import patch

from bot.main import _source_fingerprint


class RuntimeFingerprintTests(unittest.TestCase):
    def test_reports_commit_and_clean_state_without_exposing_diff(self):
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout="abc123\n")
        clean = subprocess.CompletedProcess(args=[], returncode=0, stdout="")
        with patch("bot.main.subprocess.run", side_effect=[result, clean]):
            self.assertEqual(_source_fingerprint(), ("abc123", "clean"))

    def test_reports_dirty_state_as_boolean_marker_only(self):
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout="abc123\n")
        dirty = subprocess.CompletedProcess(args=[], returncode=0, stdout=" M .env\n?? secret.txt\n")
        with patch("bot.main.subprocess.run", side_effect=[result, dirty]):
            self.assertEqual(_source_fingerprint(), ("abc123", "dirty"))

    def test_missing_git_returns_unknown_without_startup_failure(self):
        with patch("bot.main.subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(_source_fingerprint(), ("unknown", "unknown"))


if __name__ == "__main__":
    unittest.main()
