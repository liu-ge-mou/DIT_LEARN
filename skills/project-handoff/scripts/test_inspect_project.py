"""Isolated integration checks; no network, credentials, or existing repos used."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("inspect_project.py")


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="handoff-test-")
        self.root = Path(self.tmp.name) / "repo with spaces"
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        result = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True, encoding="utf-8", errors="replace", check=True,
        )
        return result.stdout.strip()

    def initialize(self, commit=True):
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Isolated Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        if commit:
            (self.root / "file.txt").write_text("base\n", encoding="utf-8")
            self.git("add", "file.txt")
            self.git("-c", "core.hooksPath=", "commit", "-m", "base")

    def inspect(self, path=None):
        self.assertTrue(SCRIPT.is_file(), "Read-only project inspector is not implemented")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--path", str(path or self.root)],
            capture_output=True, encoding="utf-8", errors="replace", check=True,
        )
        return json.loads(result.stdout)

    def test_non_repository(self):
        data = self.inspect()
        self.assertFalse(data["is_repository"])
        self.assertTrue(data["git_available"])
        self.assertFalse((self.root / ".git").exists())

    def test_missing_path(self):
        data = self.inspect(self.root / "missing")
        self.assertEqual(data["error"], "path_not_directory")

    def test_unborn_branch(self):
        self.initialize(commit=False)
        data = self.inspect()
        self.assertEqual(data["branch"], "main")
        self.assertIsNone(data["head"])
        self.assertIsNone(data["upstream"])

    def test_clean_snapshot_does_not_change_index(self):
        self.initialize()
        index = self.root / ".git" / "index"
        before = hashlib.sha256(index.read_bytes()).hexdigest()
        data = self.inspect()
        self.assertEqual(data["changes"]["total"], 0)
        self.assertEqual(data["snapshot_scope"], "local_cached_refs_no_fetch")
        self.assertEqual(hashlib.sha256(index.read_bytes()).hexdigest(), before)
        self.assertFalse((self.root / ".git" / "index.lock").exists())

    def test_staged_unstaged_untracked_and_rename(self):
        self.initialize()
        self.git("mv", "file.txt", "renamed file.txt")
        (self.root / "renamed file.txt").write_text("changed\n", encoding="utf-8")
        (self.root / "交接记录.md").write_text("local\n", encoding="utf-8")
        data = self.inspect()
        self.assertEqual(data["changes"], {
            "total": 2, "staged": 1, "unstaged": 1, "untracked": 1, "conflicts": 0,
        })

    def test_only_unstaged_change_preserves_status_columns(self):
        self.initialize()
        (self.root / "file.txt").write_text("changed\n", encoding="utf-8")
        data = self.inspect()
        self.assertEqual(data["changes"]["staged"], 0)
        self.assertEqual(data["changes"]["unstaged"], 1)

    def test_local_upstream_counts_and_remote_url_redaction(self):
        self.initialize()
        self.git("remote", "add", "origin", "https://user:DO_NOT_EXPOSE@example.invalid/repo")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("branch", "--set-upstream-to=origin/main", "main")
        (self.root / "file.txt").write_text("second\n", encoding="utf-8")
        self.git("add", "file.txt")
        self.git("-c", "core.hooksPath=", "commit", "-m", "second")
        data = self.inspect()
        self.assertEqual(data["ahead"], 1)
        self.assertEqual(data["behind"], 0)
        self.assertEqual(data["remotes"], ["origin"])
        self.assertNotIn("DO_NOT_EXPOSE", json.dumps(data))
        self.assertNotIn("example.invalid", json.dumps(data))

    def test_detached_head(self):
        self.initialize()
        self.git("checkout", "--detach")
        data = self.inspect()
        self.assertTrue(data["detached_head"])
        self.assertIsNone(data["branch"])
        self.assertIsNotNone(data["head"])

    def test_linked_worktree_and_existing_project_markers(self):
        self.initialize()
        linked = Path(self.tmp.name) / "linked"
        self.git("-c", "core.hooksPath=", "worktree", "add", "-b", "feature", str(linked))
        (linked / "AGENTS.md").write_text("existing rules", encoding="utf-8")
        (linked / "uv.lock").write_text("version = 1", encoding="utf-8")
        (linked / "docs").mkdir()
        (linked / "docs" / "progress.md").write_text("existing state", encoding="utf-8")
        data = self.inspect(linked)
        self.assertTrue(data["is_linked_worktree"])
        self.assertIn("AGENTS.md", data["instruction_files"])
        self.assertIn("uv.lock", data["dependency_markers"])
        self.assertIn("docs/progress.md", data["handoff_candidates"])

    def test_conflicted_merge(self):
        self.initialize()
        self.git("checkout", "-b", "other")
        (self.root / "file.txt").write_text("other\n", encoding="utf-8")
        self.git("add", "file.txt")
        self.git("-c", "core.hooksPath=", "commit", "-m", "other")
        self.git("checkout", "main")
        (self.root / "file.txt").write_text("main\n", encoding="utf-8")
        self.git("add", "file.txt")
        self.git("-c", "core.hooksPath=", "commit", "-m", "main")
        result = subprocess.run(["git", "-C", str(self.root), "merge", "other"], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        data = self.inspect()
        self.assertEqual(data["changes"]["conflicts"], 1)
        self.assertIn("merge", data["in_progress"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
