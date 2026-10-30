# Sourced after the pinned AnyKernel setup; never repack or write other partitions.
shennong_install_boot() {
  local image expected_sha size stock_sha slot target actual backup;
  image=$1; expected_sha=$2; size=$3; stock_sha=$4;
  [ "$(getprop ro.product.device)" = shennong ] || abort "Unsupported device.";
  [ "$(getprop ro.build.version.incremental)" = OS3.0.310.0.WNBCNXM ] || abort "Unsupported ROM.";
  # Root modules can spoof ro.boot properties; this target provides bootconfig.
  actual=$(sed -n 's/^androidboot\.vbmeta\.device_state = "\([^"]*\)"$/\1/p' /proc/bootconfig) || abort "Cannot read bootloader state.";
  [ "$actual" = unlocked ] || abort "Unlocked bootloader required.";
  slot=$(sed -n 's/^androidboot\.slot_suffix = "\([^"]*\)"$/\1/p' /proc/bootconfig) || abort "Cannot read active slot.";
  case "$slot" in _a|_b) ;; *) abort "Invalid active slot.";; esac;
  [ "$SLOT" = "$slot" ] || abort "Active slot mismatch.";
  target=/dev/block/by-name/boot$slot;
  test -b "$target" || target=/dev/block/bootdevice/by-name/boot$slot;
  test -b "$target" || abort "Active boot partition missing.";
  target=$(readlink -f "$target") || abort "Cannot resolve active boot partition.";
  actual=$(readlink -f "$BLOCK") || abort "Cannot resolve installer target.";
  [ -n "$target" ] && [ "$actual" = "$target" ] || abort "Refusing a non-boot target.";
  actual=$(blockdev --getsize64 "$target") || abort "Cannot read partition size.";
  [ "$actual" = "$size" ] || abort "Unexpected boot partition size.";
  actual=$(stat -c %s "$image") || abort "Missing canonical boot image.";
  [ "$actual" = "$size" ] || abort "Canonical boot size mismatch.";
  actual=$(sha256sum "$image") || abort "Cannot hash canonical boot image.";
  [ "${actual%% *}" = "$expected_sha" ] || abort "Canonical boot hash mismatch.";

  backup=$AKHOME/stock-boot.img;
  dd if="$target" of="$backup" bs=1048576 || abort "Stock boot read failed.";
  actual=$(sha256sum "$backup") || abort "Cannot hash stock boot backup.";
  [ "${actual%% *}" = "$stock_sha" ] || abort "Restore the verified stock boot before testing.";

  # Unlike the generic repacker, write exactly the verified partition-sized payload.
  dd if="$image" of="$target" bs=1048576 conv=fsync || abort "Boot write failed. Restore stock boot before rebooting.";
  sync || abort "Boot sync failed. Restore stock boot before rebooting.";
  actual=$(sha256sum "$target") || abort "Boot readback failed. Restore stock boot before rebooting.";
  [ "${actual%% *}" = "$expected_sha" ] || abort "Boot readback mismatch. Restore stock boot before rebooting.";
  ui_print "Verified exact boot image on active slot $slot. Runtime boot is not yet tested.";
}
