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
