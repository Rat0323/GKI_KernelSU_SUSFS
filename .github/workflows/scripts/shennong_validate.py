"""Independent static gates. Passing these gates is not a bootability claim."""

import hashlib
import re
import struct


def validate_image(data: bytes) -> dict:
    if len(data) < 64 or data[56:60] != b"ARM\x64":
        raise ValueError("missing/truncated ARM64 Image header")
    flags = struct.unpack_from("<Q", data, 24)[0]
    if flags & 1 or (flags >> 1) & 3 != 1:
        raise ValueError("expected little-endian 4K ARM64 Image")
    instruction = struct.unpack_from("<I", data, 4)[0]
    if instruction & 0xFC000000 != 0x14000000:
        raise ValueError("Image entry at +4 is not ARM64 B")
    immediate = instruction & 0x03FFFFFF
    if immediate & 0x02000000:
        immediate -= 1 << 26
    target = 4 + immediate * 4
    if target < 64 or target + 4 > len(data):
        raise ValueError(f"Image entry outside file: {target} / {len(data)}")
    memory_size = struct.unpack_from("<Q", data, 16)[0]
    if memory_size < 64:
        raise ValueError("invalid Image in-memory size")
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "file_size": len(data), "memory_size": memory_size,
        "branch_target": target, "entry_instruction": hex(instruction),
        "page_size": 4096, "flags": hex(flags),
    }


def validate_kpm(data: bytes, original: bytes, payload: bytes) -> dict:
    """Check the pinned ARM64 fork's preset and code without running kptools."""
    report, base = validate_image(data), validate_image(original)
    offset = (len(original) + 4095) // 4096 * 4096
    if len(payload) <= 4096 or payload[:8] != b"KP1158\0\0":
        raise ValueError("missing compiled KPM payload")
    if struct.unpack_from("<Q", payload, 16)[0] != 2:
        raise ValueError("KPM payload must be Android release ARM64")
    if offset + len(payload) + 128 > len(data):
        raise ValueError("KPM payload outside file")
    if data[offset:offset + 64] != payload[:64]:
        raise ValueError("KPM payload header mismatch")
    # kpimg.lds reserves the first 4K for the mutable preset, not code.
    if data[offset + 4096:offset + len(payload)] != payload[4096:]:
        raise ValueError("compiled KPM payload code mismatch")
    if report["branch_target"] != offset + 4096:
        raise ValueError("KPM entry must target the file-backed setup code")
    fields = struct.unpack_from("<7q", data, offset + 64 + 8)
    kimg_size, kpimg_size, kernel_size, page_shift, setup, start, extra = fields
    if (kimg_size, kpimg_size, kernel_size, page_shift, setup) != (
            len(original), len(payload), base["memory_size"], 12, offset):
        raise ValueError("KPM preset differs from original kernel/payload")
    if data[offset + 224:offset + 232] != original[:8]:
        raise ValueError("KPM original entry backup mismatch")
    if extra != 128 or len(data) != offset + len(payload) + extra or any(data[-128:]):
        raise ValueError("unexpected KPM extra module or footer layout")
    if any(data[len(original):offset]):
        raise ValueError("unexpected padding before KPM payload")
    expected_start = max((base["memory_size"] + 4095) // 4096 * 4096,
                         (len(data) + 4095) // 4096 * 4096)
    if start != expected_start:
        raise ValueError("KPM memory copy start overlaps the input image")
    if report["memory_size"] < start + len(payload) + extra:
        raise ValueError("KPM copy exceeds declared Image memory")
    return {
        "payload_offset": offset, "payload_size": len(payload),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "unpatched_sha256": base["sha256"], "copy_start": start,
        "copy_reserved_end": start + len(payload) + extra,
        "preset_contract": "SukiSU KernelPatch 1c5b608916357ac86a94cc736e235db1fac4a29d",
    }


def parse_config(text: str) -> dict:
    values = {}
    for line in text.splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            key, value = line[2:-11], "n"
        else:
            continue
        if key in values and values[key] != value:
            raise ValueError(f"conflicting resolved config: {key}")
        values[key] = value
    return values


def validate_protected_exports(text: str) -> dict:
    empty_table = re.search(
        r"static const char gki_protected_exports_symbols\[\]"
        r"\[MAX_PROTECTED_EXPORTS_NAME_LEN\]\s*=\s*\{\s*\};", text)
    minimum_width = re.search(
        r"^#define MAX_PROTECTED_EXPORTS_NAME_LEN\s+\(1\)\s*$", text, re.M)
    if not empty_table or not minimum_width:
        raise ValueError("generated protected exports table must be empty for stock module compatibility")
    return {"symbol_count": 0, "header_sha256": hashlib.sha256(text.encode()).hexdigest()}


def validate_config(text: str, require_kpm: bool = True) -> dict:
    values = parse_config(text)
    required = ["ARM64", "64BIT", "ARM64_4K_PAGES", "KSU", "KSU_SUSFS"]
    if require_kpm:
        required.append("KPM")
    for name in required:
        if values.get(f"CONFIG_{name}") != "y":
            raise ValueError(f"required built-in CONFIG_{name}=y missing")
    if not require_kpm and values.get("CONFIG_KPM", "n") != "n":
        raise ValueError("base must have CONFIG_KPM disabled")
    return values
