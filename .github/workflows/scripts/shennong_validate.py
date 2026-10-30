"""Independent static gates. Passing these gates is not a bootability claim."""

import hashlib
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
