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
from shennong_validate import validate_config, validate_image


STOCK_SHA = "f75f52b48aa1f955a26e8c9cd7ff072a4a7ac91dcb54b6f82b809a287997c861"
PINS = {
    "mkbootimg": "615a1fced5f8e2755642d30357674d3bfa21b85e",
    "avb": "c5066a96caa7bf4150c0a8cc8cc14ab81733fdc7",
    "anykernel": "41f04bdb4ee30a44e429c2c1131d5e8bb32b15f1",
}


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
    return report


def create_anykernel(image_path, repo, output):
    members = ["LICENSE", "META-INF/com/google/android/update-binary",
               "META-INF/com/google/android/updater-script",
               "tools/ak3-core.sh", "tools/busybox", "tools/magiskboot"]
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in members:
            content = run(["git", "show", f"HEAD:{name}"], repo, capture_output=True).stdout
            entry = zipfile.ZipInfo(name)
            entry.create_system = 3
            entry.external_attr = (0o100755 if name != "LICENSE" else 0o100644) << 16
            archive.writestr(entry, content, compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr("anykernel.sh", (ROOT / "packaging/anykernel.sh").read_text())
        archive.write(image_path, "Image")
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None or archive.read("Image") != image_path.read_bytes():
            raise ValueError("AnyKernel zip round-trip mismatch")


def package(args):
    for name in PINS:
        verify_repository(getattr(args, name), name)
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
    boot = out / f"{args.name}-UNSIGNED-EXPERIMENTAL-boot.img"
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
    archive = out / f"{args.name}-EXPERIMENTAL-AnyKernel3.zip"
    create_anykernel(args.image, args.anykernel, archive)
    report.update({
        "stock_sha256": STOCK_SHA, "stock_layout": layout,
        "candidate_layout": packed_layout, "tool_source_pins": PINS,
        "authentication": "unsigned AVB NONE; original OEM/GKI signatures not copied",
        "runtime": "NOT TESTED; separate flash/root-migration approval required",
        "anykernel_target": "active-slot boot only; requires verified stock boot",
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
    for name in ["stock-boot", "image", "mkbootimg", "avb", "anykernel", "out"]:
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--name", required=True, choices=["sukisu-susfs-base", "sukisu-susfs-kpm"])
    package(parser.parse_args())


if __name__ == "__main__":
    main()
