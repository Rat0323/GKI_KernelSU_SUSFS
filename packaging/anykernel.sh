#!/sbin/sh
# Experimental shennong kernel-only package. Requires separate test approval.
properties() { '
kernel.string=Shennong SukiSU SuSFS experimental
do.devicecheck=1
do.modules=0
do.systemless=0
do.cleanup=1
do.cleanuponabort=0
device.name1=shennong
supported.versions=16
supported.patchlevels=
supported.vendorpatchlevels=
'; }

BLOCK=boot;
IS_SLOT_DEVICE=1;
SLOT_SELECT=active;
RAMDISK_COMPRESSION=auto;
PATCH_VBMETA_FLAG=0;
NO_MAGISK_CHECK=1;
NO_VBMETA_PARTITION_PATCH=1;
. tools/ak3-core.sh;

rom=$(getprop ro.build.version.incremental);
[ "$rom" = "OS3.0.310.0.WNBCNXM" ] || abort "This package is restricted to OS3.0.310.0.WNBCNXM.";
split_boot;
stock_sha=f75f52b48aa1f955a26e8c9cd7ff072a4a7ac91dcb54b6f82b809a287997c861;
current_sha=$(sha256sum "$BOOTIMG" | cut -d' ' -f1);
[ "$current_sha" = "$stock_sha" ] || abort "Expected the verified stock boot. Restore the known-good baseline before testing.";
flash_boot;
