# Shennong Experimental Build

This branch builds Xiaomi 14 Pro candidates for OS3.0.310.0.WNBCNXM.
It does not access a phone, flash partitions, change installed root modules,
publish a Release, or send Telegram messages.

## Identity

All source inputs are full commits in `shennong-source-lock.json`.
The stock ACK common commit is 0c3d559bcd85c64dae9c9c9687abbe5347fff3ad;
superproject c5fd854d46f3fe29b89a80e5f2cf3bc643c64f20 pins its build
system and Clang r487747c. Linux 6.1.138, KMI generation 11, little-endian
4K pages, no LTO, CFI and shadow call stack match the inspected stock config.
This does not reproduce Google's signing keys, release infrastructure,
or prove compatibility with Xiaomi's proprietary vendor modules.

SukiSU's builtin source is used directly; the root-side SuSFS patch is not
applied twice. The filesystem patch is checked before applying. SukiSU's
moving network-derived version metadata is replaced with recorded values,
without changing root logic or presenting a main-branch checkout as builtin.
The three newer ACK include contexts in the SuSFS patch are adapted to this
stock revision by `patches/susfs-6138-context.patch`; no functional SuSFS
patch additions/removals are changed.

## Gates and Intentional Differences

- Checkout, patch, build and required outputs fail closed.
- The stock defconfig text-comparison step is replaced by validation of the
  actual resolved `.config`, its independently extracted embedded copy,
  ARM64 Image structure and linked feature symbols.
- KMI trimming and strict symbol-list checks remain enabled.
- Only the four additional KPM export names are added for the KPM build.
- The aarch64 protected-exports list is omitted per the SuSFS author's
  Android 14 build instructions. This differs from stock module protection
  and requires vendor-module/runtime testing; it is not an ABI guarantee.
- The KPM payload and host tool are compiled from one fixed SukiSU fork.
  Its kernel-to-kernel framing and ABI v2 are checked before building.
- Original, unpatched Images are retained. Patched entries must lie inside
  the actual file, including the historical EOF-branch regression.
- No generic 64 MiB boot image is delivered. Device-specific packaging is
  a later offline step using the matching stock boot format.

## Running

Dispatch the existing `Kernel Build` workflow on this feature branch with
`shennong_pinned=true`. Generic workflow fields are ignored in this mode.
The reusable workflow has read-only repository permissions, no secrets,
and uploads either verified build evidence or clearly named failure logs.

Run local regressions with `python -m unittest discover -s tests -v`.
Passing compilation/static gates means an experimental candidate only.
Flashing, root migration and benign KPM load/unload tests require separate
human approval and verified rollback files.

## Offline Packaging

`shennong_package.py` consumes only independently validated CI Images,
their resolved and embedded configurations, and the exact stock boot
backup. Packaging tools must have the pinned HEAD and no local changes.
It creates a canonical kernel-only v4 boot at the verified 96 MiB
partition size, with a recomputed unsigned AVB hash footer. Invalid OEM/GKI
signatures from the old kernel are not carried over.

The separate AnyKernel3 ZIP includes only the selected Image and a pinned
kernel-only installer. Its checks restrict it to shennong,
OS3.0.310.0.WNBCNXM and the verified stock boot hash. It targets active-slot
boot only, not init_boot, vendor_boot, DTB or global vbmeta. Neither the
installer nor bootloader acceptance is verified by an offline ZIP
round-trip. Both package types remain explicitly experimental.

The AnyKernel template's default binaries are ARM32. Packaging instead
requires `--installer-assets` pointing to the unmodified official
Magisk-v31.1.apk and osm0sis BusyBox 13614 release ZIP. Both archive and
selected member SHA-256 values are fixed in `INSTALLER_SOURCES`.
Only native little-endian AArch64 ELF64 static executables with a
file-backed entry are accepted. No APK/module installer is run or copied
into the kernel package.

Sources for this replacement:

- https://github.com/osm0sis/AnyKernel3/blob/41f04bdb4ee30a44e429c2c1131d5e8bb32b15f1/README.md#--binary-inclusion-
- https://github.com/topjohnwu/Magisk/releases/tag/v31.1
- https://github.com/Magisk-Modules-Repo/busybox-ndk/releases/tag/13614

KPM packaging independently parses the pinned fork's preset. It verifies
the compiled payload's header/code, original Image identity, setup entry,
empty extra-module region and copy reservation within declared Image
memory. The patch tool's status text alone is not acceptance.

The unsigned canonical boot packaging removes the stock's obsolete GKI
certificate trailer. The AnyKernel installer instead relies on its pinned
magiskboot repacker; its on-device certificate/footer handling has not been
verified offline. Prefer the independently inspected full boot candidate
when reviewing the later test procedure.

No package is produced from failure diagnostics or the old malformed
Image. Re-extracted Image hashes must match the validated build.

## Verified Build, Not Runtime Acceptance

Run 41042524296 built source f778b62 successfully in 37m43s:
https://github.com/Rat0323/GKI_KernelSU_SUSFS/actions/runs/41042524296.
All 23 extracted artifact files match the CI checksum manifest.
Both actual configurations, linked feature paths, Image entry geometry,
and the compiled/embedded KPM payload passed independent offline checks.

The base and KPM builds share 8142 export names with unchanged CRCs; the
KPM build adds four SukiSU interfaces. This is a comparison between our
two variants, not verification of Xiaomi vendor-module CRCs.
The actual banner is 6.1.138-android14-11-maybe-dirty with reproducible
Kleaf metadata, not the OEM release identity or its signing process.

Although CONFIG_CFI_CLANG remains enabled, the KPM payload's
before_rest_init calls bypass_kcfi. Runtime CFI protection is therefore
not promised to be unchanged. The payload ELF also has an RWE LOAD
segment; final runtime permissions have not been measured.
Source:
https://github.com/SukiSU-Ultra/SukiSU_KernelPatch_patch/blob/1c5b608916357ac86a94cc736e235db1fac4a29d/kernel/patch/patch.c.

AnyKernel installer execution, bootloader acceptance, root/LKM migration,
vendor modules and benign KPM load/query/unload remain untested.
