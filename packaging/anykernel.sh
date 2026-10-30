#!/sbin/sh
# Device-specific canonical boot package. Requires separate test approval.
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

stock_sha=f75f52b48aa1f955a26e8c9cd7ff072a4a7ac91dcb54b6f82b809a287997c861;
. tools/shennong-flash.sh || abort "Missing guarded installer.";
shennong_install_boot "$AKHOME/candidate-boot.img" "@BOOT_SHA256@" @PARTITION_SIZE@ "$stock_sha";
