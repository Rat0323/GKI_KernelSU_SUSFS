"""Offline, kernel-only packaging. Never execute installers or access a device."""

import argparse
import gzip
import hashlib
import importlib.util
import json
import struct
import sys
import zipfile
from pathlib import Path

from shennong_build import ROOT, run
from shennong_validate import validate_config, validate_image, validate_kpm, validate_protected_exports


STOCK_SHA = "f75f52b48aa1f955a26e8c9cd7ff072a4a7ac91dcb54b6f82b809a287997c861"
PINS = {
    "mkbootimg": "615a1fced5f8e2755642d30357674d3bfa21b85e",
    "avb": "c5066a96caa7bf4150c0a8cc8cc14ab81733fdc7",
    "anykernel": "41f04bdb4ee30a44e429c2c1131d5e8bb32b15f1",
}
INSTALLER_SOURCES = {
    "tools/busybox": {
        "archive": "UPDATE-Busybox.Installer.v1.36.1-ALL-signed.zip",
        "archive_sha256": "d6b0baeb4f224e994113d29c2d47e6df2413b6cd65fd8b88c2b7d53e0db26c09",
        "member": "busybox-arm64",
        "sha256": "c629fce4b0dd3ba9775f851d0941e74582115f423258d3a79800f2bd11d30f5c",
        "release": "https://github.com/Magisk-Modules-Repo/busybox-ndk/releases/tag/13614",
        "revision": "ad6700620120e3c475df05b0f8bfa7a40e741ee0",
    },
    "tools/magiskboot": {
        "archive": "Magisk-v31.1.apk",
        "archive_sha256": "89cefac0a318afa39fb3880c45c87b036c05bc1ebf02a7a715b7f8d7c26ac6e7",
        "member": "lib/arm64-v8a/libmagiskboot.so",
        "sha256": "97e9ec75731103d31c9e1effca7a50c6444309ce8f78c2ec27e9fe721da4bb54",
        "release": "https://github.com/topjohnwu/Magisk/releases/tag/v31.1",
        "revision": "0972f67d97f9b6302b67c5171283330c74114b4a",
    },
}


def validate_installer_binary(data, name):
    if len(data) < 64 or data[:7] != b"\x7fELF\x02\x01\x01":
        raise ValueError(f"{name}: native little-endian ARM64 ELF64 required")
    kind, machine, version, entry, phoff, _, _, ehsize, phsize, count, _, _, _ = (
        struct.unpack_from("<HHIQQQIHHHHHH", data, 16))
    if machine != 183 or kind != 2 or version != 1:
        raise ValueError(f"{name}: native ARM64 executable required")
    if ehsize != 64 or phsize != 56 or count == 0 or phoff + count * phsize > len(data):
        raise ValueError(f"{name}: invalid ELF program headers")
    executable_entry = False
    for index in range(count):
        segment, flags, offset, address, _, size, memory, _ = struct.unpack_from(
            "<IIQQQQQQ", data, phoff + index * phsize)
        if segment in {2, 3}:
            raise ValueError(f"{name}: dynamic dependencies/interpreter not permitted")
        if segment == 1:
            if size > memory or offset + size > len(data):
                raise ValueError(f"{name}: ELF load segment outside file")
            executable_entry |= bool(flags & 1 and address <= entry < address + size)
    if not executable_entry:
        raise ValueError(f"{name}: entry is not in file-backed executable code")
    return {"architecture": "AArch64", "elf_class": 64, "static": True,
            "sha256": hashlib.sha256(data).hexdigest()}


def load_installer_tools(assets):
    tools = {}
    for name, source in INSTALLER_SOURCES.items():
        archive_path = assets / source["archive"]
        with archive_path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != source["archive_sha256"]:
            raise ValueError(f"{name}: installer archive hash mismatch")
        with zipfile.ZipFile(archive_path) as archive:
            content = archive.read(source["member"])
        if hashlib.sha256(content).hexdigest() != source["sha256"]:
            raise ValueError(f"{name}: installer member hash mismatch")
        validate_installer_binary(content, name)
        tools[name] = content
    return tools


