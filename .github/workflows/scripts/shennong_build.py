"""Pinned shennong experiment; no device access, generic boot images or releases."""

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

from shennong_validate import validate_config, validate_image, validate_protected_exports


ROOT = Path(__file__).resolve().parents[3]
EXPORTS = ["sukisu_compact_find_symbol", "sukisu_super_find_struct",
           "sukisu_super_access", "sukisu_super_container_of"]


def run(args, cwd=None, **kwargs):
    print("+", " ".join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), cwd=cwd, check=True, **kwargs)


def require_outputs(directory, names):
    for name in names:
        path = directory / name
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.stat().st_size == 0:
            raise ValueError(f"empty required output: {path}")


def replace_once(path, old, new):
    text = path.read_text()
    if text.count(old) != 1:
        raise ValueError(f"expected exactly one source anchor in {path}: {old!r}")
    path.write_text(text.replace(old, new))


def checkout(path, url, revision):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("source must be locked to a full commit")
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        run(["git", "init", str(path)])
        run(["git", "remote", "add", "origin", url], path)
        run(["git", "fetch", "--depth=1", "origin", revision], path)
        run(["git", "checkout", "--detach", "FETCH_HEAD"], path)
    actual = run(["git", "rev-parse", "HEAD"], path,
                 capture_output=True, text=True).stdout.strip()
    if actual != revision:
        raise ValueError(f"source revision mismatch: {path}: {actual}")


def prepare(work, out, lock):
    tree = work / "ack"
    checkout(tree, **lock["superproject"])
    for relative, (project, revision) in lock["projects"].items():
        # Verify every support pin against the exact superproject gitlink.
        link = run(["git", "ls-tree", "HEAD", relative], tree,
                   capture_output=True, text=True).stdout.split()
        if len(link) < 3 or link[0] != "160000" or link[2] != revision:
            raise ValueError(f"superproject gitlink mismatch: {relative}")
        checkout(tree / relative, f"https://android.googlesource.com/{project}", revision)
    for name, component in lock["components"].items():
        checkout(work / name, component["url"], component["revision"])
    common = tree / "common"
    for key, expected in [("VERSION", "6"), ("PATCHLEVEL", "1"), ("SUBLEVEL", "138")]:
        if not re.search(rf"^{key} = {expected}$", (common / "Makefile").read_text(), re.M):
            raise ValueError(f"unexpected Linux baseline: {key}")
    if "KMI_GENERATION=11" not in (common / "build.config.common").read_text():
        raise ValueError("unexpected KMI generation")
    root = common / "drivers/kernelsu"
    if root.exists():
        raise ValueError("refusing duplicate SukiSU integration")
    shutil.copytree(work / "sukisu/kernel", root)
    makefile = root / "Makefile"
    text = makefile.read_text()
    begin = text.index("REPO_OWNER  :=")
    end = text.index("ccflags-y += -DKSU_VERSION=")
    component = lock["components"]["sukisu"]
    text = text[:begin] + (
        f"KSU_VERSION := {component['version_code']}\n"
        f"KSU_VERSION_FULL := {component['version_label']}\n"
        "REPO_NAME := SukiSU-Ultra\n"
        "$(info -- Locked SukiSU: $(KSU_VERSION) [$(KSU_VERSION_FULL)])\n\n"
    ) + text[end:]
    makefile.write_text(text)
    if "curl" in text or "git fetch" in text:
        raise ValueError("moving network metadata remains in SukiSU Makefile")
    with (common / "drivers/Makefile").open("a") as stream:
        stream.write("\nobj-$(CONFIG_KSU) += kernelsu/\n")
    with (common / "drivers/Kconfig").open("a") as stream:
        stream.write('\nsource "drivers/kernelsu/Kconfig"\n')
    patches = work / "susfs/kernel_patches"
    adaptation = ROOT / "patches/susfs-6138-context.patch"
    run(["git", "apply", "--check", str(adaptation)], work / "susfs")
    run(["git", "apply", str(adaptation)], work / "susfs")
    patch = patches / "50_add_susfs_in_gki-android14-6.1.patch"
    run(["git", "apply", "--check", str(patch)], common)
    run(["git", "apply", str(patch)], common)
    for folder in ["fs", "include/linux"]:
        for source in (patches / folder).iterdir():
            if source.is_file():
                shutil.copy2(source, common / folder / source.name)
    # The builtin branch already has the root-side SuSFS hooks.
    if "susfs_init();" not in (root / "ksu_main.c").read_text():
        raise ValueError("missing root SuSFS initialization")
    bridge = (root / "extension/kpm/bridge.h").read_text()
    backend = (work / "kernelpatch/kernel/patch/sukisu/supercall.h").read_text()
    for name in ["ABI_VERSION", "SUPERCALL_MAGIC", "SUPERCALL_MAGIC2", "SUPERCALL_MAGIC3"]:
        pattern = rf"^#define SUKISU_KPM_{name}\s+(\S+)"
        left, right = re.search(pattern, bridge, re.M), re.search(pattern, backend, re.M)
        if not left or not right or left[1] != right[1]:
            raise ValueError(f"KPM bridge contract mismatch: {name}")
    # Keep strict KMI symbol-list checks. Only add the four new KPM exports.
    build = common / "BUILD.bazel"
    replace_once(build, '        "android/abi_gki_aarch64_xiaomi",',
                 '        "android/abi_gki_aarch64_sukisu",\n        "android/abi_gki_aarch64_xiaomi",')
    # Author's Android 14 SuSFS instruction; affects unsigned vendor modules.
    replace_once(build,
                 '        "protected_exports_list": "android/abi_gki_protected_exports_aarch64",\n'
                 '        "protected_modules_list": "android/gki_aarch64_protected_modules",\n'
                 '        "make_goals": _GKI_AARCH64_MAKE_GOALS,\n    },\n    "kernel_aarch64_16k":',
                 '        "protected_exports_list": None,\n'
                 '        "protected_modules_list": "android/gki_aarch64_protected_modules",\n'
                 '        "make_goals": _GKI_AARCH64_MAKE_GOALS,\n    },\n    "kernel_aarch64_16k":')
    # Modified defaults cannot use the stock savedefconfig text comparison.
    # The independent resolved-config gate below replaces that text-only check.
    replace_once(common / "build.config.gki", 'POST_DEFCONFIG_CMDS="check_defconfig"',
                 'POST_DEFCONFIG_CMDS=""')
    # Record actual .config as a declared build output, not an intended fragment.
    replace_once(tree / "build/kernel/kleaf/constants.bzl",
                 'DEFAULT_GKI_OUTS = _common_outs + DEFAULT_IMAGES',
                 'DEFAULT_GKI_OUTS = _common_outs + DEFAULT_IMAGES + '
                 '[".config", "gki_module_protected_exports.h"]')
    return tree


