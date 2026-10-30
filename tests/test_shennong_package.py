import importlib.util
import gzip
import hashlib
import json
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
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


def installer_elf(machine=183, elf_class=2, segment_type=1):
    data = bytearray(256)
    data[:7] = b"\x7fELF" + bytes([elf_class, 1, 1])
    struct.pack_into("<HHIQQQIHHHHHH", data, 16,
                     2, machine, 1, 0x400080, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    struct.pack_into("<IIQQQQQQ", data, 64,
                     segment_type, 5, 0, 0x400000, 0x400000, len(data), len(data), 4096)
    return bytes(data)


class InstallerArchitectureTests(unittest.TestCase):
    def test_default_arm32_template_cannot_be_packaged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "Image"
            image.write_bytes(b"verified image fixture")
            with patch.object(packager, "run") as command:
                command.return_value.stdout = installer_elf(machine=40, elf_class=1)
                with self.assertRaisesRegex(ValueError, "ARM64"):
                    packager.create_anykernel(image, root, root / "candidate.zip")
            self.assertFalse((root / "candidate.zip").exists())

    def test_native_arm64_static_executable_accepted(self):
        result = packager.validate_installer_binary(installer_elf(), "busybox")
        self.assertEqual(result["architecture"], "AArch64")
        self.assertEqual(result["sha256"], hashlib.sha256(installer_elf()).hexdigest())

    def test_arm32_and_wrong_machine_rejected(self):
        for data in [installer_elf(machine=40, elf_class=1), installer_elf(machine=62)]:
            with self.subTest(header=data[:20]):
                with self.assertRaisesRegex(ValueError, "ARM64"):
                    packager.validate_installer_binary(data, "busybox")

    def test_truncated_or_dynamic_executable_rejected(self):
        for data in [installer_elf()[:20], installer_elf()[:80],
                     installer_elf(segment_type=3), installer_elf(segment_type=2)]:
            with self.subTest(size=len(data), header=data[:20]):
                with self.assertRaises(ValueError):
                    packager.validate_installer_binary(data, "magiskboot")

    def test_arm64_tools_round_trip_in_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "Image"
            image.write_bytes(b"verified image fixture")
            tools = {name: installer_elf() for name in ["tools/busybox", "tools/magiskboot"]}
            with patch.object(packager, "run") as command:
                command.return_value.stdout = b"# pinned script fixture\n"
                packager.create_anykernel(image, root, root / "candidate.zip", tools)
            with zipfile.ZipFile(root / "candidate.zip") as archive:
                for name, expected in tools.items():
                    self.assertEqual(archive.read(name), expected)

    def test_installer_archive_and_member_hashes_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / "tools.zip"
            binary = installer_elf()
            with zipfile.ZipFile(asset, "w") as archive:
                archive.writestr("arm64/tool", binary)
            source = {"archive": asset.name, "member": "arm64/tool",
                      "archive_sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
                      "sha256": hashlib.sha256(binary).hexdigest()}
            with patch.dict(packager.INSTALLER_SOURCES,
                            {"tools/busybox": source}, clear=True):
                self.assertEqual(packager.load_installer_tools(root)["tools/busybox"], binary)
                source["sha256"] = "0" * 64
                with self.assertRaisesRegex(ValueError, "member hash"):
                    packager.load_installer_tools(root)
                source["archive_sha256"] = "0" * 64
                with self.assertRaisesRegex(ValueError, "archive hash"):
                    packager.load_installer_tools(root)


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
    def test_avb_staging_name_matches_boot_partition(self):
        staged, final = packager.boot_output_paths(Path("new-output"), "sukisu-susfs-base")
        self.assertEqual(staged.name, "boot.img")
        self.assertEqual(final.name, "sukisu-susfs-base-UNSIGNED-EXPERIMENTAL-boot.img")
        self.assertEqual(staged.parent, final.parent)

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
