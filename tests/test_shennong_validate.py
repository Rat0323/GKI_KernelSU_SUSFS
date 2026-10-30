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


class ProtectedExportsTests(unittest.TestCase):
    HEADER = """#define NR_PROTECTED_EXPORTS_SYMBOLS \\
    (ARRAY_SIZE(gki_protected_exports_symbols))
#define MAX_PROTECTED_EXPORTS_NAME_LEN (1)
static const char gki_protected_exports_symbols[][MAX_PROTECTED_EXPORTS_NAME_LEN] = {
};
"""

    def test_empty_generated_table_accepted(self):
        self.assertEqual(validator.validate_protected_exports(self.HEADER)["symbol_count"], 0)

    def test_actual_boot_failure_symbol_rejected(self):
        header = self.HEADER.replace("(1)", "(13)").replace("{\n};", '{\n"rfkill_alloc",\n};')
        with self.assertRaisesRegex(ValueError, "protected exports"):
            validator.validate_protected_exports(header)

    def test_missing_generated_table_rejected(self):
        with self.assertRaisesRegex(ValueError, "protected exports"):
            validator.validate_protected_exports("")


def kpm_fixture():
    original = image(size=8192, memory_size=16384)
    payload = bytearray(8192)
    payload[:8] = b"KP1158\0\0"
    struct.pack_into("<Q", payload, 16, 2)
    payload[4096:4100] = b"\x1f\x20\x03\xd5"
    data = bytearray(original) + payload + bytearray(128)
    struct.pack_into("<I", data, 4, 0x14000000 | ((8192 + 4096 - 4) // 4))
    struct.pack_into("<Q", data, 16, 32768)
    struct.pack_into("<7q", data, 8192 + 64 + 8,
                     len(original), len(payload), 16384, 12, 8192, 20480, 128)
    data[8192 + 224:8192 + 232] = original[:8]
    return bytes(data), original, bytes(payload)


class KpmLayoutTests(unittest.TestCase):
    def test_payload_and_memory_copy_range_match(self):
        data, original, payload = kpm_fixture()
        report = validator.validate_kpm(data, original, payload)
        self.assertEqual(report["payload_offset"], 8192)
        self.assertEqual(report["copy_start"], 20480)

    def test_missing_payload_rejected(self):
        data, original, payload = kpm_fixture()
        modified = bytearray(data)
        modified[8192] = 0
        with self.assertRaisesRegex(ValueError, "payload"):
            validator.validate_kpm(bytes(modified), original, payload)

    def test_modified_payload_code_rejected(self):
        data, original, payload = kpm_fixture()
        modified = bytearray(data)
        modified[8192 + 4096] ^= 1
        with self.assertRaisesRegex(ValueError, "payload"):
            validator.validate_kpm(bytes(modified), original, payload)

    def test_entry_must_target_payload_setup(self):
        data, original, payload = kpm_fixture()
        modified = bytearray(data)
        struct.pack_into("<I", modified, 4, 0x1400001F)
        with self.assertRaisesRegex(ValueError, "entry"):
            validator.validate_kpm(bytes(modified), original, payload)

    def test_copy_range_must_fit_declared_memory(self):
        data, original, payload = kpm_fixture()
        modified = bytearray(data)
        struct.pack_into("<Q", modified, 16, 24576)
        with self.assertRaisesRegex(ValueError, "memory"):
            validator.validate_kpm(bytes(modified), original, payload)

    def test_wrong_original_kernel_rejected(self):
        data, original, payload = kpm_fixture()
        modified = bytearray(original)
        struct.pack_into("<Q", modified, 16, 32768)
        with self.assertRaisesRegex(ValueError, "original"):
            validator.validate_kpm(data, bytes(modified), payload)

    def test_unexpected_extra_module_rejected(self):
        data, original, payload = kpm_fixture()
        modified = bytearray(data)
        modified[-128] = 1
        with self.assertRaisesRegex(ValueError, "extra"):
            validator.validate_kpm(bytes(modified), original, payload)


if __name__ == "__main__":
    unittest.main()
