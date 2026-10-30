import importlib.util
import gzip
import hashlib
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / ".github/workflows/scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("shennong_package", SCRIPTS / "shennong_package.py")
packager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packager)


def candidate(directory, kpm=False):
    config = "\n".join([
        "CONFIG_ARM64=y", "CONFIG_64BIT=y", "CONFIG_ARM64_4K_PAGES=y",
        "CONFIG_KSU=y", "CONFIG_KSU_SUSFS=y",
        "CONFIG_KPM=y" if kpm else "# CONFIG_KPM is not set",
    ]) + "\n"
    data = bytearray(4096)
    struct.pack_into("<I", data, 4, 0x1400001F)
    struct.pack_into("<Q", data, 16, len(data))
    struct.pack_into("<Q", data, 24, 0xA)
    data[56:60] = b"ARM\x64"
    embedded = b"IKCFG_ST" + gzip.compress(config.encode()) + b"IKCFG_ED"
    data[512:512 + len(embedded)] = embedded
    (directory / "Image").write_bytes(data)
    (directory / "validation.json").write_text(json.dumps({
        "sha256": hashlib.sha256(data).hexdigest(),
    }))
    if kpm:
        original = bytes(data)
        payload = bytearray(8192)
        payload[:8] = b"KP1158\0\0"
        struct.pack_into("<Q", payload, 16, 2)
        payload[4096:4100] = b"\x1f\x20\x03\xd5"
        (directory / "kpimg").write_bytes(payload)
        data += payload + bytearray(128)
        struct.pack_into("<I", data, 4, 0x14000000 | ((8192 - 4) // 4))
        struct.pack_into("<Q", data, 16, 28672)
        struct.pack_into("<7q", data, 4096 + 64 + 8,
                         len(original), len(payload), 4096, 12, 4096, 16384, 128)
        data[4096 + 224:4096 + 232] = original[:8]
    image = directory / ("Image.kpm" if kpm else "Image")
    image.write_bytes(data)
    (directory / ".config").write_text(config)
    metadata = "kpm-validation.json" if kpm else "validation.json"
    (directory / metadata).write_text(json.dumps({
        "sha256": hashlib.sha256(data).hexdigest(),
    }))
    return image


def boot():
    data = bytearray(12288)
    data[:8] = b"ANDROID!"
    struct.pack_into("<I", data, 8, 4096)
    struct.pack_into("<I", data, 20, 1584)
    struct.pack_into("<I", data, 40, 4)
    data[8192:8196] = b"AVB0"
    struct.pack_into(">4sIIQQQ", data, len(data) - 64, b"AVBf", 1, 0, 8192, 8192, 256)
    return bytes(data)


class CandidateTests(unittest.TestCase):
    def test_base_and_final_round_trip(self):
        for kpm, name in [(False, "sukisu-susfs-base"), (True, "sukisu-susfs-kpm")]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                image = candidate(Path(directory), kpm)
                self.assertEqual(packager.verify_candidate(image, name)["sha256"],
                                 hashlib.sha256(image.read_bytes()).hexdigest())

    def test_unknown_variant_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            image = candidate(Path(directory))
            with self.assertRaisesRegex(ValueError, "variant"):
                packager.verify_candidate(image, "unknown")

    def test_modified_image_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            image = candidate(Path(directory))
            data = bytearray(image.read_bytes())
            data[350] = 1
            image.write_bytes(data)
            with self.assertRaisesRegex(ValueError, "hash"):
                packager.verify_candidate(image, "sukisu-susfs-base")

    def test_embedded_config_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            image = candidate(Path(directory))
            with (Path(directory) / ".config").open("a") as stream:
                stream.write("CONFIG_LOCAL_TEST=y\n")
            with self.assertRaisesRegex(ValueError, "embedded/resolved"):
                packager.verify_candidate(image, "sukisu-susfs-base")

    def test_base_cannot_be_packaged_as_kpm(self):
        with tempfile.TemporaryDirectory() as directory:
            image = candidate(Path(directory))
            with self.assertRaisesRegex(ValueError, "CONFIG_KPM"):
                packager.verify_candidate(image, "sukisu-susfs-kpm")

    def test_kpm_requires_compiled_payload_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            image = candidate(Path(directory), True)
            (Path(directory) / "kpimg").unlink()
            with self.assertRaises(FileNotFoundError):
                packager.verify_candidate(image, "sukisu-susfs-kpm")


class SourceIntegrityTests(unittest.TestCase):
    def test_modified_tool_source_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
            source = repo / "avbtool.py"
            source.write_text("# pinned tool\n")
            subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "-c", "user.name=Regression", "-c",
                            "user.email=regression@example.invalid", "commit", "-m",
                            "Test fixture"], cwd=repo, check=True, capture_output=True)
            revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                      check=True, capture_output=True, text=True).stdout.strip()
            source.write_text("# modified tool\n")
            with patch.dict(packager.PINS, {"avb": revision}):
                with self.assertRaisesRegex(ValueError, "modified"):
                    packager.verify_repository(repo, "avb")


class BootLayoutTests(unittest.TestCase):
    def test_candidate_without_resolved_config_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "Image"
            image.write_bytes(b"not a compiled candidate")
            with self.assertRaises(FileNotFoundError):
                packager.verify_candidate(image, "sukisu-susfs-base")

    def test_verified_v4_layout(self):
        result = packager.inspect_boot(boot())
        self.assertEqual(result["kernel_size"], 4096)
        self.assertEqual(result["header_version"], 4)
        self.assertEqual(result["vbmeta_offset"], 8192)

    def test_old_generic_header_rejected(self):
        data = bytearray(boot())
        struct.pack_into("<I", data, 40, 3)
        with self.assertRaises(ValueError):
            packager.inspect_boot(bytes(data))

    def test_nonempty_ramdisk_rejected(self):
        data = bytearray(boot())
        struct.pack_into("<I", data, 12, 128)
        with self.assertRaises(ValueError):
            packager.inspect_boot(bytes(data))

    def test_missing_footer_rejected(self):
        with self.assertRaises(ValueError):
            packager.inspect_boot(boot()[:-64])

    def test_footer_out_of_bounds_rejected(self):
        data = bytearray(boot())
        struct.pack_into(">Q", data, len(data) - 44, len(data) + 100)
        with self.assertRaises(ValueError):
            packager.inspect_boot(bytes(data))

    def test_kernel_out_of_bounds_rejected(self):
        data = bytearray(boot())
        struct.pack_into("<I", data, 8, 65536)
        with self.assertRaises(ValueError):
            packager.inspect_boot(bytes(data))


if __name__ == "__main__":
    unittest.main()