def inspect_boot(data):
    if len(data) < 4096 or data[:8] != b"ANDROID!":
        raise ValueError("invalid Android boot header")
    kernel, ramdisk, os_version, header_size = struct.unpack_from("<4I", data, 8)
    version = struct.unpack_from("<I", data, 40)[0]
    signature = struct.unpack_from("<I", data, 1580)[0]
    if version != 4 or header_size != 1584 or ramdisk or signature or os_version:
        raise ValueError("expected the verified v4 kernel-only boot layout")
    if any(data[24:40]) or any(data[44:4096]):
        raise ValueError("unexpected stock reserved, cmdline or header padding")
    kernel_end = (4096 + kernel + 4095) // 4096 * 4096
    if kernel == 0 or kernel_end > len(data) - 64:
        raise ValueError("boot kernel outside file")
    magic, major, minor, original, offset, size = struct.unpack_from(">4sIIQQQ", data, len(data) - 64)
    if magic != b"AVBf" or (major, minor) != (1, 0):
        raise ValueError("expected AVB 1.0 footer")
    if original < kernel_end or offset < original or size < 256 or offset + size > len(data) - 64:
        raise ValueError("AVB footer ranges outside image")
    if data[offset:offset + 4] != b"AVB0":
        raise ValueError("missing AVB metadata header")
    return {
        "kernel_size": kernel, "header_version": version,
        "partition_size": len(data), "original_image_size": original,
        "kernel_padded_end": kernel_end, "vbmeta_offset": offset,
        "vbmeta_size": size, "extra_after_kernel": original - kernel_end,
    }


def verify_repository(path, name):
    actual = run(["git", "rev-parse", "HEAD"], path,
                 capture_output=True, text=True).stdout.strip()
    if actual != PINS[name]:
        raise ValueError(f"packaging tool source mismatch: {name}")
    changes = run(["git", "status", "--porcelain", "--untracked-files=normal"], path,
                  capture_output=True, text=True).stdout
    if changes.strip():
        raise ValueError(f"packaging tool source is modified: {name}")


