import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / ".github/workflows/scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("shennong_build", SCRIPTS / "shennong_build.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class PipelineTests(unittest.TestCase):
    def test_command_failure_is_fatal(self):
        with patch.object(builder.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["git"])):
            with self.assertRaises(subprocess.CalledProcessError):
                builder.run(["git", "fetch"])

    def test_commands_use_check_true(self):
        with patch.object(builder.subprocess, "run") as command:
            builder.run(["patch", "--dry-run"])
            self.assertTrue(command.call_args.kwargs["check"])

    def test_missing_output_is_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                builder.require_outputs(Path(directory), ["Image", "vmlinux"])

    def test_empty_output_is_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "Image").touch()
            with self.assertRaises(ValueError):
                builder.require_outputs(Path(directory), ["Image"])

    def test_source_edit_requires_exact_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Makefile"
            path.write_text("different\n")
            with self.assertRaises(ValueError):
                builder.replace_once(path, "expected", "changed")
            self.assertEqual(path.read_text(), "different\n")

    def test_duplicate_anchor_is_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Makefile"
            path.write_text("expected expected")
            with self.assertRaises(ValueError):
                builder.replace_once(path, "expected", "changed")


if __name__ == "__main__":
    unittest.main()
