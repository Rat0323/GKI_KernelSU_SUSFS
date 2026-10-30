import unittest
from pathlib import Path


class ContextPatchTests(unittest.TestCase):
    def test_adaptation_never_changes_added_or_removed_c_code(self):
        patch = Path(__file__).resolve().parents[1] / "patches/susfs-6138-context.patch"
        for line in patch.read_text().splitlines():
            if line.startswith(("+++", "---")):
                self.assertIn("kernel_patches/50_add_susfs_in_gki-android14-6.1.patch", line)
            elif line.startswith(("+", "-")):
                self.assertTrue(line[1:].startswith((" ", "@@")),
                                f"adaptation changes functional patch content: {line}")


if __name__ == "__main__":
    unittest.main()