def load_avb(path):
    spec = importlib.util.spec_from_file_location("pinned_avbtool", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_candidate(path, name):
    if name not in {"sukisu-susfs-base", "sukisu-susfs-kpm"}:
        raise ValueError("unknown candidate variant")
    kpm = name == "sukisu-susfs-kpm"
    config = validate_config((path.parent / ".config").read_text(), kpm)
    data = path.read_bytes()
    report = validate_image(data)
    begin, end = data.find(b"IKCFG_ST"), data.find(b"IKCFG_ED")
    if begin < 0 or end < begin:
        raise ValueError("candidate lacks actual embedded config")
    if validate_config(gzip.decompress(data[begin + 8:end]).decode(), kpm) != config:
        raise ValueError("candidate embedded/resolved config mismatch")
    metadata = path.parent / ("kpm-validation.json" if kpm else "validation.json")
    if json.loads(metadata.read_text())["sha256"] != report["sha256"]:
        raise ValueError("candidate hash differs from validated CI artifact")
    report["module_exports"] = validate_protected_exports(
        (path.parent / "gki_module_protected_exports.h").read_text())
    build_report = json.loads((path.parent / "validation.json").read_text())
    if report["module_exports"] != build_report.get("module_exports"):
        raise ValueError("generated protected exports header differs from build evidence")
    if kpm:
        original = (path.parent / "Image").read_bytes()
        original_sha = json.loads((path.parent / "validation.json").read_text())["sha256"]
        if hashlib.sha256(original).hexdigest() != original_sha:
            raise ValueError("unpatched KPM variant hash mismatch")
        report["kpm"] = validate_kpm(data, original, (path.parent / "kpimg").read_bytes())
    return report


def create_anykernel(boot_path, repo, output, tools=None):
    if tools is None or set(tools) != {"tools/busybox", "tools/magiskboot"}:
        raise ValueError("explicit verified ARM64 installer tools required")
    for name, content in tools.items():
        validate_installer_binary(content, name)
    boot = boot_path.read_bytes()
    inspect_boot(boot)
    installer = (ROOT / "packaging/anykernel.sh").read_text()
    for placeholder, value in {"@BOOT_SHA256@": hashlib.sha256(boot).hexdigest(),
                               "@PARTITION_SIZE@": str(len(boot))}.items():
        if installer.count(placeholder) != 1:
            raise ValueError(f"installer must contain exactly one {placeholder}")
        installer = installer.replace(placeholder, value)
    members = ["LICENSE", "META-INF/com/google/android/update-binary",
               "META-INF/com/google/android/updater-script",
               "tools/ak3-core.sh", "tools/busybox", "tools/magiskboot"]
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in members:
            content = (tools[name] if name in tools else
                       run(["git", "show", f"HEAD:{name}"], repo, capture_output=True).stdout)
            entry = zipfile.ZipInfo(name)
            entry.create_system = 3
            entry.external_attr = (0o100755 if name != "LICENSE" else 0o100644) << 16
            archive.writestr(entry, content, compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr("anykernel.sh", installer)
        archive.writestr("tools/shennong-flash.sh", (ROOT / "packaging/shennong-flash.sh").read_text())
        archive.write(boot_path, "candidate-boot.img")
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None or archive.read("candidate-boot.img") != boot:
            raise ValueError("AnyKernel zip round-trip mismatch")
        if archive.read("anykernel.sh").decode() != installer:
            raise ValueError("AnyKernel installer round-trip mismatch")
        for name, content in tools.items():
            if archive.read(name) != content:
                raise ValueError(f"{name}: AnyKernel tool round-trip mismatch")


def boot_output_paths(output, name):
    # AVB resolves the hash descriptor's partition name as a sibling filename.
    return output / "boot.img", output / f"{name}-UNSIGNED-EXPERIMENTAL-boot.img"


def package(args):
    for name in PINS:
        verify_repository(getattr(args, name), name)
    tools = load_installer_tools(args.installer_assets)
    stock = args.stock_boot.read_bytes()
    if hashlib.sha256(stock).hexdigest() != STOCK_SHA:
        raise ValueError("stock boot hash does not match the verified ROM backup")
    layout = inspect_boot(stock)
    image = args.image.read_bytes()
    report = verify_candidate(args.image, args.name)
    out = args.out.resolve()
    if out.exists():
        raise ValueError("use a new output directory; do not overwrite candidates")
    out.mkdir(parents=True)
    avb = load_avb(args.avb / "avbtool.py")
    _, header, descriptors, _ = avb.Avb()._parse_image(
        avb.ImageHandler(str(args.stock_boot), read_only=True))
    hashes = [desc for desc in descriptors if isinstance(desc, avb.AvbHashDescriptor)]
    if len(hashes) != 1 or hashes[0].partition_name != "boot" or hashes[0].flags != 0:
        raise ValueError("unexpected stock AVB hash descriptor")
    properties = [desc for desc in descriptors if isinstance(desc, avb.AvbPropertyDescriptor)]
    if len(hashes) + len(properties) != len(descriptors):
        raise ValueError("unknown stock AVB descriptor type")
    boot, final_boot = boot_output_paths(out, args.name)
    run([sys.executable, args.mkbootimg / "mkbootimg.py", "--header_version", "4",
         "--kernel", args.image, "--output", boot])
    generated = boot.read_bytes()
    expected_header = bytearray(stock[:4096])
    struct.pack_into("<I", expected_header, 8, len(image))
    if generated[:4096] != expected_header:
        raise ValueError("generated boot header differs beyond kernel size")
    footer = [sys.executable, args.avb / "avbtool.py", "add_hash_footer",
              "--image", boot, "--partition_size", layout["partition_size"],
              "--partition_name", "boot", "--algorithm", "NONE",
              "--hash_algorithm", hashes[0].hash_algorithm,
              "--salt", hashes[0].salt.hex(),
              "--rollback_index", header.rollback_index, "--flags", header.flags]
    for prop in properties:
        footer.extend(["--prop", f"{prop.key}:{prop.value.decode()}"])
    run(footer)
    # This verifies the recomputed digest, not an OEM signature or bootloader acceptance.
    run([sys.executable, args.avb / "avbtool.py", "verify_image", "--image", boot])
    packed = boot.read_bytes()
    packed_layout = inspect_boot(packed)
    if packed_layout["partition_size"] != layout["partition_size"]:
        raise ValueError("candidate boot partition size changed")
    if packed[4096:4096 + len(image)] != image:
        raise ValueError("boot kernel round-trip mismatch")
    boot.rename(final_boot)
    archive = out / f"{args.name}-EXPERIMENTAL-AnyKernel3.zip"
    create_anykernel(final_boot, args.anykernel, archive, tools)
    report.update({
        "stock_sha256": STOCK_SHA, "stock_layout": layout,
        "candidate_layout": packed_layout, "tool_source_pins": PINS,
        "authentication": "unsigned AVB NONE; original OEM/GKI signatures not copied",
        "runtime": "NOT TESTED; separate flash/root-migration approval required",
        "anykernel_target": "active-slot boot only; requires verified stock boot",
        "anykernel_runtime": "host-tested direct canonical boot write with readback; device execution NOT TESTED",
        "anykernel_payload": "candidate-boot.img equals canonical boot byte-for-byte; no on-device repacking",
        "installer_sources": INSTALLER_SOURCES,
        "installer_binaries": {name: validate_installer_binary(data, name)
                               for name, data in tools.items()},
    })
    (out / "packaging-validation.json").write_text(json.dumps(report, indent=2) + "\n")
    entries = []
    for path in sorted(out.iterdir()):
        if path.is_file():
            with path.open("rb") as stream:
                entries.append(f"{hashlib.file_digest(stream, 'sha256').hexdigest()}  {path.name}")
    (out / "SHA256SUMS").write_text("\n".join(entries) + "\n")


def main():
    parser = argparse.ArgumentParser()
    for name in ["stock-boot", "image", "mkbootimg", "avb", "anykernel", "installer-assets", "out"]:
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--name", required=True, choices=["sukisu-susfs-base", "sukisu-susfs-kpm"])
    package(parser.parse_args())


if __name__ == "__main__":
    main()