def configure(tree, kpm):
    common = tree / "common"
    # Kbuild reads this file even when the Bazel protected_exports_list is None.
    # Keep an empty file so incremental header generation also replaces stale data.
    (common / "android/abi_gki_protected_exports_aarch64").write_text("")
    config = common / "arch/arm64/configs/gki_defconfig"
    text = config.read_text()
    for name in ["KSU", "KSU_SUSFS", "KPM", "IKCONFIG", "IKCONFIG_PROC"]:
        text = re.sub(rf"^(?:CONFIG_{name}=.*|# CONFIG_{name} is not set)\n",
                      "", text, flags=re.M)
    text += "\nCONFIG_KSU=y\nCONFIG_KSU_SUSFS=y\nCONFIG_IKCONFIG=y\nCONFIG_IKCONFIG_PROC=y\n"
    text += "CONFIG_KPM=y\n" if kpm else "# CONFIG_KPM is not set\n"
    config.write_text(text)
    (common / "android/abi_gki_aarch64_sukisu").write_text(
        "[abi_symbol_list]\n" + "".join(f"  {s}\n" for s in EXPORTS if kpm))


def build_variant(tree, out, name, kpm):
    configure(tree, kpm)
    log = out / f"{name}-build.log"
    with log.open("w") as stream:
        run(["tools/bazel", "build", "--lto=none", "--jobs=2",
             "//common:kernel_aarch64"], tree, stdout=stream, stderr=subprocess.STDOUT)
    products = tree / "bazel-bin/common/kernel_aarch64"
    names = ["Image", "vmlinux", "System.map", ".config", "vmlinux.symvers",
             "gki_module_protected_exports.h"]
    require_outputs(products, names)
    data = (products / "Image").read_bytes()
    report = validate_image(data)
    report["module_exports"] = validate_protected_exports(
        (products / "gki_module_protected_exports.h").read_text())
    config_text = (products / ".config").read_text()
    config = validate_config(config_text, require_kpm=kpm)
    for key in ["CFI_CLANG", "SHADOW_CALL_STACK", "LTO_NONE", "MODVERSIONS", "MODULE_SIG_PROTECT"]:
        if config.get(f"CONFIG_{key}") != "y":
            raise ValueError(f"stock critical setting lost: CONFIG_{key}")
    begin, end = data.find(b"IKCFG_ST"), data.find(b"IKCFG_ED")
    if begin < 0 or end < begin:
        raise ValueError("missing actual embedded config")
    if validate_config(gzip.decompress(data[begin + 8:end]).decode(), kpm) != config:
        raise ValueError("embedded config does not match resolved .config")
    symbols = (products / "System.map").read_text()
    for symbol in ["kernelsu_init", "susfs_init", "ksu_handle_sys_reboot", "ksu_handle_execveat"]:
        if not re.search(rf"\b{symbol}$", symbols, re.M):
            raise ValueError(f"missing linked feature symbol: {symbol}")
    if kpm:
        for symbol in EXPORTS + ["sukisu_kpm_bridge"]:
            if not re.search(rf"\b{symbol}$", symbols, re.M):
                raise ValueError(f"missing linked KPM interface: {symbol}")
    if "KSU_NO_PREINIT" in log.read_text() or "preinit disabled" in log.read_text():
        raise ValueError("SukiSU preinit was disabled")
    destination = out / name
    destination.mkdir()
    for filename in names:
        shutil.copy2(products / filename, destination / filename)
    (destination / "validation.json").write_text(json.dumps(report, indent=2) + "\n")
    return destination


