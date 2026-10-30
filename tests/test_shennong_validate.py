import importlib.util
import struct
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / ".github/workflows/scripts/shennong_validate.py"
SPEC = importlib.util.spec_from_file_location("shennong_validate", SCRIPT)
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


def image(size=4096, target=128, flags=0xA, memory_size=None):
    data = bytearray(size)
    struct.pack_into("<I", data, 4, 0x14000000 | ((target - 4) // 4))
    struct.pack_into("<Q", data, 16, size if memory_size is None else memory_size)
    struct.pack_into("<Q", data, 24, flags)
    data[56:60] = b"ARM\x64"
    return bytes(data)


CONFIG = "\n".join([
    "CONFIG_ARM64=y", "CONFIG_64BIT=y", "CONFIG_ARM64_4K_PAGES=y",
    "CONFIG_KSU=y", "CONFIG_KSU_SUSFS=y", "CONFIG_KPM=y",
])


class ImageTests(unittest.TestCase):
    def test_valid_4k_entry(self):
        result = validator.validate_image(image())
        self.assertEqual(result["branch_target"], 128)
        self.assertEqual(result["page_size"], 4096)

    def test_memory_size_can_exceed_file_size(self):
        validator.validate_image(image(memory_size=8192))

    def test_magic_required(self):
        data = bytearray(image())
        data[56] = 0
        with self.assertRaises(ValueError):
            validator.validate_image(bytes(data))

    def test_non_4k_rejected(self):
        with self.assertRaises(ValueError):
            validator.validate_image(image(flags=0xC))

    def test_truncated_rejected(self):
        with self.assertRaises(ValueError):
            validator.validate_image(b"ARM\x64")

    def test_historical_kpm_entry_beyond_eof(self):
        data = bytearray(image(size=36941968))
        struct.pack_into("<I", data, 4, 0x148CEFFF)
        with self.assertRaisesRegex(ValueError, "entry"):
            validator.validate_image(bytes(data))

    def test_non_branch_rejected(self):
        data = bytearray(image())
        struct.pack_into("<I", data, 4, 0)
        with self.assertRaises(ValueError):
            validator.validate_image(bytes(data))


class ConfigTests(unittest.TestCase):
    def test_final_features(self):
        validator.validate_config(CONFIG)

    def test_base_without_kpm(self):
        validator.validate_config(CONFIG.replace("CONFIG_KPM=y", "# CONFIG_KPM is not set"),
                                  require_kpm=False)

    def test_lkm_root_rejected(self):
        with self.assertRaises(ValueError):
            validator.validate_config(CONFIG.replace("CONFIG_KSU=y", "CONFIG_KSU=m"))

    def test_missing_susfs_rejected(self):
        with self.assertRaises(ValueError):
            validator.validate_config(CONFIG.replace("CONFIG_KSU_SUSFS=y", ""))

    def test_final_kpm_required(self):
        with self.assertRaises(ValueError):
            validator.validate_config(CONFIG.replace("CONFIG_KPM=y", ""))

    def test_conflicting_config_rejected(self):
        with self.assertRaises(ValueError):
            validator.validate_config(CONFIG + "\nCONFIG_KSU=m")


if __name__ == "__main__":
    unittest.main()