def patch_kpm(work, variant, out, lock):
    component = work / "kernelpatch"
    shutil.copy2(component / "kernel/include/preset.h", component / "tools/preset.h")
    run(["make", "-C", str(component / "tools"), "-j2"])
    archive = work / "arm-toolchain.tar.xz"
    urllib.request.urlretrieve(lock["arm_toolchain"]["url"], archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != lock["arm_toolchain"]["sha256"]:
        raise ValueError("ARM toolchain checksum mismatch")
    with tarfile.open(archive) as stream:
        stream.extractall(work, filter="data")
    compiler = work / "arm-gnu-toolchain-12.2.rel1-x86_64-aarch64-none-elf/bin/aarch64-none-elf-"
    run(["make", "-C", str(component / "kernel"), "-j2", f"TARGET_COMPILE={compiler}"])
    tool, payload = component / "tools/kptools", component / "kernel/kpimg"
    require_outputs(tool.parent, ["kptools"])
    require_outputs(payload.parent, ["kpimg", "kpimg.elf"])
    original = variant / "Image"
    patched = variant / "Image.kpm"
    with (out / "kpm-patch.log").open("w") as stream:
        # This SukiSU fork removed APatch root; it supports no superkey.
        run([tool, "-p", "-i", original, "-k", payload, "-o", patched],
            stdout=stream, stderr=subprocess.STDOUT)
    report = validate_image(patched.read_bytes())
    if report["sha256"] == hashlib.sha256(original.read_bytes()).hexdigest():
        raise ValueError("KPM patch did not change Image")
    with (variant / "kpm-metadata.txt").open("w") as stream:
        run([tool, "-l", "-i", patched], stdout=stream, stderr=subprocess.STDOUT)
    metadata = (variant / "kpm-metadata.txt").read_text()
    if "kpimg" not in metadata.lower():
        raise ValueError("KPM metadata missing")
    (variant / "kpm-validation.json").write_text(json.dumps(report, indent=2) + "\n")
    for source in [tool, payload, component / "kernel/kpimg.elf"]:
        shutil.copy2(source, variant / source.name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    work, out = args.work.resolve(), args.out.resolve()
    if work == out or work in out.parents or out in work.parents:
        raise ValueError("build work and deliverables must be separate")
    out.mkdir(parents=True, exist_ok=True)
    lock = json.loads((ROOT / "shennong-source-lock.json").read_text())
    shutil.copy2(ROOT / "shennong-source-lock.json", out)
    tree = prepare(work, out, lock)
    with (out / "common-integration.patch").open("w") as stream:
        run(["git", "diff", "--binary"], tree / "common", stdout=stream)
    build_variant(tree, out, "sukisu-susfs-base", False)
    final = build_variant(tree, out, "sukisu-susfs-kpm", True)
    patch_kpm(work, final, out, lock)
    checksums = []
    for path in sorted(out.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            checksums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(out)}")
    (out / "SHA256SUMS").write_text("\n".join(checksums) + "\n")
    print("Static build gates passed; device compatibility remains untested.", flush=True)


if __name__ == "__main__":
    main()
